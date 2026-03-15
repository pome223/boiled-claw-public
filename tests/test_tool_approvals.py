from types import SimpleNamespace

import pytest

import src.bridges.host_bridge_exec as bridge_exec_module
import src.bridges.desktop_exec as desktop_exec_module
from src.desktop import (
    DesktopAxFindResult,
    DesktopRuntimeStatusResult,
    DesktopControlResult,
    DesktopWaitElementResult,
    DesktopWindowDescriptor,
    DesktopWindowsResult,
)
from src.security.policy import SecurityPolicy
from src.security.tool_policy import ToolPolicyEngine
from src.tools import browser as browser_module
from src.tools import desktop as desktop_module
from src.tools import file_manager as file_module
from src.tools import shell as shell_module


def test_browser_validate_url_blocks_loopback_by_default(monkeypatch):
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(browser_allow_loopback=False),
    )

    valid, reason = browser_module._validate_url("http://localhost:18789/chat")

    assert valid is False
    assert "blocked" in reason.lower()


def test_browser_validate_url_allows_loopback_when_enabled(monkeypatch):
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(browser_allow_loopback=True),
    )

    valid, reason = browser_module._validate_url("http://localhost:18789/chat")

    assert valid is True
    assert reason is None


@pytest.mark.asyncio
async def test_tool_policy_request_approval_round_trip():
    engine = ToolPolicyEngine()
    captured = {}

    async def notifier(payload):
        captured.update(payload)
        engine.resolve_approval(payload["request_id"], True, "approved in test")

    engine.set_notifier(notifier)

    approved, reason = await engine.request_approval(
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo hello"},
        session_id="session-1",
        reason="shell commands need approval",
    )

    assert approved is True
    assert reason == "approved in test"
    assert captured["tool_name"] == "run_shell"
    assert captured["agent_name"] == "boiled_claw"


@pytest.mark.asyncio
async def test_run_shell_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    engine.set_notifier(notifier)
    monkeypatch.setattr(shell_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        shell_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=False),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await shell_module.run_shell("echo approved", tool_context=tool_context)

    assert result["return_code"] == 0
    assert "approved" in result["stdout"]


@pytest.mark.asyncio
async def test_run_shell_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def run_shell(self, request):
            seen["shell_approval_token"] = request.approval_token
            return shell_module.HostShellRunResult(
                ok=True,
                stdout=f"bridge:{request.command}",
                stderr="",
                return_code=0,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(shell_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        shell_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(shell_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await shell_module.run_shell("echo bridge", tool_context=tool_context)

    assert result["return_code"] == 0
    assert result["stdout"] == "bridge:echo bridge"
    assert seen["shell_approval_token"]
    assert emitted[0][0] == "start"
    assert emitted[0][1]["tool_name"] == "host.shell.run"
    assert emitted[0][1]["metadata"]["executor"] == "host_bridge"
    assert emitted[1][0] == "result"
    assert emitted[1][1]["tool_name"] == "host.shell.run"
    assert emitted[1][1]["metadata"]["executor"] == "host_bridge"


@pytest.mark.asyncio
async def test_write_file_rejected_when_approval_denied(monkeypatch, tmp_path):
    engine = ToolPolicyEngine()

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], False, "rejected")

    engine.set_notifier(notifier)
    monkeypatch.setattr(file_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(file_module, "get_security_policy", lambda: SecurityPolicy())

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )
    target = tmp_path / "blocked.txt"

    result = await file_module.write_file(
        str(target),
        "blocked content",
        tool_context=tool_context,
    )

    assert "error" in result
    assert "denied" in result["error"]
    assert not target.exists()


@pytest.mark.asyncio
async def test_read_file_uses_host_bridge_when_enabled(monkeypatch):
    emitted = []

    class FakeClient:
        async def read_file(self, request):
            from src.bridges.host_bridge_schema import HostFileReadResult

            return HostFileReadResult(
                ok=True,
                path=request.path,
                content=f"bridge:{request.path}",
                size=len(f"bridge:{request.path}"),
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(
        file_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(file_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(file_module, "get_security_policy", lambda: SecurityPolicy())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await file_module.read_file("/tmp/demo.txt", tool_context=tool_context)

    assert result["content"] == "bridge:/tmp/demo.txt"
    assert emitted[0][1]["tool_name"] == "host.file.read"
    assert emitted[1][1]["tool_name"] == "host.file.read"


@pytest.mark.asyncio
async def test_write_file_uses_host_bridge_when_enabled(monkeypatch, tmp_path):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def write_file(self, request):
            seen["file_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostFileWriteResult

            return HostFileWriteResult(
                ok=True,
                path=request.path,
                size=len(request.content),
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(file_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        file_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(file_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(file_module, "get_security_policy", lambda: SecurityPolicy())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )
    target = tmp_path / "bridge.txt"

    result = await file_module.write_file(
        str(target),
        "bridge content",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["size"] == len("bridge content")
    assert seen["file_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.file.write"
    assert emitted[0][1]["metadata"]["executor"] == "host_bridge"
    assert emitted[1][1]["tool_name"] == "host.file.write"


@pytest.mark.asyncio
async def test_browser_navigate_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def navigate_browser(self, request):
            seen["navigate_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserNavigateResult

            return HostBrowserNavigateResult(
                ok=True,
                url=request.url,
                title="bridge title",
                status=200,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_navigate(
        "https://example.com",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["title"] == "bridge title"
    assert seen["navigate_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.navigate"
    assert emitted[0][1]["metadata"]["executor"] == "host_bridge"
    assert emitted[1][1]["tool_name"] == "host.browser.navigate"


@pytest.mark.asyncio
async def test_browser_extract_text_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def extract_browser_text(self, request):
            seen["extract_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserExtractTextResult

            return HostBrowserExtractTextResult(
                ok=True,
                text="bridge text",
                selector=request.selector or "body",
                length=11,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_extract_text(
        "main",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["text"] == "bridge text"
    assert seen["extract_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.extract_text"
    assert emitted[1][1]["tool_name"] == "host.browser.extract_text"


@pytest.mark.asyncio
async def test_browser_screenshot_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def screenshot_browser(self, request):
            seen["screenshot_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserScreenshotResult

            return HostBrowserScreenshotResult(
                ok=True,
                path=request.path or "/tmp/capture.png",
                full_page=request.full_page,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_screenshot(
        "/tmp/bridge.png",
        full_page=True,
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["path"] == "/tmp/bridge.png"
    assert seen["screenshot_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.screenshot"
    assert emitted[1][1]["tool_name"] == "host.browser.screenshot"


@pytest.mark.asyncio
async def test_browser_click_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def click_browser(self, request):
            seen["click_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserClickResult

            return HostBrowserClickResult(
                ok=True,
                selector=request.selector,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_click(
        "textarea",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["selector"] == "textarea"
    assert seen["click_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.click"
    assert emitted[1][1]["tool_name"] == "host.browser.click"


@pytest.mark.asyncio
async def test_browser_fill_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def fill_browser(self, request):
            seen["fill_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserFillResult

            return HostBrowserFillResult(
                ok=True,
                selector=request.selector,
                text_length=len(request.text),
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_fill(
        "textarea",
        "Hello World",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["text_length"] == 11
    assert seen["fill_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.fill"
    assert emitted[1][1]["tool_name"] == "host.browser.fill"


@pytest.mark.asyncio
async def test_browser_press_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def press_browser(self, request):
            seen["press_approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostBrowserPressResult

            return HostBrowserPressResult(
                ok=True,
                key=request.key,
                selector=request.selector,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(browser_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(browser_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await browser_module.browser_press(
        "Enter",
        selector="textarea",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["key"] == "Enter"
    assert seen["press_approval_token"]
    assert emitted[0][1]["tool_name"] == "host.browser.press"
    assert emitted[1][1]["tool_name"] == "host.browser.press"


@pytest.mark.asyncio
async def test_desktop_view_windows_uses_desktop_client(monkeypatch):
    emitted = []

    class FakeDesktopClient:
        async def windows(self, request):
            return DesktopWindowsResult(
                ok=True,
                windows=[
                    DesktopWindowDescriptor(
                        window_id="w1",
                        app_name="Safari",
                        title="Example",
                    )
                ],
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=False),
    )
    monkeypatch.setattr(desktop_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(desktop_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_view_windows(tool_context=tool_context)

    assert result["windows"][0]["app_name"] == "Safari"
    assert emitted[0][1]["tool_name"] == "desktop.view.windows"
    assert emitted[0][1]["metadata"]["executor"] == "local_desktop"


@pytest.mark.asyncio
async def test_desktop_ax_find_is_allowed_without_approval(monkeypatch):
    emitted = []

    class FakeDesktopClient:
        async def ax_find(self, request):
            return DesktopAxFindResult(
                ok=True,
                matched=True,
                target={"identifier": request.target.identifier},
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=False),
    )
    monkeypatch.setattr(desktop_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(desktop_exec_module, "emit_tool_result", _emit_result)

    result = await desktop_module.desktop_ax_find(
        app_name="Safari",
        window_id="w1",
        identifier="open-button",
    )

    assert result["matched"] is True
    assert result["target"]["identifier"] == "open-button"
    assert emitted[0][1]["tool_name"] == "desktop.ax.find"


@pytest.mark.asyncio
async def test_desktop_wait_element_is_allowed_without_approval(monkeypatch):
    emitted = []

    class FakeDesktopClient:
        async def wait_element(self, request):
            return DesktopWaitElementResult(
                ok=True,
                matched=True,
                target={"identifier": request.target.identifier},
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=False),
    )
    monkeypatch.setattr(desktop_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(desktop_exec_module, "emit_tool_result", _emit_result)

    result = await desktop_module.desktop_wait_element(
        app_name="Safari",
        window_id="w1",
        identifier="open-button",
    )

    assert result["matched"] is True
    assert result["target"]["identifier"] == "open-button"
    assert emitted[0][1]["tool_name"] == "desktop.wait.element"


@pytest.mark.asyncio
async def test_desktop_runtime_stop_is_allowed_without_approval(monkeypatch):
    class FakeDesktopClient:
        async def emergency_stop(self, request):
            return DesktopRuntimeStatusResult(
                ok=True,
                stopped=True,
                reason=request.reason,
                changed=True,
            )

    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=False),
    )

    result = await desktop_module.desktop_runtime_stop(reason="panic")

    assert result["success"] is True
    assert result["stopped"] is True
    assert result["reason"] == "panic"


@pytest.mark.asyncio
async def test_desktop_click_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def click(self, request):
            seen["approval_token"] = request.approval_token
            return DesktopControlResult(ok=True)

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )
    monkeypatch.setattr(desktop_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(desktop_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_control_click(10, 20, tool_context=tool_context)

    assert result["success"] is True
    assert seen["approval_token"]
    assert emitted[0][1]["tool_name"] == "desktop.control.click"
    assert emitted[0][1]["metadata"]["executor"] == "desktop_bridge"


@pytest.mark.asyncio
async def test_desktop_type_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def type_text(self, request):
            seen["approval_token"] = request.approval_token
            return DesktopControlResult(ok=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_control_type("hello", tool_context=tool_context)

    assert result["success"] is True
    assert seen["approval_token"]


@pytest.mark.asyncio
async def test_desktop_drag_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def drag(self, request):
            seen["approval_token"] = request.approval_token
            return DesktopControlResult(ok=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_control_drag(10, 20, 30, 40, tool_context=tool_context)

    assert result["success"] is True
    assert seen["approval_token"]


@pytest.mark.asyncio
async def test_desktop_scroll_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def scroll(self, request):
            seen["approval_token"] = request.approval_token
            return DesktopControlResult(ok=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_control_scroll(delta_y=-5, tool_context=tool_context)

    assert result["success"] is True
    assert seen["approval_token"]


@pytest.mark.asyncio
async def test_desktop_runtime_clear_stop_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def clear_stop(self, request):
            seen["approval_token"] = request.approval_token
            return DesktopRuntimeStatusResult(ok=True, stopped=False, changed=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_runtime_clear_stop(tool_context=tool_context)

    assert result["success"] is True
    assert seen["approval_token"]


@pytest.mark.asyncio
async def test_desktop_click_selector_passes_target(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def click(self, request):
            seen["target"] = request.target.model_dump()
            return DesktopControlResult(ok=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await desktop_module.desktop_control_click(
        app_name="Safari",
        window_id="w1",
        role="AXButton",
        identifier="open-button",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["target"]["identifier"] == "open-button"


@pytest.mark.asyncio
async def test_desktop_launch_and_focus_wait_for_approval(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeDesktopClient:
        async def launch_app(self, request):
            seen["launch_token"] = request.approval_token
            return DesktopControlResult(ok=True)

        async def focus_window(self, request):
            seen["focus_token"] = request.approval_token
            return DesktopControlResult(ok=True)

    engine.set_notifier(notifier)
    monkeypatch.setattr(desktop_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(desktop_module, "get_desktop_client", lambda: FakeDesktopClient())
    monkeypatch.setattr(
        desktop_module,
        "get_settings",
        lambda: SimpleNamespace(desktop_bridge_enabled=True),
    )

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    launch = await desktop_module.desktop_control_launch_app(
        app_name="Safari",
        tool_context=tool_context,
    )
    focus = await desktop_module.desktop_control_focus_window(
        window_id="w1",
        tool_context=tool_context,
    )

    assert launch["success"] is True
    assert focus["success"] is True
    assert seen["launch_token"]
    assert seen["focus_token"]
