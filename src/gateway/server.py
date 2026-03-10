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
import hashlib
import json
from datetime import datetime
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
from src.control_loop.root_workflow import ControlLoop
from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
from src.security.audit import get_audit_logger, AuditEventType
from src.security.tool_policy import get_tool_policy_engine
from src.tools.finance import is_direct_stock_price_query, stock_price
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import skill_list as tool_skill_list, skill_execute as tool_skill_execute
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier
from src.gateway.protocol import (
    PROTOCOL_VERSION,
    ev_connected, ev_chat_done, ev_chat_history, ev_system_event,
    ev_health_tick, ev_cron_update, ev_tools_approval_request,
    ev_control_approval_request,
    normalize_client_event, validate_client_event,
)
from src.gateway.transcript import get_transcript_store
from src.cron.scheduler import get_scheduler

_HEARTBEAT_INTERVAL = 30  # seconds
_AGENT_TIMEOUT = 120       # seconds
_MAX_PENDING_PER_SESSION = 100
_MAX_PENDING_SESSIONS = 500


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
        self.tool_policy.set_notifier(self._approval_notifier_fn)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="heartbeat"
            )
        scheduler = get_scheduler()
        scheduler.set_spawn_fn(self.subagent_manager.spawn)
        scheduler.set_notifier(self._cron_notifier_fn)
        scheduler.start()
        await scheduler.fire_system_event("startup")

    async def _shutdown_gateway(self) -> None:
        set_subagent_notifier(None)
        self.tool_policy.set_notifier(None)
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None
        await get_scheduler().shutdown()

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
                "ok": result["type"] == "agent_message",
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
                resumed_result = await self._run_control_loop_http(
                    user_id=user_id,
                    session_id=session_id,
                    goal=pending.get("goal", ""),
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
                                await self._start_control_loop_run(
                                    session_id=session_id,
                                    user_id=user_id,
                                    goal=pending_control_request.get("goal", ""),
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
                quote = await stock_price(message)
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

            full_msg = self._compose_agent_message(session_id, message)
            content = types.Content(role="user", parts=[types.Part(text=full_msg)])

            async with asyncio.timeout(_AGENT_TIMEOUT):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
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
            quote = await stock_price(message)
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
        return {"type": "agent_message", "message": text}

    async def _run_control_loop_http(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
    ):
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
        if session is not None and resolved_user:
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
