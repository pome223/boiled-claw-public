"""
Desktop Bridge MCP server skeleton tests.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import src.config.settings as settings_module
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
            "desktop.runtime.status",
            "desktop.runtime.stop",
            "desktop.runtime.clear_stop",
            "desktop.view.screenshot",
            "desktop.view.windows",
            "desktop.wait.window",
            "desktop.view.frontmost_app",
            "desktop.ax.find",
            "desktop.wait.element",
            "desktop.ax.snapshot",
            "desktop.control.click",
            "desktop.control.type",
            "desktop.control.launch_app",
            "desktop.control.focus_window",
            "desktop.control.hotkey",
            "desktop.control.scroll",
            "desktop.control.drag",
        }

    def test_create_server_blocks_remote_bind_by_default(self, monkeypatch):
        from src.mcp_servers.desktop_bridge_server import create_server

        monkeypatch.setenv("BRIDGE_ALLOW_REMOTE_BIND", "false")
        settings_module.reset_settings()
        with pytest.raises(ValueError):
            create_server(host="0.0.0.0", desktop_client=FakeDesktopClient())

    def test_create_server_allows_remote_bind_when_enabled(self, monkeypatch):
        from src.mcp_servers.desktop_bridge_server import create_server

        monkeypatch.setenv("BRIDGE_ALLOW_REMOTE_BIND", "true")
        settings_module.reset_settings()
        mcp = create_server(host="0.0.0.0", desktop_client=FakeDesktopClient())
        assert mcp.settings.host == "0.0.0.0"
        assert mcp.settings.transport_security.enable_dns_rebinding_protection is False

    def test_create_server_allows_host_docker_internal_for_loopback_sse(self):
        from src.mcp_servers.desktop_bridge_server import create_server

        mcp = create_server(host="127.0.0.1", desktop_client=FakeDesktopClient())
        security = mcp.settings.transport_security
        assert security.enable_dns_rebinding_protection is True
        assert "host.docker.internal:*" in security.allowed_hosts
        assert "http://host.docker.internal:*" in security.allowed_origins

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
                    "desktop.wait.window",
                    "desktop.wait.element",
                    "desktop.control.scroll",
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
        runtime_status = await mcp.call_tool(
            "desktop.runtime.status",
            {
                "request_id": "req-runtime-status",
                "session_id": "sess-runtime-status",
                "user_id": "user-runtime-status",
                "agent_name": "pytest",
            },
        )
        runtime_stop = await mcp.call_tool(
            "desktop.runtime.stop",
            {
                "request_id": "req-runtime-stop",
                "session_id": "sess-runtime-stop",
                "user_id": "user-runtime-stop",
                "agent_name": "pytest",
                "reason": "stop from test",
            },
        )
        runtime_clear = await mcp.call_tool(
            "desktop.runtime.clear_stop",
            {
                "request_id": "req-runtime-clear",
                "session_id": "sess-runtime-clear",
                "user_id": "user-runtime-clear",
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
        waited_window = await mcp.call_tool(
            "desktop.wait.window",
            {
                "request_id": "req-injected-wait-window",
                "session_id": "sess-injected-wait-window",
                "user_id": "user-injected-wait-window",
                "agent_name": "pytest",
                "window_id": "w1",
            },
        )
        waited_element = await mcp.call_tool(
            "desktop.wait.element",
            {
                "request_id": "req-injected-wait-element",
                "session_id": "sess-injected-wait-element",
                "user_id": "user-injected-wait-element",
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
        scroll = await mcp.call_tool(
            "desktop.control.scroll",
            {
                "request_id": "req-injected-scroll",
                "session_id": "sess-injected-scroll",
                "user_id": "user-injected-scroll",
                "agent_name": "pytest",
                "delta_y": -3,
            },
        )

        windows_text = self._text(windows)
        frontmost_text = self._text(frontmost)
        runtime_status_text = self._text(runtime_status)
        runtime_stop_text = self._text(runtime_stop)
        runtime_clear_text = self._text(runtime_clear)
        found_text = self._text(found)
        waited_window_text = self._text(waited_window)
        waited_element_text = self._text(waited_element)
        launch_text = self._text(launch)
        focus_text = self._text(focus)
        scroll_text = self._text(scroll)
        assert '"ok": true' in windows_text.lower()
        assert "Safari" in windows_text
        assert '"ok": true' in frontmost_text.lower()
        assert '"pid": 42' in frontmost_text
        assert '"stopped": false' in runtime_status_text.lower()
        assert '"stopped": true' in runtime_stop_text.lower()
        assert '"stopped": false' in runtime_clear_text.lower()
        assert '"matched": true' in found_text.lower()
        assert '"matched": true' in waited_window_text.lower()
        assert '"matched": true' in waited_element_text.lower()
        assert '"ok": true' in launch_text.lower()
        assert "Safari" in launch_text
        assert '"ok": true' in focus_text.lower()
        assert "Injected" in focus_text
        assert '"ok": true' in scroll_text.lower()


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
    assert "desktop.runtime.status" in names
    assert "desktop.runtime.stop" in names
    assert "desktop.runtime.clear_stop" in names
    assert "desktop.view.windows" in names
    assert "desktop.wait.window" in names
    assert "desktop.wait.element" in names
    assert "desktop.control.scroll" in names
    assert "desktop.control.drag" in names
