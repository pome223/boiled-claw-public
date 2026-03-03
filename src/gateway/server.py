"""
WebSocketゲートウェイサーバー
OpenClaw のゲートウェイアーキテクチャを参考
"""

import asyncio
import json
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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
from src.tools.finance import stock_price
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import skill_list as tool_skill_list, skill_execute as tool_skill_execute
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier


class ConnectionManager:
    """WebSocket接続管理"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        """接続を追加"""
        await websocket.accept()
        self.active_connections[session_id] = websocket

    def disconnect(self, session_id: str):
        """接続を削除"""
        if session_id in self.active_connections:
            del self.active_connections[session_id]

    async def send_message(self, session_id: str, message: dict):
        """特定セッションにメッセージ送信"""
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_json(message)

    async def broadcast(self, message: dict, exclude: Optional[str] = None):
        """全接続にブロードキャスト"""
        for session_id, connection in self.active_connections.items():
            if session_id != exclude:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass


class GatewayServer:
    """ゲートウェイサーバー"""

    def __init__(self):
        self.app = FastAPI(title="boiled-claw Gateway", version="0.1.0")
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

        # CORS設定
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # 認証ミドルウェア
        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            api_key = self.settings.gateway_api_key
            if not api_key:
                return await call_next(request)
            # 認証不要のパス
            public_prefixes = ("/health", "/chat-static", "/chat")
            if any(request.url.path.startswith(p) for p in public_prefixes) or request.url.path == "/":
                return await call_next(request)
            # トークン確認 (ヘッダー or クエリパラメータ)
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

        async def _subagent_notifier(payload: Dict[str, Any]) -> None:
            session_id = payload.get("requester_session_id")
            message = payload.get("message")
            if not session_id or not message:
                return
            await self.manager.send_message(
                session_id,
                {
                    "type": "agent_message",
                    "message": message,
                    "source": "subagent",
                    "run_id": payload.get("run_id"),
                    "status": payload.get("status"),
                    "agent_name": payload.get("agent_name"),
                },
            )

        set_subagent_notifier(_subagent_notifier)

        self._setup_routes()

    def _setup_routes(self):
        """ルート設定"""

        @self.app.on_event("startup")
        async def startup_event():
            await ensure_skills_loaded()

        @self.app.on_event("shutdown")
        async def shutdown_event():
            set_subagent_notifier(None)

        @self.app.get("/")
        async def root():
            return {
                "name": "boiled-claw Gateway",
                "version": "0.1.0",
                "status": "running",
                "active_sessions": len(self.manager.active_connections),
                "skills_loaded": get_skills_report().get("loaded", False),
                "skills_count": get_skills_report().get("count", 0),
            }

        @self.app.get("/health")
        async def health():
            return {"status": "healthy"}

        @self.app.get("/skills")
        async def skills():
            await ensure_skills_loaded()
            detail = await tool_skill_list()
            report = get_skills_report()
            return {
                **report,
                "details": detail.get("skills", []),
            }

        @self.app.post("/skills/{skill_name}/execute")
        async def execute_skill(skill_name: str, payload: Dict[str, Any] | None = None):
            params = {}
            if payload and isinstance(payload.get("params"), dict):
                params = payload.get("params", {})
            result = await tool_skill_execute(skill_name, json.dumps(params, ensure_ascii=False))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "Skill execution failed"))
            return result

        @self.app.get("/sessions/{user_id}")
        async def list_sessions(user_id: str):
            response = await self.session_service.list_sessions(
                app_name="boiled-claw", user_id=user_id
            )
            sessions = response.sessions or []
            return {"sessions": [{"id": s.id} for s in sessions]}

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

        @self.app.get("/subagents/{session_id}")
        async def subagents_list_endpoint(session_id: str):
            return await self.subagent_manager.list_runs(requester_session_id=session_id)

        @self.app.post("/subagents/{run_id}/steer")
        async def subagents_steer_endpoint(run_id: str, payload: Dict[str, Any] | None = None):
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

        @self.app.get("/chat")
        async def chat_ui():
            return FileResponse(self.static_dir / "index.html")

        @self.app.websocket("/ws/{user_id}")
        async def websocket_endpoint(
            websocket: WebSocket,
            user_id: str,
            session_id: Optional[str] = Query(default=None),
            token: Optional[str] = Query(default=None),
        ):
            # WebSocket 認証
            if self.settings.gateway_api_key:
                if token != self.settings.gateway_api_key:
                    await websocket.close(code=4401, reason="Unauthorized")
                    return

            # 既存セッションの再利用または新規作成
            session = None
            if session_id:
                session = await self.session_service.get_session(
                    app_name="boiled-claw",
                    user_id=user_id,
                    session_id=session_id,
                )
            if session is None:
                session = await self.session_service.create_session(
                    app_name="boiled-claw",
                    user_id=user_id,
                )
            session_id = session.id

            await self.manager.connect(websocket, session_id)

            # 監査ログ
            self.audit_logger.log(
                event_type=AuditEventType.SESSION_START,
                user_id=user_id,
                session_id=session_id,
                action="connect",
                result="success",
            )

            try:
                # 接続確認メッセージ
                await self.manager.send_message(session_id, {
                    "type": "connected",
                    "session_id": session_id,
                    "user_id": user_id,
                })

                while True:
                    # メッセージ受信
                    data = await websocket.receive_json()

                    message_type = data.get("type", "message")

                    if message_type == "message":
                        await self._handle_message(
                            user_id=user_id,
                            session_id=session_id,
                            message=data.get("message", ""),
                        )
                    elif message_type == "ping":
                        await self.manager.send_message(session_id, {"type": "pong"})

            except WebSocketDisconnect:
                self.manager.disconnect(session_id)
                self.audit_logger.log(
                    event_type=AuditEventType.SESSION_END,
                    user_id=user_id,
                    session_id=session_id,
                    action="disconnect",
                    result="success",
                )
            except Exception as e:
                self.audit_logger.log_error(
                    error=str(e),
                    user_id=user_id,
                    session_id=session_id,
                    context={"endpoint": "websocket"},
                )
                self.manager.disconnect(session_id)

    async def _handle_message(self, user_id: str, session_id: str, message: str):
        """メッセージ処理"""
        # ユーザーメッセージ送信確認
        await self.manager.send_message(session_id, {
            "type": "user_message",
            "message": message,
        })

        # 株価クエリは専用ツールで即時処理（LLMループによる長時間待機を回避）
        if "株価" in message.lower():
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

            await self.manager.send_message(session_id, {
                "type": "agent_message",
                "message": text,
            })
            return

        # エージェント実行
        content = types.Content(
            role="user",
            parts=[types.Part(text=message)]
        )

        try:
            # ストリーミングレスポンス
            response_text = ""
            # 無限待ち防止: モデル/ツール呼び出しが長時間停止した場合は打ち切る
            async with asyncio.timeout(45):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
                    if event.is_final_response():
                        if event.content and event.content.parts:
                            for part in event.content.parts:
                                if part.text:
                                    response_text += part.text

            if not response_text.strip():
                response_text = (
                    "応答の生成に失敗しました。もう一度試すか、質問を少し具体化してください。"
                )

            # レスポンス送信
            await self.manager.send_message(session_id, {
                "type": "agent_message",
                "message": response_text,
            })

            # 監査ログ
            self.audit_logger.log_agent_message(
                agent_name="root_agent",
                message=response_text,
                user_id=user_id,
                session_id=session_id,
            )

        except TimeoutError:
            error_message = (
                "Agent timed out after 45 seconds. "
                "Please try again with a more specific query."
            )
            await self.manager.send_message(session_id, {
                "type": "error",
                "message": error_message,
            })
            self.audit_logger.log_error(
                error=error_message,
                user_id=user_id,
                session_id=session_id,
                context={"message": message, "reason": "timeout"},
            )
        except Exception as e:
            error_message = f"Error: {str(e)}"
            await self.manager.send_message(session_id, {
                "type": "error",
                "message": error_message,
            })
            self.audit_logger.log_error(
                error=str(e),
                user_id=user_id,
                session_id=session_id,
                context={"message": message},
            )

    def run(self, host: Optional[str] = None, port: Optional[int] = None):
        """サーバー起動"""
        import uvicorn
        uvicorn.run(
            self.app,
            host=host or self.settings.gateway_host,
            port=port or self.settings.gateway_port,
        )


def create_gateway() -> GatewayServer:
    """ゲートウェイインスタンスを作成"""
    return GatewayServer()
