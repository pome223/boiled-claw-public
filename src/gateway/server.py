"""
WebSocket Gateway Server

Typed Gateway Protocol v1:
  Client -> Server: chat.send / control.run / chat.inject / chat.abort / chat.history /
                    presence.ping / tools.approval
  Server -> Client: connected / chat.done / chat.token / chat.history /
                    system.event / health.tick / cron.update /
                    tools.approval_request / control.approval_request
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
from datetime import datetime
import uuid
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types
from pathlib import Path

from src.config.settings import get_settings
from src.agents.root_agent import root_agent
from src.agents.sub_agents import SUB_AGENTS
from src.control_loop.live_failure_taxonomy import classify_control_loop_failure
from src.control_loop.root_workflow import ControlLoop, ExecutionResult
from src.gateway.routing_agent import routing_agent
from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
from src.security.audit import get_audit_logger, AuditEventType
from src.security.tool_policy import APPROVAL_EXPIRY_REASONS, get_tool_policy_engine
from src.tools.finance import is_direct_stock_price_query, stock_price
from src.tools.web_search import web_search
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import (
    capability_invoke as tool_capability_invoke,
    capability_list as tool_capability_list,
    resource_list as tool_resource_list,
    resource_read as tool_resource_read,
    skill_execute as tool_skill_execute,
    skill_list as tool_skill_list,
)
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier
from src.gateway.protocol import (
    EVENT_SCHEMAS,
    HTTP_ROUTE_SCHEMAS,
    PROTOCOL_VERSION,
    RUNTIME_SUBSTRATE_SCHEMA,
    ev_chat_done, ev_system_event,
    ev_health_tick, ev_cron_update, ev_tools_approval_request,
    ev_control_approval_request,
    ev_tools_approval_update, ev_task_update, ev_audit_append,
    ev_tool_start, ev_tool_result,
)
from src.gateway.routing import (
    RoutingDecision,
    decision_from_payload,
    heuristic_decision,
    targets_user_browser,
)
from src.gateway.task_replay import (
    persist_control_loop_step_events,
)
from src.gateway.control_supervisor import ControlLoopSupervisor
from src.gateway.route_utils import normalize_constraints
from src.gateway.task_routes import build_task_router
from src.gateway.audit_routes import build_audit_router
from src.gateway.ws_handler import build_websocket_router
from src.runtime.tool_events import set_tool_event_notifier
from src.runtime.session_service import create_session_service, describe_session_backend
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import SPREADSHEET_KEYWORDS, prefers_isolated_browser_for_goal
from src.gateway.transcript import get_transcript_store
from src.cron.scheduler import get_scheduler
from src.runtime.task_store import get_task_store
from src.tools.tasks import create_task_record, update_task_record

_HEARTBEAT_INTERVAL = 30  # seconds
_AGENT_TIMEOUT = 120       # seconds
_MAX_PENDING_PER_SESSION = 100
_MAX_PENDING_SESSIONS = 500
_FRESHNESS_KEYWORDS = {
    "最新", "最近", "ニュース", "調べて", "噂", "今年", "来日", "予定",
    "公演", "ライブ", "フェス", "開催", "話題", "リサーチ", "調査",
    "推測", "予測", "見通し", "発表", "gtc", "tour", "festival",
}

_BROWSER_TOOL_NAMES = {
    "control_ui_chat_send_message",
    "computer_observe",
    "computer_click",
    "computer_fill",
    "current_tab_info",
    "current_tab_navigate",
    "current_tab_click",
    "current_tab_fill",
    "current_tab_extract_text",
    "browser_navigate",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_extract_text",
    "browser_screenshot",
    "host.browser.navigate",
    "host.browser.click",
    "host.browser.fill",
    "host.browser.press",
    "host.browser.extract_text",
    "host.browser.screenshot",
    "host.control_ui_chat.send_message",
    "host.current_tab.info",
    "host.current_tab.navigate",
    "host.current_tab.click",
    "host.current_tab.fill",
    "host.current_tab.extract_text",
}
_BROWSER_INFRA_ERROR_FRAGMENTS = (
    "playwright is not installed",
    "host bridge is not enabled",
    "requires host bridge",
    "host_bridge_enabled is true but host_bridge_url is not set",
    "host bridge tool call failed",
    "host bridge returned empty tool content",
    "host bridge returned non-json tool content",
    "current tab extension bridge",
    "current tab extension relay",
    "current tab extension is not connected",
    "desktop bridge",
    "desktop_bridge_enabled",
)
_USER_BROWSER_REQUIRED_CAPABILITIES = {
    "desktop.view.frontmost_app",
    "desktop.view.windows",
    "desktop.control.focus_window",
    "desktop.ax.find",
    "desktop.control.click",
    "desktop.control.type",
}
_CONTROL_LOOP_FOLLOWUP_MARKERS = (
    "記載して",
    "入力して",
    "転記して",
    "スプレッドシートに",
    "sheetに",
    "spreadsheetに",
)
_CURRENT_BROWSER_CONTROL_BASE_CONSTRAINTS = [
    "Operate only on the currently visible browser/tab/window.",
    "Do not launch a new browser application or open a managed browser for this task.",
    "Start by identifying the frontmost app and matching it to the existing browser window.",
    "If the current browser window cannot be identified or focused, stop and report an explicit error.",
    "Do not mark the task complete after typing alone; submit the action and verify the resulting page content.",
]
_CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT = (
    "Do not open a new browser tab or window unless the user explicitly asked for it."
)
_CURRENT_BROWSER_PRESERVE_CONTROL_UI_TAB_CONSTRAINT = (
    "If the current tab is the boiled-claw Control UI chat, preserve that tab and "
    "open a new tab in the same browser window for browsing or search. Otherwise "
    "stay on the current tab."
)
_ISOLATED_BROWSER_TEXT_ENTRY_CONSTRAINTS = [
    "For current-browser visible text-entry or form-filling work, use an isolated browser or managed browser page instead of the user's existing browser tabs or forms.",
    "Do not interact with pre-existing browser tabs, windows, or form fields owned by the user.",
    "Verify the final URL/title/content inside that isolated browser session before marking the task complete.",
]


@dataclass
class SpecialistToolFailure:
    tool_name: str
    error: str
    infrastructure: bool = False


@dataclass
class SpecialistPrepassResult:
    text: str = ""
    tool_failures: list[SpecialistToolFailure] = field(default_factory=list)
    used_tools: set[str] = field(default_factory=set)
    evidence_blocks: list[str] = field(default_factory=list)

    @property
    def infrastructure_blocked(self) -> bool:
        return any(item.infrastructure for item in self.tool_failures)

    @property
    def browser_failure(self) -> bool:
        return any(item.tool_name in _BROWSER_TOOL_NAMES for item in self.tool_failures)


class ConnectionManager:
    """WebSocket connection + running task management"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # session_id -> user_id mapping for system event routing
        self._session_users: Dict[str, str] = {}
        self._pending_events: Dict[str, list[dict[str, Any]]] = {}

    async def connect(self, websocket: WebSocket, session_id: str, user_id: str = "") -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket
        if user_id:
            self._session_users[session_id] = user_id

    def disconnect(
        self,
        session_id: str,
        *,
        preserve_pending: bool = False,
        preserve_user: bool = False,
    ) -> None:
        self.active_connections.pop(session_id, None)
        if not preserve_user:
            self._session_users.pop(session_id, None)
        if not preserve_pending:
            self._pending_events.pop(session_id, None)

    def get_user_id(self, session_id: str) -> Optional[str]:
        return self._session_users.get(session_id)

    async def send_json(self, session_id: str, data: dict) -> None:
        ws = self.active_connections.get(session_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def broadcast_json(self, data: dict, exclude: Optional[str] = None) -> None:
        for sid, ws in list(self.active_connections.items()):
            if sid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def send_or_queue_json(self, session_id: str, data: dict) -> None:
        if session_id in self.active_connections:
            await self.send_json(session_id, data)
            return
        queue = self._pending_events.setdefault(session_id, [])
        if len(queue) >= _MAX_PENDING_PER_SESSION:
            queue.pop(0)
        queue.append(data)
        if len(self._pending_events) > _MAX_PENDING_SESSIONS:
            oldest_key = next(iter(self._pending_events))
            del self._pending_events[oldest_key]

    async def flush_pending(self, session_id: str) -> None:
        pending = self._pending_events.pop(session_id, [])
        for payload in pending:
            await self.send_json(session_id, payload)

    async def flush_all_pending(self) -> None:
        for session_id in list(self.active_connections.keys()):
            await self.flush_pending(session_id)

    # --- task tracking for abort ---

    def set_task(self, session_id: str, task: asyncio.Task) -> None:
        self._tasks[session_id] = task

    def clear_task(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)

    async def abort(self, session_id: str) -> bool:
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False


class GatewayServer:
    """Gateway server with typed protocol, transcript, cron platform, and tool security."""

    def __init__(self):
        self.settings = get_settings()
        self.session_backend = describe_session_backend(self.settings)
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.manager = ConnectionManager()
        self.session_service = create_session_service(self.settings)
        self.memory_service = get_promoted_memory_service()
        self.subagent_manager = get_subagent_manager()
        self.runner = Runner(
            agent=root_agent,
            app_name="boiled-claw",
            session_service=self.session_service,
            memory_service=self.memory_service,
        )
        self.routing_session_service = create_session_service(self.settings)
        self.routing_runner = Runner(
            agent=routing_agent,
            app_name="boiled-claw-router",
            session_service=self.routing_session_service,
            memory_service=self.memory_service,
        )
        self.specialist_runners = {
            agent.name: Runner(
                agent=agent,
                app_name="boiled-claw",
                session_service=self.session_service,
                memory_service=self.memory_service,
            )
            for agent in SUB_AGENTS
        }
        self.control_loop = ControlLoop(
            session_service=self.session_service,
            memory_service=self.memory_service,
        )
        self.audit_logger = get_audit_logger()
        self.task_store = get_task_store()
        self.transcript = get_transcript_store()
        self.tool_policy = get_tool_policy_engine()
        self.control_supervisor = ControlLoopSupervisor(
            run_control_loop_with_task=self._run_control_loop_with_task,
            emit_session_event=self._emit_session_event,
        )
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.app = FastAPI(
            title="boiled-claw Gateway",
            version="0.3.0",
            lifespan=self._lifespan,
        )

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            api_key = self.settings.gateway_api_key
            if not api_key:
                return await call_next(request)
            public_prefixes = ("/health", "/chat-static", "/chat", "/protocol")
            if any(request.url.path.startswith(p) for p in public_prefixes) or request.url.path == "/":
                return await call_next(request)
            token = (
                request.headers.get("X-API-Key")
                or request.headers.get("Authorization", "").removeprefix("Bearer ")
                or request.query_params.get("token")
            )
            if token != api_key:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

        @self.app.middleware("http")
        async def chat_cache_control_middleware(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            if path == "/chat" or path.startswith("/chat-static"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        self.app.mount(
            "/chat-static",
            StaticFiles(directory=str(self.static_dir)),
            name="chat_static",
        )

        # subagent -> WS notifier
        async def _subagent_notifier(payload: Dict[str, Any]) -> None:
            session_id = payload.get("requester_session_id")
            if not session_id:
                return
            await self._emit_session_event(
                session_id,
                source="subagent",
                status=payload.get("status", ""),
                message=payload.get("message", ""),
                run_id=payload.get("run_id"),
                task_id=payload.get("task_id"),
                agent_name=payload.get("agent_name"),
            )

        self._subagent_notifier_fn = _subagent_notifier

        # cron -> WS/session notifier
        async def _cron_notifier(payload: Dict[str, Any]) -> None:
            event = ev_cron_update(
                job_id=payload.get("job_id", ""),
                status=payload.get("status", ""),
                message=payload.get("message", ""),
            )
            await self.manager.broadcast_json(event)
            requester_session_id = payload.get("requester_session_id")
            if requester_session_id and self.transcript.has_session(requester_session_id):
                await self._emit_session_event(
                    requester_session_id,
                    source="cron",
                    status=payload.get("status", ""),
                    message=payload.get("message", ""),
                )

        self._cron_notifier_fn = _cron_notifier

        async def _approval_notifier(payload: Dict[str, Any]) -> None:
            session_id = payload.get("session_id", "")
            if not session_id:
                return
            approval_payload = {key: value for key, value in payload.items() if key != "event_type"}
            approval_event = str(payload.get("event_type") or "updated")
            await self.manager.send_or_queue_json(
                session_id,
                ev_tools_approval_update(
                    approval_payload,
                    approval_event=approval_event,
                ),
            )
            if approval_payload.get("state") != "pending":
                return
            await self.manager.send_or_queue_json(
                session_id,
                ev_tools_approval_request(
                    request_id=approval_payload.get("request_id", ""),
                    tool_name=approval_payload.get("tool_name", ""),
                    agent_name=approval_payload.get("agent_name", ""),
                    args=approval_payload.get("args") or {},
                    reason=approval_payload.get("reason", ""),
                    state=approval_payload.get("state", "pending"),
                    scope=approval_payload.get("scope", "single"),
                    tool_pattern=approval_payload.get("tool_pattern"),
                    path_scope=approval_payload.get("path_scope"),
                    expires_at=approval_payload.get("expires_at"),
                    propagate_to_subagents=bool(approval_payload.get("propagate_to_subagents", False)),
                    source_request_id=approval_payload.get("source_request_id"),
                ),
            )

        self._approval_notifier_fn = _approval_notifier

        async def _task_notifier(payload: Dict[str, Any]) -> None:
            task = payload.get("task")
            task = task if isinstance(task, dict) else {}
            owner_session_id = str(task.get("owner_session_id") or "")
            if not owner_session_id:
                return
            await self.manager.send_or_queue_json(
                owner_session_id,
                ev_task_update(task, payload.get("event") or {}),
            )

        self._task_notifier_fn = _task_notifier

        def _iter_audit_push_sessions(payload: Dict[str, Any]) -> list[str]:
            metadata = payload.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            primary_session_id = str(payload.get("session_id") or "").strip()
            target_session_id = str(metadata.get("target_session_id") or "").strip()

            session_ids = {primary_session_id} if primary_session_id else set()
            # Only approval resolution events are allowed to fan out to an
            # explicitly-targeted session. Other audit types stay session-local.
            if (
                target_session_id
                and target_session_id != primary_session_id
                and str(payload.get("event_type") or "") == AuditEventType.TOOL_APPROVAL.value
            ):
                session_ids.add(target_session_id)
            return sorted(session_ids)

        async def _audit_notifier(payload: Dict[str, Any]) -> None:
            for session_id in _iter_audit_push_sessions(payload):
                if session_id not in self.manager.active_connections:
                    continue
                await self.manager.send_json(session_id, ev_audit_append(payload))

        self._audit_notifier_fn = _audit_notifier
        self._setup_routes()

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        await self._startup_gateway()
        try:
            yield
        finally:
            await self._shutdown_gateway()

    async def _startup_gateway(self) -> None:
        await ensure_skills_loaded()
        set_subagent_notifier(self._subagent_notifier_fn)
        set_tool_event_notifier(self._send_tool_event)
        self.tool_policy.set_notifier(self._approval_notifier_fn)
        self.task_store.set_notifier(self._task_notifier_fn)
        self.audit_logger.set_notifier(self._audit_notifier_fn)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="heartbeat"
            )
        scheduler = get_scheduler()
        scheduler.set_spawn_fn(self._spawn_cron_target)
        scheduler.set_notifier(self._cron_notifier_fn)
        scheduler.start()
        await scheduler.fire_system_event("startup")

    async def _shutdown_gateway(self) -> None:
        await self.control_supervisor.shutdown()
        set_subagent_notifier(None)
        set_tool_event_notifier(None)
        self.tool_policy.set_notifier(None)
        self.task_store.set_notifier(None)
        self.audit_logger.set_notifier(None)
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None
        await get_scheduler().shutdown()

    def _should_force_web_research(self, message: str) -> bool:
        normalized = (message or "").strip().lower()
        if not normalized or is_direct_stock_price_query(message):
            return False
        return any(keyword in normalized for keyword in _FRESHNESS_KEYWORDS)

    def _select_web_search_timelimit(self, message: str) -> str:
        normalized = (message or "").strip().lower()
        if any(keyword in normalized for keyword in {"今日", "きょう", "today", "速報"}):
            return "d"
        if any(keyword in normalized for keyword in {"今年", "来日", "公演", "ライブ", "フェス", "予定", "開催"}):
            return "y"
        if any(keyword in normalized for keyword in {"最新", "最近", "ニュース", "噂", "発表", "gtc"}):
            return "w"
        return "m"

    async def _send_tool_event(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        if session_id not in self.manager.active_connections:
            return
        await self.manager.send_or_queue_json(session_id, payload)

    def _summarize_tool_result(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            summarized: dict[str, Any] = {}
            for key, value in payload.items():
                if key in {"results"} and isinstance(value, list):
                    summarized[key] = value[:3]
                    summarized["count"] = len(value)
                    continue
                if isinstance(value, str):
                    summarized[key] = value[:400]
                elif isinstance(value, list):
                    summarized[key] = value[:5]
                elif isinstance(value, dict):
                    summarized[key] = self._summarize_tool_result(value)
                else:
                    summarized[key] = value
            return summarized
        return {"value": str(payload)[:400]}

    @staticmethod
    def _tool_response_error(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        error = str(response.get("error") or "").strip()
        if error:
            return error
        if response.get("success") is False:
            return "tool reported success=false"
        if response.get("ok") is False:
            return "tool reported ok=false"
        return None

    @staticmethod
    def _is_browser_infrastructure_error(tool_name: str, error: str) -> bool:
        if tool_name not in _BROWSER_TOOL_NAMES:
            return False
        normalized = (error or "").strip().lower()
        return any(fragment in normalized for fragment in _BROWSER_INFRA_ERROR_FRAGMENTS)

    def _format_specialist_runtime_failure(
        self,
        specialist_name: str,
        result: SpecialistPrepassResult,
    ) -> str:
        first_error = next(
            (item.error for item in result.tool_failures if item.error),
            "required runtime is unavailable",
        )
        if specialist_name in {"browser_automator", "control_ui_chat_operator", "current_tab_operator"} and result.infrastructure_blocked:
            if specialist_name == "current_tab_operator":
                return (
                    "現在のブラウザ/タブ操作は実行できませんでした。\n"
                    f"- 原因: {first_error}\n"
                    "- 現在のリクエストでは、desktop 制御や managed browser へ自動フォールバックしません。\n"
                    "- 対応: Host Bridge を起動し、Chrome に Current Tab Adapter extension を読み込んでください。"
                )
            return (
                "ブラウザ操作は実行できませんでした。\n"
                f"- 原因: {first_error}\n"
                "- 現在のリクエストでは、ブラウザを実際に操作せずに web_search へ自動フォールバックしません。\n"
                "- 対応: Host Bridge を有効化して host 側で Playwright を実行するか、"
                "この実行環境に Playwright をインストールしてください。"
            )

        if specialist_name == "computer_operator" and result.infrastructure_blocked:
            return (
                "computer use は実行できませんでした。\n"
                f"- 原因: {first_error}\n"
                "- 現在のリクエストでは、見えているブラウザや GUI を前提にした操作が必要です。\n"
                "- 対応: Host Bridge / Current Tab relay / Desktop Bridge の必要な runtime を起動し、"
                "現在のブラウザまたは対象 GUI が host 側で操作可能な状態にしてください。"
            )

        return (
            f"{specialist_name} の実行に失敗しました。\n"
            f"- 原因: {first_error}"
        )

    async def _current_browser_runtime_error(
        self,
        message: str,
    ) -> str | None:
        if prefers_isolated_browser_for_goal(message):
            return None
        if not targets_user_browser(message):
            return None

        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return (
                "現在開いているブラウザや既存のスプレッドシートは操作できませんでした。\n"
                "- 原因: Desktop Bridge が無効です。\n"
                "- このリクエストは、managed browser やローカル CSV ではなく、"
                "あなたが今開いているブラウザを対象にする必要があります。\n"
                "- 対応: DESKTOP_BRIDGE_ENABLED=true と DESKTOP_BRIDGE_URL を設定し、"
                "host 側で Desktop Bridge を起動してください。"
            )

        try:
            from src.bridges.desktop_bridge_client import get_desktop_client

            client = get_desktop_client()
            capability_result = await client.capabilities()
        except Exception as exc:
            return (
                "現在開いているブラウザや既存のスプレッドシートは操作できませんでした。\n"
                f"- 原因: Desktop Bridge capability check failed: {exc}\n"
                "- 対応: Desktop Bridge を起動し、host 側 runtime が正常応答することを確認してください。"
            )

        implemented = {
            capability.name
            for capability in capability_result.capabilities
            if capability.implemented
        }
        missing = sorted(_USER_BROWSER_REQUIRED_CAPABILITIES - implemented)
        if not missing:
            return None

        available = ", ".join(sorted(implemented)) or "(none)"
        required = ", ".join(sorted(_USER_BROWSER_REQUIRED_CAPABILITIES))
        missing_text = ", ".join(missing)
        return (
            "現在開いているブラウザや既存のスプレッドシートは操作できませんでした。\n"
            f"- 原因: Desktop Bridge に必要 capability が不足しています: {missing_text}\n"
            f"- 必要 capability: {required}\n"
            f"- 利用可能 capability: {available}\n"
            "- このリクエストは、managed browser やローカル CSV に置き換えず、"
            "現在のブラウザを対象にする必要があります。"
        )

    async def _emit_runner_tool_events(
        self,
        session_id: str,
        event: Event,
        *,
        fallback_request_id: str | None = None,
    ) -> None:
        for function_call in event.get_function_calls():
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name=function_call.name or "unknown_tool",
                    agent_name=event.author,
                    args=function_call.args or {},
                    request_id=function_call.id or fallback_request_id,
                ),
            )
        for function_response in event.get_function_responses():
            response = function_response.response or {}
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name=function_response.name or "unknown_tool",
                    agent_name=event.author,
                    ok="error" not in response,
                    result=self._summarize_tool_result(response),
                    request_id=function_response.id or fallback_request_id,
                ),
            )

    def _format_web_grounding(self, query: str, result: dict[str, Any]) -> str:
        lines = [f"web_search query: {query}"]
        meta = result.get("meta") or {}
        if meta:
            lines.append(
                f"timelimit={meta.get('timelimit', '')} region={meta.get('region', '')}"
            )
        entries = result.get("results") or []
        if not entries:
            lines.append(
                f"No results. message={result.get('message', 'no search results returned')}"
            )
            return "\n".join(lines)
        for index, item in enumerate(entries[:5], start=1):
            lines.append(f"{index}. {item.get('title', '')}")
            lines.append(f"   URL: {item.get('url', '')}")
            snippet = (item.get("snippet") or "").strip()
            if snippet:
                lines.append(f"   Snippet: {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _extract_grounding_block(message: str) -> str:
        marker = "[Grounding from web_search]\n"
        if marker not in message:
            return ""
        tail = message.split(marker, 1)[1]
        footer = (
            "\n\nUse the web_search grounding above as the primary evidence. "
            "If it is insufficient or contradictory, say so explicitly and avoid guessing."
        )
        if footer in tail:
            tail = tail.split(footer, 1)[0]
        return tail.strip()

    async def _compose_grounded_agent_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        *,
        research_message: str | None = None,
        agent_name: str = "",
        request_id: str | None = None,
        emit_tool_events: bool = False,
        allow_forced_research: bool = True,
    ) -> str:
        composed = self._compose_agent_message(session_id, message)
        search_query = research_message or message
        if not allow_forced_research or not self._should_force_web_research(search_query):
            return composed

        timelimit = self._select_web_search_timelimit(search_query)
        request_key = request_id or f"grounding:{session_id}"
        resolved_agent_name = agent_name or root_agent.name
        if emit_tool_events:
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name="web_search",
                    agent_name=resolved_agent_name,
                    args={
                        "query": search_query,
                        "timelimit": timelimit,
                        "region": "jp-jp",
                    },
                    request_id=request_key,
                ),
            )

        result = await web_search(
            query=search_query,
            timelimit=timelimit,
            region="jp-jp",
        )
        self.audit_logger.log(
            event_type=AuditEventType.WEB_SEARCH,
            user_id=user_id,
            session_id=session_id,
            action="search",
            resource=search_query,
            result="success" if result.get("results") else "empty",
            metadata={
                "timelimit": timelimit,
                "count": len(result.get("results") or []),
                "message": result.get("message", ""),
            },
        )
        if emit_tool_events:
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name="web_search",
                    agent_name=resolved_agent_name,
                    ok="error" not in result,
                    result=self._summarize_tool_result(result),
                    request_id=request_key,
                ),
            )

        grounding = self._format_web_grounding(search_query, result)
        return (
            f"{composed}\n\n"
            "[Grounding from web_search]\n"
            f"{grounding}\n\n"
            "Use the web_search grounding above as the primary evidence. "
            "If it is insufficient or contradictory, say so explicitly and avoid guessing."
        )

    async def _emit_routing_event(
        self,
        session_id: str,
        *,
        status: str,
        message: str,
        user_id: str,
        agent_name: str | None = None,
    ) -> None:
        await self._emit_session_event(
            session_id,
            source="router",
            status=status,
            message=message,
            user_id=user_id,
            agent_name=agent_name,
        )

    def _format_root_routing_message(
        self,
        original_message: str,
        decision: RoutingDecision,
        specialist_output: str | None = None,
        specialist_evidence: list[str] | None = None,
    ) -> str:
        lines = [
            "[Gateway routing]",
            f"Primary specialist: {decision.specialist or 'root_agent'}",
        ]
        if decision.reason:
            lines.append(f"Reason: {decision.reason}")
        lines.append(
            "You are still the root_agent. Use the routing context below to decide delegation and synthesis."
        )
        if specialist_evidence:
            lines.append(
                "If specialist evidence is provided, treat it as the primary factual source "
                "for this reply and restate the concrete facts in your answer."
            )
        lines.append(
            "Do not imply that forecast details were already shared unless you actually include "
            "those details in this response."
        )
        if specialist_output:
            lines.extend(
                [
                    "",
                    f"[Specialist output from {decision.specialist}]",
                    specialist_output.strip(),
                ]
            )
        for evidence in specialist_evidence or []:
            lines.extend(
                [
                    "",
                    f"[Specialist evidence from {decision.specialist}]",
                    evidence.strip(),
                ]
            )
        lines.extend(["", "[Original user request]", original_message])
        return "\n".join(lines)

    async def _run_specialist_prepass(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        specialist_name: str,
        request_id: str | None = None,
    ) -> SpecialistPrepassResult:
        runner = self.specialist_runners.get(specialist_name)
        if runner is None:
            return SpecialistPrepassResult()

        full_message = message
        if specialist_name == "web_researcher":
            full_message = await self._compose_grounded_agent_message(
                session_id,
                user_id,
                message,
                research_message=message,
                agent_name=specialist_name,
                request_id=request_id,
                emit_tool_events=True,
                allow_forced_research=True,
            )
        content = types.Content(role="user", parts=[types.Part(text=full_message)])
        partial = ""
        result = SpecialistPrepassResult()
        if specialist_name == "web_researcher":
            grounding = self._extract_grounding_block(full_message)
            if grounding:
                result.evidence_blocks.append(grounding)
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            await self._emit_runner_tool_events(
                session_id,
                event,
                fallback_request_id=request_id,
            )
            for function_call in event.get_function_calls():
                if function_call.name:
                    result.used_tools.add(function_call.name)
            for function_response in event.get_function_responses():
                if function_response.name:
                    result.used_tools.add(function_response.name)
                if function_response.name == "web_search":
                    response = function_response.response or {}
                    query = str(response.get("query") or message).strip()
                    grounding = self._format_web_grounding(query, response)
                    if grounding and grounding not in result.evidence_blocks:
                        result.evidence_blocks.append(grounding)
                error = self._tool_response_error(function_response.response or {})
                if not error:
                    continue
                result.tool_failures.append(
                    SpecialistToolFailure(
                        tool_name=function_response.name or "unknown_tool",
                        error=error,
                        infrastructure=self._is_browser_infrastructure_error(
                            function_response.name or "",
                            error,
                        ),
                    )
                )
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        partial += part.text
        result.text = partial.strip()
        return result

    def _routing_history_block(self, session_id: str, limit: int = 8) -> str:
        lines: list[str] = []
        for entry in self.transcript.get_history(session_id, limit=limit):
            if entry.role not in {"user", "assistant", "inject", "system"}:
                continue
            content = (entry.content or "").strip()
            if not content:
                continue
            lines.append(f"{entry.role}: {content[:280]}")
        return "\n".join(lines) if lines else "(empty)"

    def _build_routing_request(
        self,
        *,
        session_id: str,
        source: str,
        message: str,
        explicit_target: str | None = None,
    ) -> str:
        override = explicit_target or "auto"
        history_block = self._routing_history_block(session_id)
        return (
            f"source={source}\n"
            f"explicit_target={override}\n\n"
            "[Recent transcript]\n"
            f"{history_block}\n\n"
            "[Current request]\n"
            f"{message}\n"
        )

    @staticmethod
    def _extract_json_payload(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None

    async def _select_route_for_message(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        source: str,
        explicit_target: str | None = None,
    ) -> RoutingDecision:
        prompt = self._build_routing_request(
            session_id=session_id,
            source=source,
            message=message,
            explicit_target=explicit_target,
        )
        routing_session = await self.routing_session_service.create_session(
            app_name="boiled-claw-router",
            user_id=user_id,
            session_id=f"route_{uuid.uuid4().hex[:12]}",
        )
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        raw_response = ""

        try:
            async with asyncio.timeout(20):
                async for event in self.routing_runner.run_async(
                    user_id=user_id,
                    session_id=routing_session.id,
                    new_message=content,
                ):
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                raw_response += part.text

            payload = self._extract_json_payload(raw_response)
            if payload is None:
                raise ValueError("routing_agent returned non-JSON output")

            decision = decision_from_payload(payload, fallback_message=message)
            if decision.confidence < 0.35:
                raise ValueError("routing_agent confidence too low")
            return decision
        except Exception as exc:
            fallback = heuristic_decision(message)
            self.audit_logger.log(
                event_type=AuditEventType.AGENT_MESSAGE,
                user_id=user_id,
                session_id=session_id,
                action="routing_fallback",
                resource="routing_agent",
                result="fallback",
                metadata={
                    "error": str(exc),
                    "fallback_target": fallback.route_label,
                },
            )
            return fallback

    @staticmethod
    def _default_dynamic_instruction(message: str) -> str:
        return (
            "You are a dedicated dynamic agent created for a single user task.\n"
            "Work only on the assigned task, stay within the provided tools, and "
            "return concise status and results.\n\n"
            f"Assigned task:\n{message}"
        )

    async def _spawn_dynamic_route(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        decision: RoutingDecision,
    ) -> dict[str, Any]:
        dynamic_request = decision.dynamic_agent
        instruction = (
            dynamic_request.instruction.strip()
            or self._default_dynamic_instruction(message)
        )
        result = await self.subagent_manager.spawn_dynamic(
            task=message,
            instruction=instruction,
            mcp_servers=dynamic_request.mcp_servers,
            requester_session_id=session_id,
            user_id=user_id,
            app_name="boiled-claw",
            mode=dynamic_request.mode or "run",
        )
        return result

    async def _spawn_cron_target(
        self,
        *,
        task: str,
        agent_name: str,
        requester_session_id: str,
        user_id: str,
        app_name: str,
        mode: str = "run",
    ) -> dict[str, Any]:
        if agent_name != "auto":
            return await self.subagent_manager.spawn(
                task=task,
                agent_name=agent_name,
                requester_session_id=requester_session_id,
                user_id=user_id,
                app_name=app_name,
                mode=mode,
            )

        decision = await self._select_route_for_message(
            session_id=requester_session_id,
            user_id=user_id,
            message=task,
            source="cron",
            explicit_target="auto",
        )

        if decision.target == "specialist" and decision.specialist:
            return await self.subagent_manager.spawn(
                task=task,
                agent_name=decision.specialist,
                requester_session_id=requester_session_id,
                user_id=user_id,
                app_name=app_name,
                mode=mode,
            )

        if decision.target == "dynamic_agent":
            return await self._spawn_dynamic_route(
                session_id=requester_session_id,
                user_id=user_id,
                message=task,
                decision=decision,
            )

        run_id = f"cronrt_{uuid.uuid4().hex[:12]}"
        if decision.target == "control_loop":
            asyncio.create_task(
                self._cron_control_loop_task(
                    run_id=run_id,
                    session_id=requester_session_id,
                    user_id=user_id,
                    goal=task,
                ),
                name=f"cron-control:{run_id}",
            )
            return {
                "status": "accepted",
                "run_id": run_id,
                "agent_name": "control_loop",
                "mode": mode,
                "requester_session_id": requester_session_id,
            }

        asyncio.create_task(
            self._cron_root_agent_task(
                run_id=run_id,
                session_id=requester_session_id,
                user_id=user_id,
                message=task,
            ),
            name=f"cron-root:{run_id}",
        )
        return {
            "status": "accepted",
            "run_id": run_id,
            "agent_name": "root_agent",
            "mode": mode,
            "requester_session_id": requester_session_id,
        }

    async def _cron_root_agent_task(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        message: str,
    ) -> None:
        result = await self._run_agent_http(user_id, session_id, message)
        await self._deliver_background_result(
            session_id=session_id,
            user_id=user_id,
            source="cron",
            run_id=run_id,
            agent_name="root_agent",
            message=result.get("message", ""),
            ok=bool(result.get("ok")),
            metadata={"type": result.get("type", "agent_message")},
        )

    async def _cron_control_loop_task(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        goal: str,
    ) -> None:
        result = await self._run_control_loop_http(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=[],
            source="cron",
        )
        await self._deliver_background_result(
            session_id=session_id,
            user_id=user_id,
            source="cron",
            run_id=run_id,
            agent_name="control_loop",
            message=result.final_text,
            ok=result.success,
            metadata={
                "type": "control_loop",
                "plan_id": result.plan_id,
                "task_id": result.metadata.get("task_id"),
            },
        )

    async def _deliver_background_result(
        self,
        *,
        session_id: str,
        user_id: str,
        source: str,
        run_id: str,
        agent_name: str,
        message: str,
        ok: bool,
        metadata: dict[str, Any],
    ) -> None:
        session = self.transcript.get_session(session_id)
        owner_id = session.user_id if session is not None else user_id
        if session is not None and message.strip():
            self.transcript.append(
                session_id,
                "assistant",
                message,
                user_id=owner_id,
                metadata=metadata,
            )
        await self._emit_session_event(
            session_id,
            source=source,
            status="completed" if ok else "failed",
            message=message,
            user_id=owner_id,
            run_id=run_id,
            agent_name=agent_name,
        )

    def _related_approvals_for_task(
        self,
        *,
        approval_ids: list[str],
        session_id: Optional[str],
    ) -> list[dict[str, Any]]:
        if not approval_ids:
            return []
        lookup = {item for item in approval_ids if item}
        approvals = self.tool_policy.list_approvals(
            session_id=session_id,
            state="all",
            include_expired=True,
            limit=max(100, len(lookup) * 4),
        )
        related: list[dict[str, Any]] = []
        seen: set[str] = set()
        for approval in approvals:
            request_id = str(approval.get("request_id") or "")
            source_request_id = str(approval.get("source_request_id") or "")
            if request_id not in lookup and source_request_id not in lookup:
                continue
            if request_id in seen:
                continue
            seen.add(request_id)
            related.append(approval)
        return related

    @staticmethod
    def _approval_is_desktop_tool(tool_name: str) -> bool:
        return str(tool_name or "").startswith("desktop_")

    @staticmethod
    def _approval_family_pattern(tool_name: str) -> str:
        normalized = str(tool_name or "").strip()
        if normalized.startswith("desktop_ax_"):
            return "desktop_ax_*"
        if normalized.startswith("desktop_view_"):
            return "desktop_view_*"
        if normalized.startswith("desktop_wait_"):
            return "desktop_wait_*"
        if normalized.startswith("desktop_control_"):
            return "desktop_control_*"
        return normalized

    @staticmethod
    def _approval_family_label(tool_name: str) -> str:
        pattern = GatewayServer._approval_family_pattern(tool_name)
        labels = {
            "desktop_ax_*": "Desktop AX Family",
            "desktop_view_*": "Desktop View Family",
            "desktop_wait_*": "Desktop Wait Family",
            "desktop_control_*": "Desktop Control Family",
        }
        return labels.get(pattern, str(tool_name or "Tool"))

    def _session_pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        result = self.tool_policy.query_approvals(
            session_id=session_id,
            state="pending",
            include_expired=False,
            page=1,
            page_size=200,
        )
        approvals = result.get("approvals")
        return approvals if isinstance(approvals, list) else []

    def _approval_resolve_suggestions(self, approval: dict[str, Any]) -> list[dict[str, Any]]:
        if str(approval.get("state") or "") not in {"pending", "expiring"}:
            return []
        session_id = str(approval.get("session_id") or "")
        tool_name = str(approval.get("tool_name") or approval.get("tool_pattern") or "")
        if not session_id or not tool_name:
            return []
        pending = self._session_pending_approvals(session_id)
        family_pattern = self._approval_family_pattern(tool_name)
        desktop_pending = [
            item for item in pending if self._approval_is_desktop_tool(item.get("tool_name") or "")
        ]
        suggestions = [
            {
                "strategy": "session_exact",
                "label": "Approve This Tool For Session",
                "description": "Reuse this exact tool approval for later requests in the same session.",
                "affected_count": max(
                    1,
                    sum(
                        1
                        for item in pending
                        if str(item.get("tool_name") or "") == tool_name
                    ),
                ),
                "tool_pattern": tool_name,
                "scope": "session",
            }
        ]
        if family_pattern and family_pattern != tool_name:
            suggestions.append(
                {
                    "strategy": "family_session",
                    "label": f"Approve {self._approval_family_label(tool_name)}",
                    "description": "Reuse a family-scoped approval for similar desktop capabilities in this session.",
                    "affected_count": max(
                        1,
                        sum(
                            1
                            for item in pending
                            if self._approval_family_pattern(item.get("tool_name") or "") == family_pattern
                        ),
                    ),
                    "tool_pattern": family_pattern,
                    "scope": "session",
                }
            )
        if self._approval_is_desktop_tool(tool_name) and desktop_pending:
            suggestions.append(
                {
                    "strategy": "desktop_session_pack",
                    "label": "Approve Desktop Pack For Session",
                    "description": "Resolve all currently-pending desktop approvals in this session using family-scoped rules.",
                    "affected_count": len(desktop_pending),
                    "tool_pattern": "desktop::*",
                    "scope": "session",
                }
            )
        return suggestions

    def _approval_bundle_specs(
        self,
        approval: dict[str, Any],
        *,
        strategy: str,
        path_scope: Optional[str] = None,
        propagate_to_subagents: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        request_id = str(approval.get("request_id") or "")
        session_id = str(approval.get("session_id") or "")
        tool_name = str(approval.get("tool_name") or approval.get("tool_pattern") or "")
        if not request_id or not session_id or not tool_name:
            return []

        pending = self._session_pending_approvals(session_id)
        family_pattern = self._approval_family_pattern(tool_name)

        def build_item(item: dict[str, Any], tool_pattern_value: str) -> dict[str, Any]:
            return {
                "request_id": str(item.get("request_id") or ""),
                "scope": "session",
                "tool_pattern": tool_pattern_value,
                "path_scope": path_scope if path_scope is not None else item.get("path_scope"),
                "propagate_to_subagents": (
                    bool(propagate_to_subagents)
                    if propagate_to_subagents is not None
                    else bool(item.get("propagate_to_subagents"))
                ),
            }

        if strategy == "single":
            return [{"request_id": request_id, "scope": approval.get("scope") or "single"}]
        if strategy == "session_exact":
            return [
                build_item(item, tool_name)
                for item in pending
                if str(item.get("tool_name") or "") == tool_name
            ] or [build_item(approval, tool_name)]
        if strategy == "family_session":
            return [
                build_item(item, self._approval_family_pattern(item.get("tool_name") or ""))
                for item in pending
                if self._approval_family_pattern(item.get("tool_name") or "") == family_pattern
            ] or [build_item(approval, family_pattern)]
        if strategy == "desktop_session_pack":
            if not self._approval_is_desktop_tool(tool_name):
                raise ValueError("desktop_session_pack is only available for desktop approvals")
            return [
                build_item(item, self._approval_family_pattern(item.get("tool_name") or ""))
                for item in pending
                if self._approval_is_desktop_tool(item.get("tool_name") or "")
            ] or [build_item(approval, family_pattern)]
        raise ValueError(f"unsupported approval bundle strategy: {strategy}")

    def _control_loop_seed_payload(
        self,
        *,
        goal: str,
        constraints: list[str],
        source: str,
        request_id: Optional[str],
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        replay_from_step: Optional[str] = None,
        replay_mode: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        artifacts: dict[str, Any] = {
            "goal": goal,
            "constraints": constraints,
            "resume_context": {
                "goal": goal,
                "constraints": constraints,
            },
        }
        metadata: dict[str, Any] = {
            "source": source,
            "request_id": request_id,
        }
        if replay_of_task_id:
            artifacts["replay"] = {
                "source_task_id": replay_of_task_id,
                "compare_to_task_id": compare_to_task_id or replay_of_task_id,
            }
            artifacts["resume_context"]["replay_of_task_id"] = replay_of_task_id
            metadata["replay_of_task_id"] = replay_of_task_id
            metadata["compare_to_task_id"] = compare_to_task_id or replay_of_task_id
            if replay_from_step:
                artifacts["replay"]["from_step"] = replay_from_step
                artifacts["resume_context"]["replay_from_step"] = replay_from_step
                metadata["replay_from_step"] = replay_from_step
            if replay_mode:
                artifacts["replay"]["mode"] = replay_mode
                artifacts["resume_context"]["replay_mode"] = replay_mode
                metadata["replay_mode"] = replay_mode
        return artifacts, metadata

    def _create_control_loop_task_record(
        self,
        *,
        user_id: str,
        session_id: str,
        owner_session_id: Optional[str] = None,
        goal: str,
        constraints: list[str],
        request_id: Optional[str],
        source: str,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        replay_from_step: Optional[str] = None,
        replay_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        artifacts, metadata = self._control_loop_seed_payload(
            goal=goal,
            constraints=constraints,
            source=source,
            request_id=request_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            replay_from_step=replay_from_step,
            replay_mode=replay_mode,
        )
        return create_task_record(
            kind="control_loop",
            title=goal,
            status="running",
            owner_session_id=owner_session_id or session_id,
            owner_user_id=user_id,
            parent_task_id=parent_task_id,
            artifacts=artifacts,
            metadata=metadata,
        )

    def _find_control_loop_task_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> str | None:
        task = self._find_control_loop_task_record_for_approval(
            session_id=session_id,
            request_id=request_id,
        )
        if not isinstance(task, dict):
            return None
        task_id = str(task.get("task_id") or "").strip()
        return task_id or None

    def _find_control_loop_task_owner_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> str | None:
        task = self._find_control_loop_task_record_for_approval(
            session_id=session_id,
            request_id=request_id,
        )
        if not isinstance(task, dict):
            return None
        owner_user_id = str(task.get("owner_user_id") or "").strip()
        return owner_user_id or None

    def _find_control_loop_task_record_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        request_id = str(request_id or "").strip()
        if not session_id or not request_id:
            return None
        payload = self.task_store.query(
            owner_session_id=session_id,
            kind="control_loop",
            status="open",
            page=1,
            page_size=100,
        )
        for task in payload.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            artifacts = task.get("artifacts") or {}
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            result = artifacts.get("result") or {}
            result = result if isinstance(result, dict) else {}
            resume_context = artifacts.get("resume_context") or {}
            resume_context = resume_context if isinstance(resume_context, dict) else {}
            candidates = [
                ((result.get("approval_request") or {}) if isinstance(result.get("approval_request"), dict) else {}).get("request_id"),
                ((resume_context.get("approval_request") or {}) if isinstance(resume_context.get("approval_request"), dict) else {}).get("request_id"),
            ]
            if any(str(candidate or "").strip() == request_id for candidate in candidates):
                return task
        return None

    @staticmethod
    def _task_timeline_sort_key(entry: dict[str, Any]) -> tuple[float, str]:
        return (float(entry.get("timestamp") or 0.0), str(entry.get("timeline_id") or ""))

    def _build_task_timeline_payload(
        self,
        task: dict[str, Any],
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 50), 200))
        history_limit = min(max(resolved_page * resolved_page_size * 4, 100), 500)
        task_history = self.task_store.query_timeline(
            task["task_id"],
            page=1,
            page_size=history_limit,
        )
        approvals = self._related_approvals_for_task(
            approval_ids=list(task.get("approval_dependencies") or []),
            session_id=task.get("owner_session_id"),
        )
        audit_entries = self.audit_logger.query_related(
            session_id=task.get("owner_session_id"),
            task_id=task.get("task_id"),
            run_id=task.get("run_id"),
            request_ids=list(task.get("approval_dependencies") or []),
            limit=history_limit,
        )

        timeline_entries: list[dict[str, Any]] = []
        for event in task_history.get("events") or []:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") or {}
            payload = payload if isinstance(payload, dict) else {}
            step_payload = payload.get("step")
            step_payload = step_payload if isinstance(step_payload, dict) else {}
            summary = (
                str(payload.get("summary") or "").strip()
                or str(step_payload.get("output_summary") or "").strip()
                or str(event.get("event_type") or "updated")
            )
            timeline_entries.append(
                {
                    "timeline_id": str(event.get("entry_id") or ""),
                    "kind": "task_event",
                    "timestamp": float(event.get("timestamp") or 0.0),
                    "title": str(
                        step_payload.get("title")
                        or event.get("title")
                        or task.get("title")
                        or "task"
                    ),
                    "status": str(
                        step_payload.get("status")
                        or event.get("status")
                        or task.get("status")
                        or ""
                    ),
                    "event_type": str(event.get("event_type") or "updated"),
                    "summary": summary,
                    "task_id": task.get("task_id"),
                    "payload": payload,
                    "task_event": event,
                }
            )

        for approval in approvals:
            history = approval.get("history")
            history = history if isinstance(history, list) else []
            for index, history_entry in enumerate(history):
                if not isinstance(history_entry, dict):
                    continue
                state = str(history_entry.get("state") or approval.get("state") or "pending")
                reason = str(history_entry.get("reason") or "").strip()
                summary = f"{state}: {approval.get('tool_name') or approval.get('tool_pattern') or approval.get('request_id') or 'approval'}"
                if reason:
                    summary = f"{summary} — {reason}"
                timeline_entries.append(
                    {
                        "timeline_id": f"approval-{approval.get('request_id')}-{index}",
                        "kind": "approval",
                        "timestamp": float(history_entry.get("ts") or approval.get("created_at") or 0.0),
                        "title": str(approval.get("tool_name") or approval.get("tool_pattern") or "approval"),
                        "status": state,
                        "event_type": state,
                        "summary": summary,
                        "request_id": approval.get("request_id"),
                        "source_request_id": approval.get("source_request_id"),
                        "approval": approval,
                        "history_entry": history_entry,
                    }
                )

        for entry in audit_entries:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            title = str(entry.get("event_type") or entry.get("action") or "audit")
            summary_parts = [
                str(entry.get("action") or "").strip(),
                str(entry.get("resource") or "").strip(),
                str(metadata.get("resolve_reason") or "").strip(),
            ]
            summary = " · ".join(part for part in summary_parts if part) or title
            timeline_entries.append(
                {
                    "timeline_id": str(entry.get("entry_id") or ""),
                    "kind": "audit",
                    "timestamp": float(entry.get("timestamp") or 0.0),
                    "title": title,
                    "status": str(entry.get("result") or entry.get("event_type") or ""),
                    "event_type": str(entry.get("event_type") or ""),
                    "summary": summary,
                    "audit_entry_id": entry.get("entry_id"),
                    "audit_focus": {
                        "entryId": entry.get("entry_id"),
                        "requestId": metadata.get("request_id") or metadata.get("source_request_id") or entry.get("resource"),
                        "taskId": metadata.get("task_id") or task.get("task_id"),
                        "runId": metadata.get("run_id") or task.get("run_id"),
                        "sessionId": entry.get("session_id") or metadata.get("target_session_id") or task.get("owner_session_id"),
                        "toolName": metadata.get("tool_name") or metadata.get("tool_pattern"),
                        "source": metadata.get("source"),
                        "result": entry.get("result"),
                    },
                    "entry": entry,
                }
            )

        timeline_entries.sort(key=self._task_timeline_sort_key, reverse=True)
        offset = (resolved_page - 1) * resolved_page_size
        page_entries = timeline_entries[offset:offset + resolved_page_size]
        return {
            "task": task,
            "entries": page_entries,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": len(timeline_entries),
                "has_more": offset + len(page_entries) < len(timeline_entries),
            },
        }

    def _shared_api_key_principal(self) -> str:
        api_key = self.settings.gateway_api_key or ""
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        return f"gateway_api:{digest}"

    def _resolve_effective_user_id(
        self,
        requested_user_id: Optional[str],
        *,
        headers: Mapping[str, str],
        default_user_id: str,
    ) -> str:
        requested = (requested_user_id or "").strip()
        if not self.settings.gateway_api_key:
            return requested or default_user_id

        trusted_header = (
            getattr(self.settings, "gateway_auth_user_header", None) or ""
        ).strip()
        if trusted_header:
            authenticated_user_id = (headers.get(trusted_header) or "").strip()
            if not authenticated_user_id:
                raise HTTPException(
                    status_code=401,
                    detail=f"Missing authenticated user header: {trusted_header}",
                )
            return authenticated_user_id

        # Shared API key mode has a single authenticated principal, so caller-supplied
        # user_id values must not affect transcript ownership checks.
        return self._shared_api_key_principal()

    def _resolve_http_user_id(
        self,
        request: Request,
        requested_user_id: Optional[str],
        *,
        default_user_id: str,
    ) -> str:
        return self._resolve_effective_user_id(
            requested_user_id,
            headers=request.headers,
            default_user_id=default_user_id,
        )

    def _resolve_websocket_user_id(
        self,
        websocket: WebSocket,
        requested_user_id: Optional[str],
        *,
        default_user_id: str,
    ) -> str:
        return self._resolve_effective_user_id(
            requested_user_id,
            headers=websocket.headers,
            default_user_id=default_user_id,
        )

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    def _setup_routes(self):
        # --- health / root / protocol ---

        @self.app.get("/")
        async def root():
            return {
                "name": "boiled-claw Gateway",
                "version": "0.3.0",
                "protocol_version": PROTOCOL_VERSION,
                "status": "running",
                "session_backend": self.session_backend["backend"],
                "session_namespace": self.session_backend["namespace"],
                "active_sessions": len(self.manager.active_connections),
                "skills_loaded": get_skills_report().get("loaded", False),
                "skills_count": get_skills_report().get("count", 0),
            }

        @self.app.get("/health")
        async def health():
            return {
                "status": "healthy",
                "session_backend": self.session_backend["backend"],
                "session_namespace": self.session_backend["namespace"],
            }

        @self.app.get("/protocol")
        async def protocol_info():
            return {
                "version": PROTOCOL_VERSION,
                "events": list(EVENT_SCHEMAS.keys()),
                "schemas": EVENT_SCHEMAS,
                "http_surfaces": HTTP_ROUTE_SCHEMAS,
                "runtime_substrate": RUNTIME_SUBSTRATE_SCHEMA,
            }

        # --- skills ---

        @self.app.get("/skills")
        async def skills():
            await ensure_skills_loaded()
            detail = await tool_skill_list()
            report = get_skills_report()
            return {**report, "details": detail.get("skills", [])}

        @self.app.post("/skills/{skill_name}/execute")
        async def execute_skill(skill_name: str, payload: Dict[str, Any] | None = Body(default=None)):
            params = {}
            if payload and isinstance(payload.get("params"), dict):
                params = payload.get("params", {})
            result = await tool_skill_execute(skill_name, json.dumps(params, ensure_ascii=False))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "Skill execution failed"))
            return result

        # --- runtime substrate ---

        @self.app.get("/runtime/resources")
        async def runtime_resources():
            return await tool_resource_list()

        @self.app.get("/runtime/resources/{resource_id:path}")
        async def runtime_resource(resource_id: str, refresh: bool = Query(default=False)):
            result = await tool_resource_read(resource_id, refresh=refresh)
            if not result.get("ok"):
                raise HTTPException(status_code=404, detail=result.get("message", "Resource not found"))
            return result

        @self.app.get("/runtime/capabilities")
        async def runtime_capabilities(refresh: bool = Query(default=False)):
            return await tool_capability_list(refresh=refresh)

        @self.app.post("/runtime/capabilities/invoke")
        async def runtime_capability_invoke(payload: Dict[str, Any] | None = Body(default=None)):
            if not payload or not isinstance(payload.get("name"), str) or not payload.get("name"):
                raise HTTPException(status_code=400, detail="name is required")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise HTTPException(status_code=400, detail="params must be an object")
            result = await tool_capability_invoke(
                payload["name"],
                json.dumps(params, ensure_ascii=False),
            )
            if not result.get("success") and str(result.get("error", "")).startswith("Unknown capability:"):
                raise HTTPException(status_code=400, detail=result["error"])
            if not result.get("success") and "requires tool_context-backed approval flow" in str(
                result.get("error", "")
            ):
                raise HTTPException(status_code=403, detail=result["error"])
            return result

        # --- sessions ---

        @self.app.get("/sessions/{user_id}")
        async def list_sessions(user_id: str, request: Request):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            sessions = self.transcript.list_sessions(user_id=effective_user_id)
            if not sessions:
                response = await self.session_service.list_sessions(
                    app_name="boiled-claw", user_id=effective_user_id
                )
                hydrated = response.sessions or []
                return {
                    "sessions": [
                        {
                            "id": s.id,
                            "user_id": effective_user_id,
                            "last_activity": getattr(s, "last_update_time", 0.0),
                            "preview": "",
                            "entry_count": len(getattr(s, "events", []) or []),
                        }
                        for s in hydrated
                    ]
                }

            return {
                "sessions": [
                    {
                        "id": item["session_id"],
                        "user_id": item["user_id"],
                        "last_activity": item["last_activity"],
                        "preview": item["preview"],
                        "entry_count": item["entry_count"],
                    }
                    for item in sessions
                ]
            }

        # --- transcript / history ---

        @self.app.get("/sessions/{user_id}/{session_id}/history")
        async def get_session_history(
            request: Request,
            user_id: str,
            session_id: str,
            limit: int = Query(default=100, ge=1, le=500),
            before: Optional[float] = Query(default=None),
        ):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            if not self.transcript.has_session(session_id, effective_user_id):
                raise HTTPException(status_code=404, detail="session not found")
            entries = self.transcript.get_history(session_id, limit=limit, before=before)
            return {
                "session_id": session_id,
                "entries": [e.to_dict() for e in entries],
                "count": len(entries),
            }

        @self.app.get("/transcript/sessions")
        async def list_transcript_sessions(
            request: Request,
            user_id: str = Query(...),
            limit: int = Query(default=50, ge=1, le=200),
        ):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            return {
                "sessions": self.transcript.list_sessions(
                    user_id=effective_user_id,
                    limit=limit,
                )
            }

        # --- agent/run (HTTP) ---

        @self.app.post("/agent/run")
        async def run_agent_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            message = str(body.get("message") or "").strip()
            session_id = body.get("session_id")

            if not message:
                raise HTTPException(status_code=400, detail="message is required")

            session = await self._get_or_create_gateway_session(
                user_id=user_id,
                session_id=str(session_id) if session_id else None,
            )

            # Record user message in transcript
            self.transcript.append(session.id, "user", message, user_id=user_id)

            result = await self._run_agent_http(user_id, session.id, message)

            # Record assistant response in transcript
            self.transcript.append(
                session.id, "assistant", result["message"],
                user_id=user_id,
                metadata={"type": result["type"]},
            )

            return {
                "ok": result.get("ok", result["type"] == "agent_message"),
                "type": result["type"],
                "response": result["message"],
                "user_id": user_id,
                "session_id": session.id,
            }

        @self.app.post("/control-loop/run")
        async def run_control_loop_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            goal = str(body.get("goal") or "").strip()
            session_id = body.get("session_id")
            constraints = normalize_constraints(body.get("constraints"))

            if not goal:
                raise HTTPException(status_code=400, detail="goal is required")

            session = await self._get_or_create_gateway_session(
                user_id=user_id,
                session_id=str(session_id) if session_id else None,
            )
            self.transcript.append(
                session.id,
                "user",
                goal,
                user_id=user_id,
                metadata={"type": "control_loop", "constraints": constraints},
            )

            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session.id,
                goal=goal,
                constraints=constraints,
                source="http",
                reset_if_terminal=True,
            )
            self.transcript.append(
                session.id,
                "assistant",
                result.final_text,
                user_id=user_id,
                metadata={
                    "type": "control_loop",
                    "success": result.success,
                    "needs_human": bool(result.metadata.get("needs_human")),
                    "plan_id": result.plan_id,
                    "task_id": result.metadata.get("task_id"),
                },
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session.id,
                    result.metadata.get("approval_request"),
                )

            return {
                "ok": result.success,
                "response": result.final_text,
                "user_id": user_id,
                "session_id": session.id,
                "plan_id": result.plan_id,
                "verification_report_id": result.verification_report_id,
                "repair_count": result.repair_count,
                "task_id": result.metadata.get("task_id"),
                "needs_human": bool(result.metadata.get("needs_human")),
                "approval_request": result.metadata.get("approval_request"),
                "promoted_memory_ids": result.promoted_memory_ids,
            }

        @self.app.post("/control-loop/approve")
        async def approve_control_loop_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            session_id = str(body.get("session_id") or "").strip()
            request_id = str(body.get("request_id") or "").strip()
            approved = bool(body.get("approved", False))

            if not session_id or not request_id:
                raise HTTPException(
                    status_code=400,
                    detail="session_id and request_id are required",
                )

            pending = await self.control_loop.get_pending_approval(
                user_id=user_id,
                session_id=session_id,
            )
            pending_task_id = self._find_control_loop_task_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            approval_owner_user_id = self._find_control_loop_task_owner_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            resolved = await self.control_loop.resolve_human_approval(
                user_id=user_id,
                session_id=session_id,
                approved=approved,
                request_id=request_id,
            )
            if (
                not pending
                and not resolved
                and approval_owner_user_id
                and approval_owner_user_id != user_id
            ):
                user_id = approval_owner_user_id
                pending = await self.control_loop.get_pending_approval(
                    user_id=user_id,
                    session_id=session_id,
                )
                resolved = await self.control_loop.resolve_human_approval(
                    user_id=user_id,
                    session_id=session_id,
                    approved=approved,
                    request_id=request_id,
                )
            if not resolved:
                raise HTTPException(status_code=404, detail="approval request not found")

            resumed_result = None
            if approved and pending:
                resume_goal = (
                    await self.control_loop.get_task_goal(
                        user_id=user_id,
                        session_id=session_id,
                    )
                    or pending.get("goal", "")
                )
                await self._start_control_loop_run(
                    user_id=user_id,
                    session_id=session_id,
                    goal=resume_goal,
                    constraints=normalize_constraints(
                        (pending.get("plan") or {}).get("constraints")
                    ),
                    task_id=pending_task_id,
                )

            response = {
                "ok": True,
                "session_id": session_id,
                "request_id": request_id,
                "approved": approved,
            }
            if approved and pending:
                response["result"] = {
                    "ok": True,
                    "response": "Approval accepted. Control loop resumed in background.",
                    "task_id": pending_task_id,
                    "needs_human": False,
                }
            return response

        # --- memory ---

        @self.app.get("/memory/stats")
        async def memory_stats_endpoint():
            return await memory_stats()

        @self.app.get("/memory")
        async def memory_search_endpoint(
            query: Optional[str] = None,
            tags: Optional[str] = None,
            limit: int = 10,
        ):
            return await memory_search(query=query, tags=tags, limit=limit)

        @self.app.delete("/memory/{memory_id}")
        async def memory_delete_endpoint(memory_id: int):
            result = await memory_delete(memory_id)
            if not result.get("success"):
                raise HTTPException(status_code=500, detail=result.get("error", "Delete failed"))
            if not result.get("deleted"):
                raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
            return result

        # --- subagents ---

        @self.app.get("/subagents/{session_id}")
        async def subagents_list_endpoint(session_id: str):
            return await self.subagent_manager.list_runs(requester_session_id=session_id)

        self.app.include_router(build_task_router(self))
        self.app.include_router(build_audit_router(self))

        @self.app.post("/subagents/{run_id}/steer")
        async def subagents_steer_endpoint(run_id: str, payload: Dict[str, Any] | None = Body(default=None)):
            message = ""
            if payload and isinstance(payload.get("message"), str):
                message = payload.get("message", "").strip()
            if not message:
                raise HTTPException(status_code=400, detail="message is required")
            result = await self.subagent_manager.steer(run_id=run_id, message=message)
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=result.get("error", "steer failed"))
            return result

        @self.app.delete("/subagents/{run_id}")
        async def subagents_kill_endpoint(run_id: str):
            result = await self.subagent_manager.kill(run_id=run_id)
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=result.get("error", "kill failed"))
            return result

        # --- cron ---

        @self.app.get("/cron")
        async def cron_list():
            return {"jobs": [j.to_dict() for j in get_scheduler().list_jobs()]}

        @self.app.post("/cron")
        async def cron_create(payload: Dict[str, Any] | None = Body(default=None)):
            body = payload or {}
            try:
                job = get_scheduler().add_job(
                    name=str(body.get("name") or ""),
                    cron_expr=str(body.get("cron_expr") or ""),
                    task=str(body.get("task") or ""),
                    agent_id=str(body.get("agent_id") or "web_researcher"),
                    delivery_target=self._resolve_cron_delivery_target(body),
                    max_retries=int(body.get("max_retries") or 0),
                    retry_delay=int(body.get("retry_delay") or 30),
                    system_event=body.get("system_event") or None,
                )
                return {"ok": True, "job": job.to_dict()}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        @self.app.delete("/cron/{job_id}")
        async def cron_delete(job_id: str):
            if not get_scheduler().delete_job(job_id):
                raise HTTPException(status_code=404, detail="job not found")
            return {"ok": True}

        @self.app.patch("/cron/{job_id}")
        async def cron_toggle(job_id: str, payload: Dict[str, Any] | None = Body(default=None)):
            body = payload or {}
            enabled = bool(body.get("enabled", True))
            job = get_scheduler().toggle_job(job_id, enabled)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            return {"ok": True, "job": job.to_dict()}

        # --- tool policy ---

        @self.app.get("/tools/policy")
        async def tool_policy_list():
            return self.tool_policy.list_policies()

        @self.app.get("/tools/approvals")
        async def tool_approvals_list(
            session_id: Optional[str] = None,
            state: Optional[str] = None,
            include_expired: bool = False,
            q: Optional[str] = None,
            page: int = 1,
            page_size: Optional[int] = None,
            limit: Optional[int] = None,
        ):
            selected_state = state or "pending"
            resolved_page_size = max(1, min(int(page_size or limit or 20), 100))
            return self.tool_policy.query_approvals(
                session_id=session_id,
                state=selected_state,
                include_expired=include_expired,
                q=q,
                page=page,
                page_size=resolved_page_size,
            )

        @self.app.get("/tools/approvals/{request_id}")
        async def tool_approval_get(request_id: str):
            approval = self.tool_policy.get_approval(request_id)
            if approval is None:
                raise HTTPException(status_code=404, detail=f"approval not found: {request_id}")
            return {
                "approval": approval,
                "resolve_suggestions": self._approval_resolve_suggestions(approval),
            }

        @self.app.post("/tools/approvals/{request_id}/resolve")
        async def tool_approval_resolve(
            request: Request,
            request_id: str,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            if "approved" not in body:
                raise HTTPException(status_code=400, detail="approved is required")
            approved = bool(body.get("approved"))
            reason = str(body.get("reason") or "").strip()
            session_id = str(body.get("session_id") or "")
            user_id = self._resolve_http_user_id(
                request,
                str(body.get("user_id") or "api_user"),
                default_user_id="api_user",
            )
            result = await self._resolve_tool_approval_request(
                request_id=request_id,
                approved=approved,
                reason=reason,
                session_id=session_id,
                user_id=user_id,
                source="http",
                scope=body.get("scope"),
                tool_pattern=body.get("tool_pattern"),
                path_scope=body.get("path_scope"),
                expires_at=body.get("expires_at"),
                propagate_to_subagents=body.get("propagate_to_subagents"),
            )
            if not result.get("resolved"):
                raise HTTPException(status_code=404, detail=result.get("error", "approval not found"))
            return result

        @self.app.post("/tools/approvals/{request_id}/resolve_bundle")
        async def tool_approval_resolve_bundle(
            request: Request,
            request_id: str,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            if "approved" not in body:
                raise HTTPException(status_code=400, detail="approved is required")
            approval = self.tool_policy.get_approval(request_id)
            if approval is None:
                raise HTTPException(status_code=404, detail=f"approval not found: {request_id}")
            strategy = str(body.get("strategy") or "single").strip() or "single"
            approved = bool(body.get("approved"))
            reason = str(body.get("reason") or "").strip()
            session_id = str(body.get("session_id") or approval.get("session_id") or "")
            user_id = self._resolve_http_user_id(
                request,
                str(body.get("user_id") or "api_user"),
                default_user_id="api_user",
            )
            try:
                specs = self._approval_bundle_specs(
                    approval,
                    strategy=strategy,
                    path_scope=body.get("path_scope") if isinstance(body.get("path_scope"), str) else None,
                    propagate_to_subagents=(
                        bool(body.get("propagate_to_subagents"))
                        if body.get("propagate_to_subagents") is not None
                        else None
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if not specs:
                raise HTTPException(status_code=404, detail="no approvals matched the requested bundle")

            default_reason = (
                f"{'Approved' if approved else 'Denied'} in Web UI ({strategy})"
            )
            results = []
            for spec in specs:
                resolved = await self._resolve_tool_approval_request(
                    request_id=spec["request_id"],
                    approved=approved,
                    reason=reason or default_reason,
                    session_id=session_id,
                    user_id=user_id,
                    source="http",
                    scope=spec.get("scope"),
                    tool_pattern=spec.get("tool_pattern"),
                    path_scope=spec.get("path_scope"),
                    propagate_to_subagents=spec.get("propagate_to_subagents"),
                )
                if resolved.get("resolved"):
                    results.append(resolved)
            if not results:
                raise HTTPException(status_code=404, detail="approvals not found")
            return {
                "resolved": True,
                "strategy": strategy,
                "approved": approved,
                "resolved_count": len(results),
                "request_ids": [item.get("request_id") for item in results if item.get("request_id")],
                "results": results,
            }

        # --- static / chat UI ---

        @self.app.get("/chat")
        async def chat_ui():
            return FileResponse(self.static_dir / "index.html")

        self.app.include_router(build_websocket_router(self))

    # ------------------------------------------------------------------
    # agent execution
    # ------------------------------------------------------------------

    async def _start_agent_run(
        self,
        session_id: str,
        user_id: str,
        message: str,
        request_id: Optional[str] = None,
    ) -> None:
        """Abort existing task then start a new agent run."""
        await self.manager.abort(session_id)
        await self._desktop_clear_stop(session_id=session_id, user_id=user_id)
        task = asyncio.create_task(
            self._agent_run_task(session_id, user_id, message, request_id),
            name=f"agent:{session_id}",
        )
        self.manager.set_task(session_id, task)

    async def _start_control_loop_run(
        self,
        session_id: str,
        user_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> None:
        await self.manager.abort(session_id)
        await self._desktop_clear_stop(session_id=session_id, user_id=user_id)
        task = asyncio.create_task(
            self._control_loop_task(
                session_id,
                user_id,
                goal,
                constraints,
                request_id,
                task_id,
                parent_task_id,
                replay_of_task_id,
                compare_to_task_id,
                initial_state,
                reset_if_terminal,
            ),
            name=f"control:{session_id}",
        )
        self.manager.set_task(session_id, task)

    async def _agent_run_task(
        self,
        session_id: str,
        user_id: str,
        message: str,
        request_id: Optional[str] = None,
    ) -> None:
        """Run agent and send chat.done. On abort, persist partial + aborted flag."""
        partial = ""
        try:
            # Stock price shortcut
            if is_direct_stock_price_query(message):
                await self._send_tool_event(
                    session_id,
                    ev_tool_start(
                        tool_name="stock_price",
                        agent_name=root_agent.name,
                        args={"query": message},
                        request_id=request_id,
                    ),
                )
                quote = await stock_price(message)
                await self._send_tool_event(
                    session_id,
                    ev_tool_result(
                        tool_name="stock_price",
                        agent_name=root_agent.name,
                        ok=bool(quote.get("ok")),
                        result=self._summarize_tool_result(quote),
                        request_id=request_id,
                    ),
                )
                if quote.get("ok"):
                    partial = (
                        f"{quote.get('symbol')} の最新日次データです。\n"
                        f"- 日付: {quote.get('date')}\n"
                        f"- 始値: {quote.get('open')}\n"
                        f"- 高値: {quote.get('high')}\n"
                        f"- 安値: {quote.get('low')}\n"
                        f"- 終値: {quote.get('close')}\n"
                        f"- 出来高: {quote.get('volume')}"
                    )
                else:
                    partial = quote.get("message", "株価データを取得できませんでした。")

                self.transcript.append(
                    session_id, "assistant", partial,
                    user_id=user_id,
                    request_id=request_id,
                )
                await self.manager.send_json(session_id, ev_chat_done(partial, request_id))
                return

            effective_message = self._resolve_control_loop_goal(
                session_id=session_id,
                goal=message,
            )
            decision = await self._select_route_for_message(
                session_id=session_id,
                user_id=user_id,
                message=effective_message,
                source="chat",
            )
            if decision.target == "control_loop":
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=f"Router selected control loop ({decision.reason or 'multi-step task'}).",
                    user_id=user_id,
                    agent_name="root_workflow",
                )
                await self._control_loop_task(
                    session_id,
                    user_id,
                    effective_message,
                    [],
                    request_id,
                    reset_if_terminal=True,
                )
                return

            if decision.target == "dynamic_agent":
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=(
                        f"Router selected dynamic_agent "
                        f"({decision.reason or 'dedicated task environment'})."
                    ),
                    user_id=user_id,
                    agent_name="dynamic_agent",
                )
                spawn = await self._spawn_dynamic_route(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    decision=decision,
                )
                if spawn.get("status") == "accepted":
                    partial = (
                        "Dynamic agent started.\n"
                        f"- run_id: {spawn.get('run_id')}\n"
                        f"- mode: {decision.dynamic_agent.mode or 'run'}"
                    )
                else:
                    partial = spawn.get("error", "Failed to start dynamic agent.")
                self.transcript.append(
                    session_id, "assistant", partial,
                    user_id=user_id,
                    request_id=request_id,
                )
                await self.manager.send_json(
                    session_id,
                    ev_chat_done(partial, request_id, aborted=False),
                )
                return

            routed_message = message
            if decision.target == "specialist" and decision.specialist:
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=(
                        f"Router selected {decision.specialist} "
                        f"({decision.reason or 'specialized task'})."
                    ),
                    user_id=user_id,
                    agent_name=decision.specialist,
                )
                if not decision.preflight_specialist:
                    prepass = await self._run_specialist_prepass(
                        session_id=session_id,
                        user_id=user_id,
                        message=message,
                        specialist_name=decision.specialist,
                        request_id=request_id,
                    )
                    if prepass.infrastructure_blocked:
                        partial = self._format_specialist_runtime_failure(
                            decision.specialist,
                            prepass,
                        )
                    else:
                        partial = prepass.text
                    if not partial.strip():
                        partial = "Specialist did not return a response."
                    self.transcript.append(
                        session_id, "assistant", partial,
                        user_id=user_id,
                        request_id=request_id,
                    )
                    await self.manager.send_json(
                        session_id,
                        ev_chat_done(partial, request_id, aborted=False),
                    )
                    return

                prepass = SpecialistPrepassResult()
                try:
                    prepass = await self._run_specialist_prepass(
                        session_id=session_id,
                        user_id=user_id,
                        message=message,
                        specialist_name=decision.specialist,
                        request_id=request_id,
                    )
                except Exception as exc:
                    await self._emit_routing_event(
                        session_id,
                        status="fallback",
                        message=(
                            f"{decision.specialist} prepass failed; "
                            f"falling back to root_agent ({exc})."
                        ),
                        user_id=user_id,
                        agent_name="root_agent",
                    )
                    prepass = SpecialistPrepassResult()
                if prepass.infrastructure_blocked:
                    partial = self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    )
                    await self._emit_routing_event(
                        session_id,
                        status="blocked",
                        message=(
                            f"{decision.specialist} runtime unavailable; "
                            "not forwarding browser context to root_agent."
                        ),
                        user_id=user_id,
                        agent_name=decision.specialist,
                    )
                    self.transcript.append(
                        session_id,
                        "assistant",
                        partial,
                        user_id=user_id,
                        request_id=request_id,
                        metadata={"type": "specialist_runtime_error"},
                    )
                    await self.manager.send_json(
                        session_id,
                        ev_chat_done(partial, request_id, aborted=False),
                    )
                    return
                routed_message = self._format_root_routing_message(
                    message,
                    decision,
                    specialist_output=prepass.text,
                    specialist_evidence=prepass.evidence_blocks,
                )
                await self._emit_routing_event(
                    session_id,
                    status="forwarded",
                    message=(
                        f"Routing context from {decision.specialist} forwarded to root_agent."
                    ),
                    user_id=user_id,
                    agent_name="root_agent",
                )

            full_msg = await self._compose_grounded_agent_message(
                session_id,
                user_id,
                routed_message,
                research_message=message,
                agent_name=root_agent.name,
                request_id=request_id,
                emit_tool_events=True,
                allow_forced_research=not (
                    decision.target == "specialist"
                    and decision.specialist == "web_researcher"
                    and decision.preflight_specialist
                ),
            )
            content = types.Content(role="user", parts=[types.Part(text=full_msg)])

            async with asyncio.timeout(_AGENT_TIMEOUT):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
                    await self._emit_runner_tool_events(
                        session_id,
                        event,
                        fallback_request_id=request_id,
                    )
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                partial += part.text

            if not partial.strip():
                partial = "応答の生成に失敗しました。もう一度試すか、質問を少し具体化してください。"

            self.audit_logger.log_agent_message(
                agent_name="root_agent",
                message=partial,
                user_id=user_id,
                session_id=session_id,
            )
            # Persist to transcript
            self.transcript.append(
                session_id, "assistant", partial,
                user_id=user_id,
                request_id=request_id,
            )
            await self.manager.send_json(session_id, ev_chat_done(partial, request_id, aborted=False))

        except asyncio.CancelledError:
            # Persist partial + aborted flag to transcript
            self.transcript.append(
                session_id, "assistant", partial,
                user_id=user_id,
                request_id=request_id,
                aborted=True,
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(partial, request_id, aborted=True),
            )
            raise

        except TimeoutError:
            msg = f"Agent timed out after {_AGENT_TIMEOUT} seconds."
            self.audit_logger.log_error(error=msg, user_id=user_id, session_id=session_id,
                                        context={"reason": "timeout"})
            self.transcript.append(
                session_id, "assistant", msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"error": "timeout"},
            )
            await self.manager.send_json(session_id, ev_chat_done(msg, request_id, aborted=False))

        except Exception as exc:
            self.audit_logger.log_error(error=str(exc), user_id=user_id, session_id=session_id,
                                        context={"message": message})
            error_msg = f"Error: {exc}"
            self.transcript.append(
                session_id, "assistant", error_msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"error": str(exc)},
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(error_msg, request_id, aborted=False),
            )

        finally:
            self.manager.clear_task(session_id)

    async def _control_loop_task(
        self,
        session_id: str,
        user_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> None:
        try:
            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session_id,
                goal=goal,
                constraints=constraints,
                request_id=request_id,
                source="websocket",
                preserve_control_ui_tab=True,
                task_id=task_id,
                parent_task_id=parent_task_id,
                replay_of_task_id=replay_of_task_id,
                compare_to_task_id=compare_to_task_id,
                initial_state=initial_state,
                reset_if_terminal=reset_if_terminal,
            )
            self.transcript.append(
                session_id,
                "assistant",
                result.final_text,
                user_id=user_id,
                request_id=request_id,
                metadata={
                    "type": "control_loop",
                    "success": result.success,
                    "needs_human": bool(result.metadata.get("needs_human")),
                    "plan_id": result.plan_id,
                    "task_id": result.metadata.get("task_id"),
                },
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session_id,
                    result.metadata.get("approval_request"),
                )
            await self.manager.send_json(
                session_id,
                ev_chat_done(result.final_text, request_id, aborted=False),
            )
        except asyncio.CancelledError:
            await self.manager.send_json(
                session_id,
                ev_chat_done("", request_id, aborted=True),
            )
            raise
        except Exception as exc:
            error_msg = f"Control loop error: {exc}"
            self.transcript.append(
                session_id,
                "assistant",
                error_msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"type": "control_loop", "error": str(exc)},
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(error_msg, request_id, aborted=False),
            )
        finally:
            self.manager.clear_task(session_id)

    def _merge_control_constraints(
        self,
        *,
        goal: str,
        constraints: list[str],
        preserve_control_ui_tab: bool,
    ) -> list[str]:
        effective_constraints = list(constraints)
        if prefers_isolated_browser_for_goal(goal):
            for item in _ISOLATED_BROWSER_TEXT_ENTRY_CONSTRAINTS:
                if item not in effective_constraints:
                    effective_constraints.append(item)
            return effective_constraints
        if not targets_user_browser(goal):
            return effective_constraints
        for item in _CURRENT_BROWSER_CONTROL_BASE_CONSTRAINTS:
            if item not in effective_constraints:
                effective_constraints.append(item)
        if preserve_control_ui_tab:
            if (
                _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
                in effective_constraints
            ):
                effective_constraints.remove(
                    _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
                )
            if (
                _CURRENT_BROWSER_PRESERVE_CONTROL_UI_TAB_CONSTRAINT
                not in effective_constraints
            ):
                effective_constraints.append(
                    _CURRENT_BROWSER_PRESERVE_CONTROL_UI_TAB_CONSTRAINT
                )
        elif (
            _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
            not in effective_constraints
        ):
            effective_constraints.append(
                _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
            )
        return effective_constraints

    @staticmethod
    def _should_expand_control_loop_followup(message: str) -> bool:
        normalized = str(message or "").strip().lower()
        if not normalized or len(normalized) > 40:
            return False
        if any(marker in normalized for marker in _CONTROL_LOOP_FOLLOWUP_MARKERS):
            return True
        return any(keyword in normalized for keyword in SPREADSHEET_KEYWORDS)

    def _latest_completed_control_loop_resume_context(
        self,
        *,
        session_id: str,
    ) -> dict[str, Any] | None:
        payload = self.task_store.query(
            owner_session_id=session_id,
            kind="control_loop",
            status="completed",
            page=1,
            page_size=10,
        )
        tasks = payload.get("tasks")
        tasks = tasks if isinstance(tasks, list) else []
        for task in tasks:
            artifacts = task.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            resume_context = artifacts.get("resume_context")
            if isinstance(resume_context, dict) and str(resume_context.get("goal") or "").strip():
                return resume_context
        return None

    def _resolve_control_loop_goal(
        self,
        *,
        session_id: str,
        goal: str,
    ) -> str:
        normalized_goal = str(goal or "").strip()
        if not self._should_expand_control_loop_followup(normalized_goal):
            return normalized_goal
        resume_context = self._latest_completed_control_loop_resume_context(
            session_id=session_id,
        )
        if not isinstance(resume_context, dict):
            return normalized_goal
        prior_goal = str(resume_context.get("goal") or "").strip()
        if not prior_goal or prior_goal == normalized_goal:
            return normalized_goal
        return (
            f"{prior_goal}\n\n"
            f"Follow-up instruction: {normalized_goal}"
        )

    # HTTP agent execution (no abort support)
    async def _run_agent_http(self, user_id: str, session_id: str, message: str) -> dict:
        if is_direct_stock_price_query(message):
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name="stock_price",
                    agent_name=root_agent.name,
                    args={"query": message},
                ),
            )
            quote = await stock_price(message)
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name="stock_price",
                    agent_name=root_agent.name,
                    ok=bool(quote.get("ok")),
                    result=self._summarize_tool_result(quote),
                ),
            )
            if quote.get("ok"):
                text = (
                    f"{quote.get('symbol')} の最新日次データです。\n"
                    f"- 日付: {quote.get('date')}\n"
                    f"- 始値: {quote.get('open')}\n"
                    f"- 高値: {quote.get('high')}\n"
                    f"- 安値: {quote.get('low')}\n"
                    f"- 終値: {quote.get('close')}\n"
                    f"- 出来高: {quote.get('volume')}"
                )
            else:
                text = quote.get("message", "株価データを取得できませんでした。")
            return {
                "type": "agent_message",
                "message": text,
                "ok": bool(quote.get("ok")),
            }

        effective_message = self._resolve_control_loop_goal(
            session_id=session_id,
            goal=message,
        )
        decision = await self._select_route_for_message(
            session_id=session_id,
            user_id=user_id,
            message=effective_message,
            source="http",
        )
        if decision.target == "control_loop":
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=f"Router selected control loop ({decision.reason or 'multi-step task'}).",
                user_id=user_id,
                agent_name="root_workflow",
            )
            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session_id,
                goal=effective_message,
                constraints=[],
                source="http",
                reset_if_terminal=True,
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session_id,
                    result.metadata.get("approval_request"),
                )
            return {
                "type": "control_loop",
                "message": result.final_text,
                "ok": result.success,
                "task_id": result.metadata.get("task_id"),
            }

        if decision.target == "dynamic_agent":
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=(
                    f"Router selected dynamic_agent "
                    f"({decision.reason or 'dedicated task environment'})."
                ),
                user_id=user_id,
                agent_name="dynamic_agent",
            )
            spawn = await self._spawn_dynamic_route(
                session_id=session_id,
                user_id=user_id,
                message=message,
                decision=decision,
            )
            if spawn.get("status") == "accepted":
                return {
                    "type": "dynamic_agent",
                    "message": (
                        "Dynamic agent started.\n"
                        f"- run_id: {spawn.get('run_id')}\n"
                        f"- mode: {decision.dynamic_agent.mode or 'run'}"
                    ),
                    "ok": True,
                }
            return {
                "type": "error",
                "message": spawn.get("error", "Failed to start dynamic agent."),
                "ok": False,
            }

        routed_message = message
        if decision.target == "specialist" and decision.specialist:
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=(
                    f"Router selected {decision.specialist} "
                    f"({decision.reason or 'specialized task'})."
                ),
                user_id=user_id,
                agent_name=decision.specialist,
            )
            if not decision.preflight_specialist:
                prepass = await self._run_specialist_prepass(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    specialist_name=decision.specialist,
                )
                if prepass.infrastructure_blocked:
                    response_text = self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    )
                    return {
                        "type": "error",
                        "message": response_text,
                        "ok": False,
                    }
                response_text = prepass.text
                if not response_text.strip():
                    response_text = "Specialist did not return a response."
                return {
                    "type": "specialist",
                    "message": response_text,
                    "ok": True,
                }

            prepass = SpecialistPrepassResult()
            try:
                prepass = await self._run_specialist_prepass(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    specialist_name=decision.specialist,
                )
            except Exception as exc:
                await self._emit_routing_event(
                    session_id,
                    status="fallback",
                    message=(
                        f"{decision.specialist} prepass failed; "
                        f"falling back to root_agent ({exc})."
                    ),
                    user_id=user_id,
                    agent_name="root_agent",
                )
                prepass = SpecialistPrepassResult()
            if prepass.infrastructure_blocked:
                await self._emit_routing_event(
                    session_id,
                    status="blocked",
                    message=(
                        f"{decision.specialist} runtime unavailable; "
                        "not forwarding browser context to root_agent."
                    ),
                    user_id=user_id,
                    agent_name=decision.specialist,
                )
                return {
                    "type": "error",
                    "message": self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    ),
                    "ok": False,
                }
            routed_message = self._format_root_routing_message(
                message,
                decision,
                specialist_output=prepass.text,
                specialist_evidence=prepass.evidence_blocks,
            )
            await self._emit_routing_event(
                session_id,
                status="forwarded",
                message=(
                    f"Routing context from {decision.specialist} forwarded to root_agent."
                ),
                user_id=user_id,
                agent_name="root_agent",
            )

        full_msg = await self._compose_grounded_agent_message(
            session_id,
            user_id,
            routed_message,
            research_message=message,
            agent_name=root_agent.name,
            emit_tool_events=False,
            allow_forced_research=not (
                decision.target == "specialist"
                and decision.specialist == "web_researcher"
                and decision.preflight_specialist
            ),
        )
        content = types.Content(role="user", parts=[types.Part(text=full_msg)])

        try:
            response_text = ""
            async with asyncio.timeout(_AGENT_TIMEOUT):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
                    await self._emit_runner_tool_events(
                        session_id,
                        event,
                    )
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                response_text += part.text

            if not response_text.strip():
                response_text = "応答の生成に失敗しました。もう一度試すか、質問を少し具体化してください。"

            self.audit_logger.log_agent_message(
                agent_name="root_agent",
                message=response_text,
                user_id=user_id,
                session_id=session_id,
            )
            return {"type": "agent_message", "message": response_text, "ok": True}

        except TimeoutError:
            msg = f"Agent timed out after {_AGENT_TIMEOUT} seconds."
            self.audit_logger.log_error(
                error=msg,
                user_id=user_id,
                session_id=session_id,
                context={"message": message, "reason": "timeout"},
            )
            return {"type": "error", "message": msg, "ok": False}

        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"message": message},
            )
            return {"type": "error", "message": f"Error: {exc}", "ok": False}

    async def _run_control_loop_with_task(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str],
        source: str,
        preserve_control_ui_tab: bool,
        task_id: Optional[str] = None,
        owner_session_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> tuple[ExecutionResult, str]:
        effective_constraints = self._merge_control_constraints(
            goal=goal,
            constraints=constraints,
            preserve_control_ui_tab=preserve_control_ui_tab,
        )
        artifacts, metadata = self._control_loop_seed_payload(
            goal=goal,
            constraints=effective_constraints,
            source=source,
            request_id=request_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            replay_from_step=(
                str(initial_state.get(StateKeys.REPLAY_FROM_STEP) or "").strip()
                if isinstance(initial_state, dict)
                else None
            ),
            replay_mode=(
                str((initial_state.get(StateKeys.REPLAY_CONTEXT) or {}).get("mode") or "").strip()
                if isinstance(initial_state, dict) and isinstance(initial_state.get(StateKeys.REPLAY_CONTEXT), dict)
                else None
            ),
        )
        if task_id:
            update_task_record(
                task_id,
                status="running",
                artifacts=artifacts,
                metadata=metadata,
                error=None,
            )
        else:
            task = self._create_control_loop_task_record(
                user_id=user_id,
                session_id=session_id,
                owner_session_id=owner_session_id,
                goal=goal,
                constraints=effective_constraints,
                request_id=request_id,
                source=source,
                parent_task_id=parent_task_id,
                replay_of_task_id=replay_of_task_id,
                compare_to_task_id=compare_to_task_id,
            )
            task_id = str(task["task_id"])

        current_browser_error = await self._current_browser_runtime_error(goal)
        if current_browser_error:
            update_task_record(
                task_id,
                status="failed",
                artifacts={
                    "result": {
                        "success": False,
                        "error": "desktop_bridge_unavailable",
                        "final_text": current_browser_error,
                    }
                },
                error="desktop_bridge_unavailable",
            )
            result = ExecutionResult(
                request_id=f"http_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                user_id=user_id,
                final_text=current_browser_error,
                success=False,
                metadata={"error": "desktop_bridge_unavailable", "task_id": task_id},
            )
            return result, task_id

        result = await self.control_loop.run(
            goal=goal,
            user_id=user_id,
            constraints=effective_constraints,
            session_id=session_id,
            initial_state=initial_state,
            reset_if_terminal=reset_if_terminal,
        )
        result.metadata["task_id"] = task_id
        needs_human = bool(result.metadata.get("needs_human"))
        approval_expired = (
            not result.success
            and not needs_human
            and any(
                reason in (result.final_text or "")
                for reason in APPROVAL_EXPIRY_REASONS
            )
        )
        error_text = None
        if not result.success and not needs_human:
            error_text = "approval_expired" if approval_expired else (result.final_text or "control loop failed")
        failure_classification = classify_control_loop_failure(
            success=result.success,
            needs_human=needs_human,
            final_text=result.final_text,
            verification_status=result.metadata.get("verification_status"),
            verification_report=(
                result.metadata.get("verification_report")
                if isinstance(result.metadata.get("verification_report"), dict)
                else None
            ),
            error=error_text,
            existing_failure_type=result.metadata.get("normalized_failure_type"),
        )
        result.metadata.update(failure_classification)
        update_task_record(
            task_id,
            status="pending" if needs_human else ("completed" if result.success else "failed"),
            artifacts={
                "result": {
                    "success": result.success,
                    "final_text": result.final_text,
                    "plan_id": result.plan_id,
                    "verification_report_id": result.verification_report_id,
                    "verification_status": result.metadata.get("verification_status"),
                    "verification_report": result.metadata.get("verification_report"),
                    "verification_inputs": result.metadata.get("verification_inputs"),
                    "artifact_refs": result.metadata.get("artifact_refs"),
                    "approved_plan": result.metadata.get("approved_plan"),
                    "step_trace": result.metadata.get("step_trace"),
                    "tail_replay_from_step_id": result.metadata.get("tail_replay_from_step_id"),
                    "repair_count": result.repair_count,
                    "promoted_memory_ids": result.promoted_memory_ids,
                    "approval_request": result.metadata.get("approval_request"),
                    "preliminary_failure_type": failure_classification["preliminary_failure_type"],
                    "normalized_failure_type": failure_classification["normalized_failure_type"],
                    "classified_by": failure_classification["classified_by"],
                    "operator_override": failure_classification["operator_override"],
                    **({"approval_expired": True} if approval_expired else {}),
                },
                "resume_context": {
                    "goal": goal,
                    "constraints": effective_constraints,
                    "plan_id": result.plan_id,
                    "approved_plan": result.metadata.get("approved_plan"),
                    "approval_request": result.metadata.get("approval_request"),
                },
            },
            metadata={
                "source": source,
                "request_id": request_id,
                "needs_human": needs_human,
                "normalized_failure_type": failure_classification["normalized_failure_type"],
                "classified_by": failure_classification["classified_by"],
                **(
                    {
                        "replay_from_step": str(initial_state.get(StateKeys.REPLAY_FROM_STEP) or "").strip(),
                    }
                    if isinstance(initial_state, dict) and initial_state.get(StateKeys.REPLAY_FROM_STEP)
                    else {}
                ),
                **({"approval_expired": True} if approval_expired else {}),
            },
            error=error_text,
        )
        persist_control_loop_step_events(task_id=task_id, result=result)
        return result, task_id

    async def _run_control_loop_http(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        source: str = "http",
        preserve_control_ui_tab: bool = False,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ):
        result, _task_id = await self._run_control_loop_with_task(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=constraints,
            request_id=request_id,
            source=source,
            preserve_control_ui_tab=preserve_control_ui_tab,
            task_id=task_id,
            parent_task_id=parent_task_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            initial_state=initial_state,
            reset_if_terminal=reset_if_terminal,
        )
        return result

    async def _emit_control_approval_request(
        self,
        session_id: str,
        approval_request: dict[str, Any] | None,
    ) -> None:
        if not approval_request:
            return
        await self.manager.send_or_queue_json(
            session_id,
            ev_control_approval_request(
                request_id=approval_request.get("request_id", ""),
                plan_id=approval_request.get("plan_id", ""),
                goal=approval_request.get("goal", ""),
                risk_level=approval_request.get("risk_level", ""),
                required_capabilities=approval_request.get(
                    "required_capabilities", []
                ),
                plan=approval_request.get("plan", {}),
                reason=approval_request.get("reason", ""),
            ),
        )

    async def _resolve_tool_approval_request(
        self,
        *,
        request_id: str,
        approved: bool,
        reason: str,
        session_id: str,
        user_id: str,
        source: str = "unknown",
        scope: Any = None,
        tool_pattern: Any = None,
        path_scope: Any = None,
        expires_at: Any = None,
        propagate_to_subagents: Any = None,
    ) -> dict[str, Any]:
        before = self.tool_policy.get_approval(request_id)
        requested_scope = scope if isinstance(scope, str) else None
        requested_tool_pattern = tool_pattern if isinstance(tool_pattern, str) else None
        requested_path_scope = path_scope if isinstance(path_scope, str) else None
        requested_propagate = (
            bool(propagate_to_subagents)
            if propagate_to_subagents is not None
            else None
        )
        result = self.tool_policy.resolve_approval(
            request_id,
            approved,
            reason,
            scope=requested_scope,
            tool_pattern=requested_tool_pattern,
            path_scope=requested_path_scope,
            expires_at=expires_at if isinstance(expires_at, (int, float)) else None,
            propagate_to_subagents=requested_propagate,
            history_metadata={
                "actor_user_id": user_id,
                "source": source,
                "scope_before": before.get("scope") if isinstance(before, dict) else None,
                "tool_pattern_before": before.get("tool_pattern") if isinstance(before, dict) else None,
                "path_scope_before": before.get("path_scope") if isinstance(before, dict) else None,
                "propagate_to_subagents_before": (
                    before.get("propagate_to_subagents") if isinstance(before, dict) else None
                ),
            },
        )
        control_loop_resolved = False
        pending_control_request = None
        if result is None:
            pending_control_request = await self.control_loop.get_pending_approval(
                user_id=user_id,
                session_id=session_id,
            )
            pending_control_task_id = self._find_control_loop_task_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            control_loop_resolved = await self.control_loop.resolve_human_approval(
                user_id=user_id,
                session_id=session_id,
                approved=approved,
                request_id=request_id,
            )
            if approved and control_loop_resolved and pending_control_request:
                resume_goal = (
                    await self.control_loop.get_task_goal(
                        user_id=user_id,
                        session_id=session_id,
                    )
                    or pending_control_request.get("goal", "")
                )
                await self._start_control_loop_run(
                    session_id=session_id,
                    user_id=user_id,
                    goal=resume_goal,
                    constraints=normalize_constraints(
                        (pending_control_request.get("plan") or {}).get("constraints")
                    ),
                    task_id=pending_control_task_id,
                )

        target_session_id = result.session_id if result else session_id
        status = "resolved" if result or control_loop_resolved else "not_found"
        await self._emit_session_event(
            target_session_id,
            source="tools.approval",
            status=status,
            message=f"Approval {request_id}: {'approved' if approved else 'denied'}",
            user_id=user_id,
        )
        response: dict[str, Any] = {
            "resolved": bool(result or control_loop_resolved),
            "request_id": request_id,
            "approved": approved,
            "status": status,
            "session_id": target_session_id,
        }
        audit_metadata: dict[str, Any] = {
            "request_id": request_id,
            "approved": approved,
            "source": source,
            "actor_user_id": user_id,
            "target_session_id": target_session_id,
        }
        if result is not None:
            response["approval"] = result.to_dict()
            after = result.to_dict()
            audit_metadata.update(
                {
                    "resolved_kind": "tool_approval",
                    "tool_name": after.get("tool_name"),
                    "agent_name": after.get("agent_name"),
                    "source_request_id": after.get("source_request_id"),
                    "state_before": before.get("state") if isinstance(before, dict) else None,
                    "state_after": after.get("state"),
                    "scope_before": before.get("scope") if isinstance(before, dict) else None,
                    "scope_after": after.get("scope"),
                    "tool_pattern_before": before.get("tool_pattern") if isinstance(before, dict) else None,
                    "tool_pattern_after": after.get("tool_pattern"),
                    "path_scope_before": before.get("path_scope") if isinstance(before, dict) else None,
                    "path_scope_after": after.get("path_scope"),
                    "propagate_to_subagents_before": (
                        before.get("propagate_to_subagents") if isinstance(before, dict) else None
                    ),
                    "propagate_to_subagents_after": after.get("propagate_to_subagents"),
                    "resolve_reason": reason,
                }
            )
        elif control_loop_resolved:
            response["control_loop"] = {"request_id": request_id, "approved": approved}
            audit_metadata.update(
                {
                    "resolved_kind": "control_loop",
                    "resolve_reason": reason,
                }
            )
        else:
            response["error"] = f"approval not found: {request_id}"
        self.audit_logger.log(
            event_type=AuditEventType.TOOL_APPROVAL,
            user_id=user_id or None,
            session_id=target_session_id or None,
            action="resolve",
            resource=request_id,
            result=status,
            metadata=audit_metadata,
        )
        return response

    async def _desktop_emergency_stop(
        self,
        *,
        session_id: str,
        user_id: str,
        reason: str,
    ) -> bool:
        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return False
        try:
            from src.bridges.desktop_bridge_client import get_desktop_client
            from src.desktop import DesktopEmergencyStopRequest

            client = get_desktop_client()
            result = await client.emergency_stop(
                DesktopEmergencyStopRequest(
                    request_id=f"gateway-stop-{uuid.uuid4().hex[:12]}",
                    session_id=session_id,
                    user_id=user_id,
                    agent_name="gateway",
                    reason=reason,
                )
            )
            return bool(result.ok)
        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"action": "desktop_emergency_stop"},
            )
            return False

    async def _desktop_clear_stop(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> bool:
        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return False
        try:
            from src.bridges.desktop_bridge_client import get_desktop_client
            from src.desktop import DesktopClearStopRequest

            client = get_desktop_client()
            result = await client.clear_stop(
                DesktopClearStopRequest(
                    request_id=f"gateway-clear-stop-{uuid.uuid4().hex[:12]}",
                    session_id=session_id,
                    user_id=user_id,
                    agent_name="gateway",
                )
            )
            return bool(result.ok)
        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"action": "desktop_clear_stop"},
            )
            return False

    # ------------------------------------------------------------------
    # session / transcript helpers
    # ------------------------------------------------------------------

    async def _get_or_create_gateway_session(
        self,
        *,
        user_id: str,
        session_id: Optional[str] = None,
    ):
        session = None
        if session_id:
            session = await self.session_service.get_session(
                app_name="boiled-claw",
                user_id=user_id,
                session_id=session_id,
            )
            if session is None and self.transcript.has_session(session_id, user_id):
                session = await self._hydrate_session_from_transcript(user_id, session_id)

        if session is None:
            session = await self.session_service.create_session(
                app_name="boiled-claw",
                user_id=user_id,
                session_id=session_id,
            )

        self.transcript.ensure_session(session.id, user_id)
        return session

    async def _hydrate_session_from_transcript(self, user_id: str, session_id: str):
        session = await self.session_service.create_session(
            app_name="boiled-claw",
            user_id=user_id,
            session_id=session_id,
        )
        for entry in self.transcript.get_history(session_id, limit=500):
            if entry.role not in {"user", "assistant"}:
                continue
            if not entry.content.strip():
                continue
            content_role = "user" if entry.role == "user" else "model"
            author = "user" if entry.role == "user" else root_agent.name
            event = Event(
                invocation_id=f"hydrated:{session_id}",
                author=author,
                content=types.Content(
                    role=content_role,
                    parts=[types.Part(text=entry.content)],
                ),
                timestamp=entry.created_at,
            )
            await self.session_service.append_event(session, event)
        return session

    def _compose_agent_message(self, session_id: str, message: str) -> str:
        now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        history = self.transcript.get_history(session_id, limit=200)
        inject_lines = []
        for entry in history:
            if entry.role != "inject":
                continue
            inject_role = entry.metadata.get("role", "system")
            inject_lines.append(f"[inject:{inject_role}] {entry.content}")

        preface = [f"[システム情報: 現在の日時は {now} です]"]
        if inject_lines:
            preface.append("[Gateway inject context]")
            preface.extend(inject_lines[-10:])
        preface.append("")
        preface.append(message)
        return "\n".join(preface)

    def _resolve_cron_delivery_target(self, payload: Dict[str, Any]) -> str:
        delivery_target = str(payload.get("delivery_target") or "isolated").strip()
        bound_session_id = str(payload.get("session_id") or "").strip()
        system_event = payload.get("system_event") or None
        if delivery_target == "main":
            if bound_session_id:
                return f"session:{bound_session_id}"
            if system_event in {"connect", "disconnect"}:
                return "main"
            raise ValueError(
                "delivery_target='main' requires either a session_id binding "
                "or a connect/disconnect system_event trigger"
            )
        return delivery_target or "isolated"

    async def _emit_session_event(
        self,
        session_id: str,
        *,
        source: str,
        status: str,
        message: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> None:
        event = ev_system_event(
            source=source,
            status=status,
            message=message,
            run_id=run_id,
            task_id=task_id,
            agent_name=agent_name,
        )
        session = self.transcript.get_session(session_id)
        resolved_user = user_id or (session.user_id if session else None)
        if (
            session is not None
            and resolved_user
            and source != "tools.approval"
        ):
            self.transcript.append(
                session_id,
                "system",
                message,
                user_id=resolved_user,
                metadata={
                    "source": source,
                    "status": status,
                    "run_id": run_id or "",
                    "task_id": task_id or "",
                    "agent_name": agent_name or "",
                },
            )
        elif session_id not in self.manager.active_connections:
            return
        await self.manager.send_or_queue_json(session_id, event)

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            # Sweep approval expiry so expiring/expired notifications fire
            # even when no new approval request arrives.
            self.tool_policy.cleanup_expired()
            if self.manager.active_connections:
                tick = ev_health_tick(len(self.manager.active_connections))
                await self.manager.broadcast_json(tick)
                await self.manager.flush_all_pending()

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self, host: Optional[str] = None, port: Optional[int] = None):
        import uvicorn
        uvicorn.run(
            self.app,
            host=host or self.settings.gateway_host,
            port=port or self.settings.gateway_port,
        )
def create_gateway() -> GatewayServer:
    return GatewayServer()
