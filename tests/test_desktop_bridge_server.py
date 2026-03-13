"""
Desktop Bridge MCP server skeleton tests.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from src.desktop import FakeDesktopClient, DesktopWindowDescriptor


ROOT = Path(__file__).resolve().parents[1]


class TestDesktopBridgeTools:
    @pytest.fixture
    def mcp(self):
        from src.mcp_servers.desktop_bridge_server import create_server

        return create_server(desktop_client=FakeDesktopClient())

    def _text(self, result) -> str:
        return "".join(c.text for c in result if hasattr(c, "text"))

    @pytest.mark.asyncio
    async def test_tools_list(self, mcp):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "ping",
            "capabilities.list",
            "desktop.view.screenshot",
            "desktop.view.windows",
            "desktop.view.frontmost_app",
            "desktop.ax.find",
            "desktop.ax.snapshot",
            "desktop.control.click",
            "desktop.control.type",
            "desktop.control.launch_app",
            "desktop.control.focus_window",
            "desktop.control.hotkey",
            "desktop.control.drag",
        }

    @pytest.mark.asyncio
    async def test_capabilities_list(self, mcp):
        result = await mcp.call_tool("capabilities.list", {})
        text = self._text(result)
        assert "desktop.ax.find" in text
        assert "desktop.view.screenshot" in text
        assert "desktop.control.click" in text
        assert '"implemented": false' in text.lower()

    @pytest.mark.asyncio
    async def test_view_screenshot_is_not_implemented(self, mcp):
        result = await mcp.call_tool(
            "desktop.view.screenshot",
            {
                "request_id": "req-shot-1",
                "session_id": "sess-shot-1",
                "user_id": "user-shot-1",
                "agent_name": "pytest",
            },
        )
        text = self._text(result)
        assert '"ok": false' in text.lower()
        assert "not implemented" in text.lower()

    @pytest.mark.asyncio
    async def test_control_click_is_not_implemented(self, mcp):
        result = await mcp.call_tool(
            "desktop.control.click",
            {
                "request_id": "req-click-1",
                "session_id": "sess-click-1",
                "user_id": "user-click-1",
                "agent_name": "pytest",
                "x": 10,
                "y": 20,
            },
        )
        text = self._text(result)
        assert '"ok": false' in text.lower()
        assert "not implemented" in text.lower()

    @pytest.mark.asyncio
    async def test_server_uses_injected_desktop_client(self):
        from src.mcp_servers.desktop_bridge_server import create_server

        mcp = create_server(
            desktop_client=FakeDesktopClient(
                implemented={
                    "desktop.ax.find",
                    "desktop.view.windows",
                    "desktop.view.frontmost_app",
                    "desktop.control.launch_app",
                    "desktop.control.focus_window",
                },
                windows=[
                    DesktopWindowDescriptor(
                        window_id="w1",
                        app_name="Safari",
                        title="Injected",
                    )
                ],
                frontmost_app_name="Safari",
                frontmost_pid=42,
            )
        )

        windows = await mcp.call_tool(
            "desktop.view.windows",
            {
                "request_id": "req-injected-1",
                "session_id": "sess-injected-1",
                "user_id": "user-injected-1",
                "agent_name": "pytest",
            },
        )
        frontmost = await mcp.call_tool(
            "desktop.view.frontmost_app",
            {
                "request_id": "req-injected-2",
                "session_id": "sess-injected-2",
                "user_id": "user-injected-2",
                "agent_name": "pytest",
            },
        )
        found = await mcp.call_tool(
            "desktop.ax.find",
            {
                "request_id": "req-injected-find",
                "session_id": "sess-injected-find",
                "user_id": "user-injected-find",
                "agent_name": "pytest",
                "window_id": "w1",
                "title": "Injected",
            },
        )
        launch = await mcp.call_tool(
            "desktop.control.launch_app",
            {
                "request_id": "req-injected-3",
                "session_id": "sess-injected-3",
                "user_id": "user-injected-3",
                "agent_name": "pytest",
                "app_name": "Safari",
            },
        )
        focus = await mcp.call_tool(
            "desktop.control.focus_window",
            {
                "request_id": "req-injected-4",
                "session_id": "sess-injected-4",
                "user_id": "user-injected-4",
                "agent_name": "pytest",
                "window_id": "w1",
            },
        )

        windows_text = self._text(windows)
        frontmost_text = self._text(frontmost)
        found_text = self._text(found)
        launch_text = self._text(launch)
        focus_text = self._text(focus)
        assert '"ok": true' in windows_text.lower()
        assert "Safari" in windows_text
        assert '"ok": true' in frontmost_text.lower()
        assert '"pid": 42' in frontmost_text
        assert '"matched": true' in found_text.lower()
        assert '"ok": true' in launch_text.lower()
        assert "Safari" in launch_text
        assert '"ok": true' in focus_text.lower()
        assert "Injected" in focus_text


async def _send_stdio_requests(messages: list[dict]) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.mcp_servers.desktop_bridge_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=ROOT,
    )

    init_messages = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]
    payload = "\n".join(json.dumps(m) for m in init_messages + messages) + "\n"
    proc.stdin.write(payload.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    responses = []
    async with asyncio.timeout(10):
        raw = await proc.stdout.read()
    await proc.wait()

    for line in raw.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "id" in obj:
                responses.append(obj)
        except json.JSONDecodeError:
            pass

    return responses


@pytest.mark.asyncio
async def test_desktop_stdio_tools_list():
    responses = await _send_stdio_requests(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        ]
    )
    tools_resp = next((r for r in responses if r.get("id") == 1), None)
    assert tools_resp is not None
    names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert "desktop.view.windows" in names
    assert "desktop.control.drag" in names
