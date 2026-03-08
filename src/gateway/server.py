"""
WebSocket Gateway Server

Typed Gateway Protocol v1:
  Client -> Server: chat.send / chat.inject / chat.abort / chat.history /
                    presence.ping / tools.approval
  Server -> Client: connected / chat.done / chat.token / chat.history /
                    system.event / health.tick / cron.update / tools.approval_request
"""

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pathlib import Path

from src.config.settings import get_settings
from src.agents.root_agent import root_agent
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
    normalize_client_event, make_request_id,
)
from src.gateway.transcript import get_transcript_store
from src.cron.scheduler import get_scheduler

_HEARTBEAT_INTERVAL = 30  # seconds
_AGENT_TIMEOUT = 120       # seconds


class ConnectionManager:
    """WebSocket connection + running task management"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # session_id -> user_id mapping for system event routing
        self._session_users: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, session_id: str, user_id: str = "") -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket
        if user_id:
            self._session_users[session_id] = user_id

    def disconnect(self, session_id: str) -> None:
        self.active_connections.pop(session_id, None)
        self._session_users.pop(session_id, None)

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
        self.app = FastAPI(title="boiled-claw Gateway", version="0.3.0")
        self.settings = get_settings()
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.manager = ConnectionManager()
        self.session_service = InMemorySessionService()
        self.subagent_manager = get_subagent_manager()
        self.runner = Runner(
            agent=root_agent,
            app_name="boiled-claw",
            session_service=self.session_service,
        )
        self.audit_logger = get_audit_logger()
        self.transcript = get_transcript_store()
        self.tool_policy = get_tool_policy_engine()
        self._heartbeat_task: Optional[asyncio.Task] = None

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
            event = ev_system_event(
                source="subagent",
                status=payload.get("status", ""),
                message=payload.get("message", ""),
                run_id=payload.get("run_id"),
                agent_name=payload.get("agent_name"),
            )
            await self.manager.send_json(session_id, event)

        set_subagent_notifier(_subagent_notifier)

        # cron -> WS broadcast notifier
        async def _cron_notifier(payload: Dict[str, Any]) -> None:
            event = ev_cron_update(
                job_id=payload.get("job_id", ""),
                status=payload.get("status", ""),
                message=payload.get("message", ""),
            )
            await self.manager.broadcast_json(event)

        self._cron_notifier_fn = _cron_notifier
        self._setup_routes()

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    def _setup_routes(self):

        @self.app.on_event("startup")
        async def startup_event():
            await ensure_skills_loaded()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="heartbeat"
            )
            scheduler = get_scheduler()
            scheduler.set_spawn_fn(self.subagent_manager.spawn)
            scheduler.set_notifier(self._cron_notifier_fn)
            scheduler.start()
            # Fire startup system event jobs
            await scheduler.fire_system_event("startup")

        @self.app.on_event("shutdown")
        async def shutdown_event():
            set_subagent_notifier(None)
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            await get_scheduler().shutdown()

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
        async def list_sessions(user_id: str):
            response = await self.session_service.list_sessions(
                app_name="boiled-claw", user_id=user_id
            )
            sessions = response.sessions or []
            return {"sessions": [{"id": s.id} for s in sessions]}

        # --- transcript / history ---

        @self.app.get("/sessions/{user_id}/{session_id}/history")
        async def get_session_history(
            user_id: str,
            session_id: str,
            limit: int = Query(default=100, ge=1, le=500),
            before: Optional[float] = Query(default=None),
        ):
            entries = self.transcript.get_history(session_id, limit=limit, before=before)
            return {
                "session_id": session_id,
                "entries": [e.to_dict() for e in entries],
                "count": len(entries),
            }

        @self.app.get("/transcript/sessions")
        async def list_transcript_sessions(limit: int = Query(default=50, ge=1, le=200)):
            return {"sessions": self.transcript.list_sessions(limit=limit)}

        # --- agent/run (HTTP) ---

        @self.app.post("/agent/run")
        async def run_agent_endpoint(payload: Dict[str, Any] | None = Body(default=None)):
            body = payload or {}
            user_id = str(body.get("user_id") or "api_user")
            message = str(body.get("message") or "").strip()
            session_id = body.get("session_id")

            if not message:
                raise HTTPException(status_code=400, detail="message is required")

            session = None
            if session_id:
                session = await self.session_service.get_session(
                    app_name="boiled-claw",
                    user_id=user_id,
                    session_id=str(session_id),
                )
            if session is None:
                session = await self.session_service.create_session(
                    app_name="boiled-claw", user_id=user_id,
                )

            # Record user message in transcript
            self.transcript.append(session.id, "user", message)

            result = await self._run_agent_http(user_id, session.id, message)

            # Record assistant response in transcript
            self.transcript.append(
                session.id, "assistant", result["message"],
                metadata={"type": result["type"]},
            )

            return {
                "ok": result["type"] == "agent_message",
                "type": result["type"],
                "response": result["message"],
                "user_id": user_id,
                "session_id": session.id,
            }

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
                    delivery_target=str(body.get("delivery_target") or "isolated"),
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

            session = None
            if session_id:
                session = await self.session_service.get_session(
                    app_name="boiled-claw",
                    user_id=user_id,
                    session_id=session_id,
                )
            if session is None:
                session = await self.session_service.create_session(
                    app_name="boiled-claw", user_id=user_id,
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
                    event_name = data.get("event", "")

                    if event_name == "chat.send":
                        text = (data.get("text") or "").strip()
                        request_id = data.get("request_id")
                        if text:
                            # Record user message in transcript
                            self.transcript.append(
                                session_id, "user", text,
                                request_id=request_id,
                            )
                            await self._start_agent_run(session_id, user_id, text, request_id)

                    elif event_name == "chat.inject":
                        text = (data.get("text") or "").strip()
                        role = data.get("role", "system")
                        request_id = data.get("request_id")
                        if text:
                            self.transcript.append(
                                session_id, "inject", text,
                                request_id=request_id,
                                metadata={"role": role},
                            )
                            await self.manager.send_json(
                                session_id,
                                ev_system_event(
                                    source="inject",
                                    status="ok",
                                    message=f"Injected {role} message into transcript",
                                ),
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
                        status = "resolved" if result else "not_found"
                        await self.manager.send_json(
                            session_id,
                            ev_system_event(
                                source="tools.approval",
                                status=status,
                                message=f"Approval {request_id}: {'approved' if approved else 'denied'}",
                            ),
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
                    request_id=request_id,
                )
                await self.manager.send_json(session_id, ev_chat_done(partial, request_id))
                return

            now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
            full_msg = f"[システム情報: 現在の日時は {now} です]\n\n{message}"
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
                request_id=request_id,
            )
            await self.manager.send_json(session_id, ev_chat_done(partial, request_id, aborted=False))

        except asyncio.CancelledError:
            # Persist partial + aborted flag to transcript
            self.transcript.append(
                session_id, "assistant", partial,
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
                request_id=request_id,
                metadata={"error": str(exc)},
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

        now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        full_msg = f"[システム情報: 現在の日時は {now} です]\n\n{message}"
        content = types.Content(role="user", parts=[types.Part(text=full_msg)])

        try:
            response_text = ""
            async with asyncio.timeout(45):
                async for event in self.runner.run_async(
                    user_id=user_id, session_id=session_id, new_message=content,
                ):
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                response_text += part.text

            if not response_text.strip():
                response_text = "応答の生成に失敗しました。もう一度試すか、質問を少し具体化してください。"

            self.audit_logger.log_agent_message(
                agent_name="root_agent", message=response_text,
                user_id=user_id, session_id=session_id,
            )
            return {"type": "agent_message", "message": response_text}

        except TimeoutError:
            msg = "Agent timed out after 45 seconds. Please try again with a more specific query."
            self.audit_logger.log_error(error=msg, user_id=user_id, session_id=session_id,
                                        context={"message": message, "reason": "timeout"})
            return {"type": "error", "message": msg}

        except Exception as exc:
            self.audit_logger.log_error(error=str(exc), user_id=user_id, session_id=session_id,
                                        context={"message": message})
            return {"type": "error", "message": f"Error: {exc}"}

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if self.manager.active_connections:
                tick = ev_health_tick(len(self.manager.active_connections))
                await self.manager.broadcast_json(tick)

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
