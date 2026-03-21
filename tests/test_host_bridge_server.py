"""
Host Bridge MCP server tests.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import src.config.settings as settings_module
from src.mcp_servers import host_bridge_server as server_module


ROOT = Path(__file__).resolve().parents[1]


class TestHostBridgeTools:
    @pytest.fixture
    def mcp(self):
        from src.mcp_servers.host_bridge_server import create_server
        return create_server()

    def _text(self, result) -> str:
        return "".join(c.text for c in result if hasattr(c, "text"))

    @pytest.mark.asyncio
    async def test_tools_list(self, mcp):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "ping",
            "capabilities.list",
            "host.shell.run",
            "host.file.read",
            "host.file.write",
            "host.file.list",
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

    @pytest.mark.asyncio
    async def test_ping(self, mcp):
        result = await mcp.call_tool("ping", {})
        text = self._text(result)
        assert "host-bridge" in text
        assert "v1" in text

    @pytest.mark.asyncio
    async def test_capabilities_list(self, mcp):
        result = await mcp.call_tool("capabilities.list", {})
        text = self._text(result)
        assert "host.shell.run" in text
        assert "host.file.read" in text
        assert "host.file.write" in text
        assert "host.file.list" in text
        assert "host.browser.navigate" in text
        assert "host.browser.click" in text
        assert "host.browser.fill" in text
        assert "host.browser.press" in text
        assert "host.browser.extract_text" in text
        assert "host.browser.screenshot" in text
        assert "host.control_ui_chat.send_message" in text
        assert "host.current_tab.info" in text
        assert "host.current_tab.navigate" in text
        assert "host.current_tab.click" in text
        assert "host.current_tab.fill" in text
        assert "host.current_tab.extract_text" in text

    def test_create_server_blocks_remote_bind_by_default(self, monkeypatch):
        from src.mcp_servers.host_bridge_server import create_server

        monkeypatch.setenv("BRIDGE_ALLOW_REMOTE_BIND", "false")
        settings_module.reset_settings()
        with pytest.raises(ValueError):
            create_server(host="0.0.0.0")

    def test_create_server_allows_remote_bind_when_enabled(self, monkeypatch):
        from src.mcp_servers.host_bridge_server import create_server

        monkeypatch.setenv("BRIDGE_ALLOW_REMOTE_BIND", "true")
        settings_module.reset_settings()
        mcp = create_server(host="0.0.0.0")
        assert mcp.settings.host == "0.0.0.0"
        assert mcp.settings.transport_security.enable_dns_rebinding_protection is False

    @pytest.mark.asyncio
    async def test_host_shell_run_success(self, mcp):
        result = await mcp.call_tool(
            "host.shell.run",
            {
                "request_id": "req-1",
                "session_id": "sess-1",
                "user_id": "user-1",
                "agent_name": "pytest",
                "command": "echo host-bridge",
            },
        )
        text = self._text(result)
        assert "host-bridge" in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_shell_run_blocked_pattern(self, mcp):
        result = await mcp.call_tool(
            "host.shell.run",
            {
                "request_id": "req-2",
                "session_id": "sess-2",
                "user_id": "user-2",
                "agent_name": "pytest",
                "command": "curl http://example.com",
            },
        )
        text = self._text(result)
        assert "blocked" in text.lower()

    @pytest.fixture(autouse=False)
    def _allow_tmp(self, tmp_path, monkeypatch):
        """Add tmp_path to FILE_WORKSPACE_PATHS so file ops are permitted."""
        import src.security.policy as policy_module
        current = os.environ.get("FILE_WORKSPACE_PATHS", "")
        monkeypatch.setenv(
            "FILE_WORKSPACE_PATHS",
            f"{current},{tmp_path}" if current else str(tmp_path),
        )
        settings_module.reset_settings()
        policy_module._security_policy = None
        yield
        policy_module._security_policy = None

    @pytest.mark.asyncio
    async def test_host_file_read_success(self, mcp, tmp_path, _allow_tmp):
        target = tmp_path / "note.txt"
        target.write_text("hello bridge", encoding="utf-8")
        result = await mcp.call_tool(
            "host.file.read",
            {
                "request_id": "req-read-1",
                "session_id": "sess-read-1",
                "user_id": "user-read-1",
                "agent_name": "pytest",
                "path": str(target),
            },
        )
        text = self._text(result)
        assert "hello bridge" in text

    @pytest.mark.asyncio
    async def test_host_file_write_success(self, mcp, tmp_path, _allow_tmp):
        target = tmp_path / "written.txt"
        result = await mcp.call_tool(
            "host.file.write",
            {
                "request_id": "req-write-1",
                "session_id": "sess-write-1",
                "user_id": "user-write-1",
                "agent_name": "pytest",
                "path": str(target),
                "content": "bridge write",
            },
        )
        text = self._text(result)
        assert "true" in text.lower()
        assert target.read_text(encoding="utf-8") == "bridge write"

    @pytest.mark.asyncio
    async def test_host_file_list_success(self, mcp, tmp_path, _allow_tmp):
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")
        result = await mcp.call_tool(
            "host.file.list",
            {
                "request_id": "req-list-1",
                "session_id": "sess-list-1",
                "user_id": "user-list-1",
                "agent_name": "pytest",
                "path": str(tmp_path),
            },
        )
        text = self._text(result)
        assert "a.txt" in text
        assert "b.txt" in text

    @pytest.mark.asyncio
    async def test_host_browser_navigate_success(self, mcp, monkeypatch):
        seen = {}

        async def fake_navigate(url, wait_for="load", timeout=30000, visible=None, tool_context=None):
            seen["visible"] = visible
            return {
                "url": url,
                "title": "Bridge Example",
                "status": 200,
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_navigate_local", fake_navigate)
        result = await mcp.call_tool(
            "host.browser.navigate",
            {
                "request_id": "req-browser-nav",
                "session_id": "sess-browser-nav",
                "user_id": "user-browser-nav",
                "agent_name": "pytest",
                "url": "https://example.com",
                "visible": True,
            },
        )
        text = self._text(result)
        assert "Bridge Example" in text
        assert '"ok": true' in text.lower()
        assert seen["visible"] is True

    @pytest.mark.asyncio
    async def test_host_browser_extract_text_success(self, mcp, monkeypatch):
        async def fake_extract(selector=None, tool_context=None):
            return {
                "text": "bridge text",
                "selector": selector or "body",
                "length": 11,
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_extract_text_local", fake_extract)
        result = await mcp.call_tool(
            "host.browser.extract_text",
            {
                "request_id": "req-browser-text",
                "session_id": "sess-browser-text",
                "user_id": "user-browser-text",
                "agent_name": "pytest",
                "selector": "main",
            },
        )
        text = self._text(result)
        assert "bridge text" in text
        assert "main" in text

    @pytest.mark.asyncio
    async def test_host_browser_click_success(self, mcp, monkeypatch):
        async def fake_click(selector, timeout=30000, tool_context=None):
            return {
                "selector": selector,
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_click_local", fake_click)
        result = await mcp.call_tool(
            "host.browser.click",
            {
                "request_id": "req-browser-click",
                "session_id": "sess-browser-click",
                "user_id": "user-browser-click",
                "agent_name": "pytest",
                "selector": "textarea",
            },
        )
        text = self._text(result)
        assert "textarea" in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_browser_fill_success(self, mcp, monkeypatch):
        async def fake_fill(selector, text, timeout=30000, tool_context=None):
            return {
                "selector": selector,
                "text_length": len(text),
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_fill_local", fake_fill)
        result = await mcp.call_tool(
            "host.browser.fill",
            {
                "request_id": "req-browser-fill",
                "session_id": "sess-browser-fill",
                "user_id": "user-browser-fill",
                "agent_name": "pytest",
                "selector": "textarea",
                "text": "Hello World",
            },
        )
        text = self._text(result)
        assert "textarea" in text
        assert '"text_length": 11' in text

    @pytest.mark.asyncio
    async def test_host_browser_press_success(self, mcp, monkeypatch):
        async def fake_press(key, selector=None, timeout=30000, tool_context=None):
            return {
                "key": key,
                "selector": selector,
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_press_local", fake_press)
        result = await mcp.call_tool(
            "host.browser.press",
            {
                "request_id": "req-browser-press",
                "session_id": "sess-browser-press",
                "user_id": "user-browser-press",
                "agent_name": "pytest",
                "key": "Enter",
                "selector": "textarea",
            },
        )
        text = self._text(result)
        assert "Enter" in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_browser_screenshot_success(self, mcp, monkeypatch, tmp_path):
        async def fake_screenshot(path=None, full_page=False, tool_context=None):
            return {
                "path": path or str(tmp_path / "capture.png"),
                "full_page": full_page,
                "success": True,
            }

        monkeypatch.setattr(server_module.browser_tools, "_browser_screenshot_local", fake_screenshot)
        result = await mcp.call_tool(
            "host.browser.screenshot",
            {
                "request_id": "req-browser-shot",
                "session_id": "sess-browser-shot",
                "user_id": "user-browser-shot",
                "agent_name": "pytest",
                "path": str(tmp_path / "capture.png"),
                "full_page": True,
            },
        )
        text = self._text(result)
        assert "capture.png" in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_control_ui_chat_send_message_success(self, mcp, monkeypatch):
        seen = {}

        async def fake_control_ui_chat(
            url,
            message,
            timeout_ms=90000,
            connect_timeout_ms=15000,
            stable_wait_ms=800,
            visible=True,
            tool_context=None,
        ):
            seen["visible"] = visible
            return {
                "url": url,
                "title": "boiled-claw Control UI",
                "message": message,
                "assistant_reply": "bridge operator reply",
                "connected": True,
                "agent_bubble_count": 1,
                "success": True,
            }

        monkeypatch.setattr(
            server_module.control_ui_chat_tools,
            "_control_ui_chat_send_message_local",
            fake_control_ui_chat,
        )
        result = await mcp.call_tool(
            "host.control_ui_chat.send_message",
            {
                "request_id": "req-control-ui-chat",
                "session_id": "sess-control-ui-chat",
                "user_id": "user-control-ui-chat",
                "agent_name": "pytest",
                "url": "http://localhost:18789/chat",
                "message": "Hello World",
                "visible": True,
            },
        )
        text = self._text(result)
        assert "bridge operator reply" in text
        assert '"ok": true' in text.lower()
        assert seen["visible"] is True

    @pytest.mark.asyncio
    async def test_host_current_tab_info_success(self, mcp, monkeypatch):
        async def fake_current_tab_info():
            return {
                "ok": True,
                "tab_id": 12,
                "window_id": 7,
                "url": "https://example.com",
                "title": "Example",
            }

        monkeypatch.setattr(server_module, "_current_tab_info_payload", fake_current_tab_info)
        monkeypatch.setattr(server_module, "current_tab_bridge_enabled", lambda: True)

        result = await mcp.call_tool(
            "host.current_tab.info",
            {
                "request_id": "req-current-tab-info",
                "session_id": "sess-current-tab-info",
                "user_id": "user-current-tab-info",
                "agent_name": "pytest",
            },
        )
        text = self._text(result)
        assert '"tab_id": 12' in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_current_tab_navigate_success(self, mcp, monkeypatch):
        async def fake_current_tab_navigate(request):
            assert request.url == "https://example.com/search?q=tokyo"
            return {
                "ok": True,
                "tab_id": 12,
                "window_id": 7,
                "url": request.url,
                "title": "Search",
            }

        monkeypatch.setattr(server_module, "_current_tab_navigate_payload", fake_current_tab_navigate)
        monkeypatch.setattr(server_module, "current_tab_bridge_enabled", lambda: True)

        result = await mcp.call_tool(
            "host.current_tab.navigate",
            {
                "request_id": "req-current-tab-nav",
                "session_id": "sess-current-tab-nav",
                "user_id": "user-current-tab-nav",
                "agent_name": "pytest",
                "url": "https://example.com/search?q=tokyo",
            },
        )
        text = self._text(result)
        assert "Search" in text
        assert '"ok": true' in text.lower()

    @pytest.mark.asyncio
    async def test_host_current_tab_extract_text_success(self, mcp, monkeypatch):
        async def fake_current_tab_extract(request):
            assert request.selector == "#content"
            return {
                "ok": True,
                "selector": "#content",
                "text": "tokyo pollen",
                "length": 12,
            }

        monkeypatch.setattr(server_module, "_current_tab_extract_text_payload", fake_current_tab_extract)
        monkeypatch.setattr(server_module, "current_tab_bridge_enabled", lambda: True)

        result = await mcp.call_tool(
            "host.current_tab.extract_text",
            {
                "request_id": "req-current-tab-text",
                "session_id": "sess-current-tab-text",
                "user_id": "user-current-tab-text",
                "agent_name": "pytest",
                "selector": "#content",
            },
        )
        text = self._text(result)
        assert "tokyo pollen" in text
        assert '"ok": true' in text.lower()


async def _send_stdio_requests(messages: list[dict]) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.mcp_servers.host_bridge_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=ROOT,
    )

    init_messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]
    payload = "\n".join(json.dumps(m) for m in init_messages + messages) + "\n"
    proc.stdin.write(payload.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    responses = []
    try:
        async with asyncio.timeout(10):
            raw = await proc.stdout.read()
    except asyncio.TimeoutError:
        proc.kill()
        raise

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
async def test_stdio_tools_list():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    tools_resp = next((r for r in responses if r.get("id") == 1), None)
    assert tools_resp is not None
    names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert names == {
        "ping",
        "capabilities.list",
        "host.shell.run",
        "host.file.read",
        "host.file.write",
        "host.file.list",
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


@pytest.mark.asyncio
async def test_stdio_call_host_shell_run():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "host.shell.run",
            "arguments": {
                "request_id": "req-stdio",
                "session_id": "sess-stdio",
                "user_id": "user-stdio",
                "agent_name": "pytest",
                "command": "echo ping",
            },
        }},
    ])
    resp = next((r for r in responses if r.get("id") == 2), None)
    assert resp is not None
    content = resp["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "ping" in text
