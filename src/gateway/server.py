"""
WebSocketゲートウェイサーバー

Typed event protocol:
  Client → Server: chat.send / chat.abort / presence.ping
  Server → Client: connected / chat.done / system.event / health.tick / cron.update
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
from src.tools.finance import is_direct_stock_price_query, stock_price
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import skill_list as tool_skill_list, skill_execute as tool_skill_execute
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier
from src.gateway.events import (
    ev_connected, ev_chat_done, ev_system_event,
    ev_health_tick, ev_cron_update,
)
from src.cron.scheduler import get_scheduler

_HEARTBEAT_INTERVAL = 30  # seconds
_AGENT_TIMEOUT = 120       # seconds


class ConnectionManager:
    """WebSocket 接続 + 実行中タスクの管理"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket

    def disconnect(self, session_id: str) -> None:
        self.active_connections.pop(session_id, None)

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
    """ゲートウェイサーバー"""

    def __init__(self):
        self.app = FastAPI(title="boiled-claw Gateway", version="0.2.0")
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
            public_prefixes = ("/health", "/chat-static", "/chat")
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

        # subagent → WS notifier
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

        # cron → WS broadcast notifier
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

        @self.app.on_event("shutdown")
        async def shutdown_event():
            set_subagent_notifier(None)
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            await get_scheduler().shutdown()

        # --- health / root ---

        @self.app.get("/")
        async def root():
            return {
                "name": "boiled-claw Gateway",
                "version": "0.2.0",
                "status": "running",
                "active_sessions": len(self.manager.active_connections),
                "skills_loaded": get_skills_report().get("loaded", False),
                "skills_count": get_skills_report().get("count", 0),
            }

        @self.app.get("/health")
        async def health():
            return {"status": "healthy"}

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

            result = await self._run_agent_http(user_id, session.id, message)
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

            await self.manager.connect(websocket, session_id)
            self.audit_logger.log(
                event_type=AuditEventType.SESSION_START,
                user_id=user_id,
                session_id=session_id,
                action="connect",
                result="success",
            )

            try:
                await self.manager.send_json(session_id, ev_connected(session_id, user_id))

                while True:
                    data = await websocket.receive_json()
                    # 新プロトコル: "event" フィールド / 旧プロトコル: "type" フィールド
                    event_name = data.get("event") or data.get("type", "")

                    if event_name in ("chat.send", "message"):
                        text = (data.get("text") or data.get("message") or "").strip()
                        request_id = data.get("request_id")
                        if text:
                            await self._start_agent_run(session_id, user_id, text, request_id)

                    elif event_name == "chat.abort":
                        request_id = data.get("request_id")
                        aborted = await self.manager.abort(session_id)
                        if not aborted:
                            # 既に完了 or 実行中タスクなし
                            await self.manager.send_json(
                                session_id,
                                ev_chat_done("", request_id, aborted=False),
                            )

                    elif event_name in ("presence.ping", "ping"):
                        await self.manager.send_json(
                            session_id,
                            ev_health_tick(len(self.manager.active_connections)),
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
        """既存のタスクを abort してから新しいエージェント実行タスクを起動する。"""
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
        """エージェントを実行し、結果を chat.done で送信する。abort 時は aborted=True。"""
        partial = ""
        try:
            # 株価ショートカット
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
            await self.manager.send_json(session_id, ev_chat_done(partial, request_id, aborted=False))

        except asyncio.CancelledError:
            await self.manager.send_json(
                session_id,
                ev_chat_done(partial, request_id, aborted=True),
            )
            raise

        except TimeoutError:
            msg = f"Agent timed out after {_AGENT_TIMEOUT} seconds."
            self.audit_logger.log_error(error=msg, user_id=user_id, session_id=session_id,
                                        context={"reason": "timeout"})
            await self.manager.send_json(session_id, ev_chat_done(msg, request_id, aborted=False))

        except Exception as exc:
            self.audit_logger.log_error(error=str(exc), user_id=user_id, session_id=session_id,
                                        context={"message": message})
            await self.manager.send_json(
                session_id,
                ev_chat_done(f"Error: {exc}", request_id, aborted=False),
            )

        finally:
            self.manager.clear_task(session_id)

    # HTTP 用エージェント実行（abort 不要）
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
