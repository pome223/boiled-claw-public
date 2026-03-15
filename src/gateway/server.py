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

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pathlib import Path

from src.config.settings import get_settings
from src.agents.root_agent import root_agent
from src.agents.sub_agents import SUB_AGENTS
from src.control_loop.root_workflow import ControlLoop, ExecutionResult
from src.gateway.routing_agent import routing_agent
from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
from src.security.audit import get_audit_logger, AuditEventType
from src.security.tool_policy import get_tool_policy_engine
from src.tools.finance import is_direct_stock_price_query, stock_price
from src.tools.web_search import web_search
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import skill_list as tool_skill_list, skill_execute as tool_skill_execute
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier
from src.gateway.protocol import (
    PROTOCOL_VERSION,
    ev_connected, ev_chat_done, ev_chat_history, ev_system_event,
    ev_health_tick, ev_cron_update, ev_tools_approval_request,
    ev_control_approval_request,
    ev_tool_start, ev_tool_result,
    normalize_client_event, validate_client_event,
)
from src.gateway.routing import (
    RoutingDecision,
    decision_from_payload,
    heuristic_decision,
    targets_user_browser,
)
from src.runtime.tool_events import set_tool_event_notifier
from src.gateway.transcript import get_transcript_store
from src.cron.scheduler import get_scheduler

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
}
_BROWSER_INFRA_ERROR_FRAGMENTS = (
    "playwright is not installed",
    "host bridge is not enabled",
    "host_bridge_enabled is true but host_bridge_url is not set",
    "host bridge tool call failed",
    "host bridge returned empty tool content",
    "host bridge returned non-json tool content",
)
_USER_BROWSER_REQUIRED_CAPABILITIES = {
    "desktop.view.windows",
    "desktop.control.focus_window",
    "desktop.ax.find",
    "desktop.control.click",
    "desktop.control.type",
}


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

    def disconnect(self, session_id: str) -> None:
        self.active_connections.pop(session_id, None)
        self._session_users.pop(session_id, None)
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
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.manager = ConnectionManager()
        self.session_service = InMemorySessionService()
        self.memory_service = get_promoted_memory_service()
        self.subagent_manager = get_subagent_manager()
        self.runner = Runner(
            agent=root_agent,
            app_name="boiled-claw",
            session_service=self.session_service,
            memory_service=self.memory_service,
        )
        self.routing_session_service = InMemorySessionService()
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
        self.transcript = get_transcript_store()
        self.tool_policy = get_tool_policy_engine()
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
            await self.manager.send_or_queue_json(
                session_id,
                ev_tools_approval_request(
                    request_id=payload.get("request_id", ""),
                    tool_name=payload.get("tool_name", ""),
                    agent_name=payload.get("agent_name", ""),
                    args=payload.get("args") or {},
                    reason=payload.get("reason", ""),
                ),
            )

        self._approval_notifier_fn = _approval_notifier
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
        set_subagent_notifier(None)
        set_tool_event_notifier(None)
        self.tool_policy.set_notifier(None)
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
        if specialist_name in {"browser_automator", "control_ui_chat_operator"} and result.infrastructure_blocked:
            return (
                "ブラウザ操作は実行できませんでした。\n"
                f"- 原因: {first_error}\n"
                "- 現在のリクエストでは、ブラウザを実際に操作せずに web_search へ自動フォールバックしません。\n"
                "- 対応: Host Bridge を有効化して host 側で Playwright を実行するか、"
                "この実行環境に Playwright をインストールしてください。"
            )

        return (
            f"{specialist_name} の実行に失敗しました。\n"
            f"- 原因: {first_error}"
        )

    async def _current_browser_runtime_error(
        self,
        message: str,
    ) -> str | None:
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
        if specialist_output:
            lines.extend(
                [
                    "",
                    f"[Specialist output from {decision.specialist}]",
                    specialist_output.strip(),
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
        )
        await self._deliver_background_result(
            session_id=session_id,
            user_id=user_id,
            source="cron",
            run_id=run_id,
            agent_name="control_loop",
            message=result.final_text,
            ok=result.success,
            metadata={"type": "control_loop", "plan_id": result.plan_id},
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
                "active_sessions": len(self.manager.active_connections),
                "skills_loaded": get_skills_report().get("loaded", False),
                "skills_count": get_skills_report().get("count", 0),
            }

        @self.app.get("/health")
        async def health():
            return {"status": "healthy"}

        @self.app.get("/protocol")
        async def protocol_info():
            from src.gateway.protocol import EVENT_SCHEMAS
            return {
                "version": PROTOCOL_VERSION,
                "events": list(EVENT_SCHEMAS.keys()),
                "schemas": EVENT_SCHEMAS,
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
            constraints = _normalize_constraints(body.get("constraints"))

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
                resumed_result = await self._run_control_loop_http(
                    user_id=user_id,
                    session_id=session_id,
                    goal=resume_goal,
                    constraints=_normalize_constraints(
                        (pending.get("plan") or {}).get("constraints")
                    ),
                )
                if resumed_result.metadata.get("needs_human"):
                    await self._emit_control_approval_request(
                        session_id,
                        resumed_result.metadata.get("approval_request"),
                    )

            response = {
                "ok": True,
                "session_id": session_id,
                "request_id": request_id,
                "approved": approved,
            }
            if resumed_result is not None:
                response["result"] = {
                    "ok": resumed_result.success,
                    "response": resumed_result.final_text,
                    "plan_id": resumed_result.plan_id,
                    "verification_report_id": resumed_result.verification_report_id,
                    "repair_count": resumed_result.repair_count,
                    "needs_human": bool(resumed_result.metadata.get("needs_human")),
                    "approval_request": resumed_result.metadata.get("approval_request"),
                    "promoted_memory_ids": resumed_result.promoted_memory_ids,
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
        async def tool_approvals_list(session_id: Optional[str] = None):
            return {"approvals": self.tool_policy.list_pending_approvals(session_id)}

        # --- static / chat UI ---

        @self.app.get("/chat")
        async def chat_ui():
            return FileResponse(self.static_dir / "index.html")

        # --- WebSocket ---

        @self.app.websocket("/ws/{user_id}")
        async def websocket_endpoint(
            websocket: WebSocket,
            user_id: str,
            session_id: Optional[str] = Query(default=None),
            token: Optional[str] = Query(default=None),
        ):
            if self.settings.gateway_api_key:
                if token != self.settings.gateway_api_key:
                    await websocket.close(code=4401, reason="Unauthorized")
                    return
            try:
                user_id = self._resolve_websocket_user_id(
                    websocket,
                    user_id,
                    default_user_id="web_user",
                )
            except HTTPException as exc:
                await websocket.close(code=4401, reason=str(exc.detail))
                return

            session = await self._get_or_create_gateway_session(
                user_id=user_id,
                session_id=session_id,
            )
            session_id = session.id

            await self.manager.connect(websocket, session_id, user_id)
            self.audit_logger.log(
                event_type=AuditEventType.SESSION_START,
                user_id=user_id,
                session_id=session_id,
                action="connect",
                result="success",
            )

            try:
                # Send connected event with protocol version
                await self.manager.send_json(session_id, ev_connected(session_id, user_id))
                await self.manager.flush_pending(session_id)

                # Fire connect system event for cron jobs
                asyncio.create_task(
                    get_scheduler().fire_system_event("connect", {
                        "user_id": user_id, "session_id": session_id,
                    }),
                    name=f"sys:connect:{session_id}",
                )

                while True:
                    raw_data = await websocket.receive_json()
                    data = normalize_client_event(raw_data)
                    validation_errors = validate_client_event(data)
                    if validation_errors:
                        await self._emit_session_event(
                            session_id,
                            source="protocol",
                            status="error",
                            message="; ".join(validation_errors),
                            user_id=user_id,
                        )
                        continue
                    event_name = data.get("event", "")

                    if event_name == "chat.send":
                        text = (data.get("text") or "").strip()
                        request_id = data.get("request_id")
                        if text:
                            # Record user message in transcript
                            self.transcript.append(
                                session_id, "user", text,
                                user_id=user_id,
                                request_id=request_id,
                            )
                            await self._start_agent_run(session_id, user_id, text, request_id)

                    elif event_name == "control.run":
                        goal = (data.get("goal") or "").strip()
                        constraints = _normalize_constraints(data.get("constraints"))
                        request_id = data.get("request_id")
                        if goal:
                            self.transcript.append(
                                session_id,
                                "user",
                                goal,
                                user_id=user_id,
                                request_id=request_id,
                                metadata={
                                    "type": "control_loop",
                                    "constraints": constraints,
                                },
                            )
                            await self._start_control_loop_run(
                                session_id,
                                user_id,
                                goal,
                                constraints,
                                request_id,
                            )

                    elif event_name == "chat.inject":
                        text = (data.get("text") or "").strip()
                        role = data.get("role", "system")
                        request_id = data.get("request_id")
                        if text:
                            self.transcript.append(
                                session_id, "inject", text,
                                user_id=user_id,
                                request_id=request_id,
                                metadata={"role": role},
                            )
                            await self._emit_session_event(
                                session_id,
                                source="inject",
                                status="ok",
                                message=f"Injected {role} message into transcript",
                                user_id=user_id,
                            )

                    elif event_name == "chat.abort":
                        request_id = data.get("request_id")
                        aborted = await self.manager.abort(session_id)
                        await self._desktop_emergency_stop(
                            session_id=session_id,
                            user_id=user_id,
                            reason="Abort requested from Web UI",
                        )
                        if not aborted:
                            await self.manager.send_json(
                                session_id,
                                ev_chat_done("", request_id, aborted=False),
                            )

                    elif event_name == "chat.history":
                        request_id = data.get("request_id")
                        target_session = data.get("session_id") or session_id
                        if not self.transcript.has_session(target_session, user_id):
                            await self._emit_session_event(
                                session_id,
                                source="protocol",
                                status="error",
                                message=f"session not found: {target_session}",
                                user_id=user_id,
                            )
                            continue
                        limit = min(int(data.get("limit") or 100), 500)
                        before = data.get("before")
                        entries = self.transcript.get_history(
                            target_session, limit=limit, before=before,
                        )
                        await self.manager.send_json(
                            session_id,
                            ev_chat_history(
                                [e.to_dict() for e in entries],
                                target_session,
                                request_id,
                            ),
                        )

                    elif event_name == "presence.ping":
                        await self.manager.send_json(
                            session_id,
                            ev_health_tick(len(self.manager.active_connections)),
                        )

                    elif event_name == "tools.approval":
                        request_id = data.get("request_id", "")
                        approved = bool(data.get("approved", False))
                        reason = data.get("reason", "")
                        result = self.tool_policy.resolve_approval(
                            request_id, approved, reason,
                        )
                        control_loop_resolved = False
                        pending_control_request = None
                        if result is None:
                            pending_control_request = (
                                await self.control_loop.get_pending_approval(
                                    user_id=user_id,
                                    session_id=session_id,
                                )
                            )
                            control_loop_resolved = (
                                await self.control_loop.resolve_human_approval(
                                    user_id=user_id,
                                    session_id=session_id,
                                    approved=approved,
                                    request_id=request_id,
                                )
                            )
                            if (
                                approved
                                and control_loop_resolved
                                and pending_control_request
                            ):
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
                                    constraints=_normalize_constraints(
                                        (pending_control_request.get("plan") or {}).get(
                                            "constraints"
                                        )
                                    ),
                                )
                        target_session_id = result.session_id if result else session_id
                        status = (
                            "resolved"
                            if result or control_loop_resolved
                            else "not_found"
                        )
                        await self._emit_session_event(
                            target_session_id,
                            source="tools.approval",
                            status=status,
                            message=f"Approval {request_id}: {'approved' if approved else 'denied'}",
                            user_id=user_id,
                        )

            except WebSocketDisconnect:
                pass
            except Exception as e:
                self.audit_logger.log_error(
                    error=str(e),
                    user_id=user_id,
                    session_id=session_id,
                    context={"endpoint": "websocket"},
                )
            finally:
                await self.manager.abort(session_id)
                self.manager.disconnect(session_id)
                self.audit_logger.log(
                    event_type=AuditEventType.SESSION_END,
                    user_id=user_id,
                    session_id=session_id,
                    action="disconnect",
                    result="success",
                )
                # Fire disconnect system event for cron jobs
                asyncio.create_task(
                    get_scheduler().fire_system_event("disconnect", {
                        "user_id": user_id, "session_id": session_id,
                    }),
                    name=f"sys:disconnect:{session_id}",
                )

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

            decision = await self._select_route_for_message(
                session_id=session_id,
                user_id=user_id,
                message=message,
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
                    message,
                    [],
                    request_id,
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
    ) -> None:
        try:
            current_browser_error = await self._current_browser_runtime_error(goal)
            if current_browser_error:
                self.transcript.append(
                    session_id,
                    "assistant",
                    current_browser_error,
                    user_id=user_id,
                    request_id=request_id,
                    metadata={
                        "type": "control_loop",
                        "success": False,
                        "error": "desktop_bridge_unavailable",
                    },
                )
                await self.manager.send_json(
                    session_id,
                    ev_chat_done(current_browser_error, request_id, aborted=False),
                )
                return

            result = await self.control_loop.run(
                goal=goal,
                user_id=user_id,
                constraints=constraints,
                session_id=session_id,
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

        decision = await self._select_route_for_message(
            session_id=session_id,
            user_id=user_id,
            message=message,
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
                goal=message,
                constraints=[],
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

    async def _run_control_loop_http(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
    ):
        current_browser_error = await self._current_browser_runtime_error(goal)
        if current_browser_error:
            return ExecutionResult(
                request_id=f"http_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                user_id=user_id,
                final_text=current_browser_error,
                success=False,
                metadata={"error": "desktop_bridge_unavailable"},
            )
        return await self.control_loop.run(
            goal=goal,
            user_id=user_id,
            constraints=constraints,
            session_id=session_id,
        )

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
        agent_name: Optional[str] = None,
    ) -> None:
        event = ev_system_event(
            source=source,
            status=status,
            message=message,
            run_id=run_id,
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


def _normalize_constraints(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def create_gateway() -> GatewayServer:
    return GatewayServer()
