"""Chrome extension relay for current-tab browser control."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional

import websockets

from src.config.settings import get_settings


class CurrentTabBridgeError(RuntimeError):
    """Raised when the current-tab extension bridge is unavailable or fails."""


class CurrentTabExtensionBridge:
    """WebSocket relay that accepts a single Chrome extension connection."""

    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server: Any = None
        self._server_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()
        self._client: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        client = self._client
        return client is not None and not getattr(client, "closed", False)

    async def ensure_started(self) -> None:
        async with self._server_lock:
            if self._server is not None:
                return
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=20,
            )

    async def _handle_connection(self, websocket: Any) -> None:
        async with self._client_lock:
            previous = self._client
            self._client = websocket

        if previous is not None and not getattr(previous, "closed", False):
            await previous.close()

        try:
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(message)
        finally:
            async with self._client_lock:
                if self._client is websocket:
                    self._client = None
            self._fail_pending("Current Tab extension disconnected")

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "").strip().lower()
        if message_type != "response":
            return

        request_id = str(message.get("request_id") or "").strip()
        if not request_id:
            return

        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        future.set_result(message)

    def _fail_pending(self, error: str) -> None:
        for request_id, future in list(self._pending.items()):
            if future.done():
                continue
            future.set_exception(CurrentTabBridgeError(error))
            self._pending.pop(request_id, None)

    async def call(
        self,
        action: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        await self.ensure_started()

        client = self._client
        if client is None or getattr(client, "closed", False):
            raise CurrentTabBridgeError(
                "Current Tab extension is not connected. Load the unpacked extension in Chrome."
            )

        request_id = f"ctab_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        request = {
            "type": "request",
            "request_id": request_id,
            "action": action,
            "payload": payload or {},
        }
        try:
            await client.send(json.dumps(request))
            response = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CurrentTabBridgeError(
                f"Current Tab extension timed out while handling '{action}'"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

        if not bool(response.get("ok")):
            error = str(response.get("error") or "").strip() or f"Current Tab action failed: {action}"
            raise CurrentTabBridgeError(error)

        result = response.get("result")
        return result if isinstance(result, dict) else {}


_current_tab_bridge: Optional[CurrentTabExtensionBridge] = None


def current_tab_bridge_enabled() -> bool:
    return bool(get_settings().current_tab_bridge_enabled)


def get_current_tab_extension_bridge() -> CurrentTabExtensionBridge:
    global _current_tab_bridge

    settings = get_settings()
    if _current_tab_bridge is None:
        _current_tab_bridge = CurrentTabExtensionBridge(
            host=settings.current_tab_bridge_host,
            port=settings.current_tab_bridge_port,
        )
    return _current_tab_bridge
