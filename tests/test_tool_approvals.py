from types import SimpleNamespace
import time

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
from src.tools import current_tab as current_tab_module
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
    assert captured["state"] == "pending"
    assert captured["scope"] == "single"
    assert captured["tool_pattern"] == "run_shell"
    assert captured["propagate_to_subagents"] is False
    assert isinstance(captured["expires_at"], float)


@pytest.mark.asyncio
async def test_tool_policy_reuses_session_scoped_approval(tmp_path):
    engine = ToolPolicyEngine()
    notifications = []

    async def notifier(payload):
        notifications.append(payload)
        engine.resolve_approval(
            payload["request_id"],
            True,
            "approved for session",
            scope="session",
            tool_pattern="run_shell",
            path_scope=str(tmp_path),
            expires_at=time.time() + 60,
            propagate_to_subagents=True,
        )

    engine.set_notifier(notifier)

    approved, reason, first_request_id = await engine.request_approval_with_id(
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo first", "cwd": str(tmp_path)},
        session_id="session-1",
        reason="shell commands need approval",
    )
    assert approved is True
    assert reason == "approved for session"

    reused_approved, reused_reason, reused_request_id = await engine.request_approval_with_id(
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo second", "cwd": str(tmp_path)},
        session_id="session-1",
        reason="shell commands need approval",
    )
    assert reused_approved is True
    assert reused_request_id == first_request_id
    assert "reused approved approval" in reused_reason
    assert len(notifications) == 1


def test_tool_policy_propagates_session_scoped_approval():
    engine = ToolPolicyEngine()
    approval = engine.create_approval_request(
        request_id="req-parent",
        tool_name="write_file",
        agent_name="boiled_claw",
        args={"path": "/tmp/demo.txt"},
        session_id="session-parent",
        reason="file writes need approval",
    )
    engine.resolve_approval(
        approval.request_id,
        True,
        "approved for session",
        scope="session",
        tool_pattern="write_file",
        path_scope="/tmp",
        expires_at=time.time() + 60,
        propagate_to_subagents=True,
    )

    propagated = engine.propagate_approvals_to_session(
        source_session_id="session-parent",
        target_session_id="session-child",
        agent_name="web_researcher",
    )

    assert len(propagated) == 1
    assert propagated[0]["state"] == "propagated"
    assert propagated[0]["session_id"] == "session-child"
    assert propagated[0]["source_request_id"] == "req-parent"
    assert propagated[0]["propagate_to_subagents"] is True


def test_tool_policy_marks_expired_approvals():
    engine = ToolPolicyEngine()
    engine.create_approval_request(
        request_id="req-expired",
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo hi"},
        session_id="session-1",
        reason="shell commands need approval",
        state="approved",
        scope="session",
        expires_at=time.time() - 1,
    )

    expired = engine.cleanup_expired()

    assert expired == 1
    approvals = engine.list_approvals(session_id="session-1", state="expired", include_expired=True)
    assert len(approvals) == 1
    assert approvals[0]["request_id"] == "req-expired"
    assert approvals[0]["state"] == "expired"


def test_tool_policy_query_approvals_supports_search_and_pagination(tmp_path):
    engine = ToolPolicyEngine()
    first = engine.create_approval_request(
        request_id="req-save",
        tool_name="write_file",
        agent_name="boiled_claw",
        args={"path": str(tmp_path / "save.txt")},
        session_id="session-1",
        reason="save note",
    )
    engine.resolve_approval(
        first.request_id,
        True,
        "approved save",
        scope="session",
        history_metadata={"actor_user_id": "alice", "source": "http"},
    )
    second = engine.create_approval_request(
        request_id="req-query",
        tool_name="write_file",
        agent_name="boiled_claw",
        args={"path": str(tmp_path / "query.txt")},
        session_id="session-1",
        reason="query note",
    )
    engine.resolve_approval(second.request_id, False, "denied query")

    result = engine.query_approvals(
        session_id="session-1",
        state="all",
        include_expired=True,
        q="approved save",
        page=1,
        page_size=1,
    )

    assert result["pagination"]["total"] == 1
    assert result["approvals"][0]["request_id"] == "req-save"
    assert result["approvals"][0]["history"][-1]["metadata"]["actor_user_id"] == "alice"


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
    assert result["intent"] == "unknown"
    assert result["risk"] == "medium"


@pytest.mark.asyncio
async def test_run_shell_returns_intent_metadata(monkeypatch):
    engine = ToolPolicyEngine()
    captured = {}

    async def notifier(payload):
        captured.update(payload)
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

    result = await shell_module.run_shell("rg approvals tests", tool_context=tool_context)

    assert result["return_code"] == 0
    assert result["intent"] == "search"
    assert result["risk"] == "low"
    assert captured["args"]["shell_intent"]["category"] == "search"
    assert captured["args"]["shell_ast"]["executable_basename"] == "rg"


@pytest.mark.asyncio
async def test_run_shell_blocks_shell_wrapper(monkeypatch):
    monkeypatch.setattr(
        shell_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=False),
    )

    result = await shell_module.run_shell('bash -lc "echo hi"')

    assert result["return_code"] == -1
    assert "Shell wrapper" in result["error"]


@pytest.mark.asyncio
async def test_run_shell_blocks_redirection(monkeypatch):
    monkeypatch.setattr(
        shell_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=False),
    )

    result = await shell_module.run_shell("echo hi > out.txt")

    assert result["return_code"] == -1
    assert "redirection" in result["error"]


@pytest.mark.asyncio
async def test_run_shell_blocks_compact_inline_eval(monkeypatch):
    monkeypatch.setattr(
        shell_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=False),
    )

    result = await shell_module.run_shell('python3 -c"print(1)"')

    assert result["return_code"] == -1
    assert "Inline interpreter evaluation" in result["error"]


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
                intent="inspect",
                risk="low",
                summary="Inspection command via echo",
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
    assert result["intent"] == "inspect"
    assert result["risk"] == "low"
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
            seen["navigate_visible"] = request.visible
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
        visible=True,
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["title"] == "bridge title"
    assert seen["navigate_approval_token"]
    assert seen["navigate_visible"] is True
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
async def test_get_browser_session_keeps_visible_session_until_explicit_override(monkeypatch):
    starts = []
    closes = []

    class FakeSession:
        def __init__(self):
            self.headless = True
            self.page = object()

        @property
        def is_alive(self):
            return True

        async def start(self, headless=True):
            self.headless = headless
            starts.append(headless)

        async def close(self):
            closes.append(self.headless)

    monkeypatch.setattr(browser_module, "BrowserSession", FakeSession)
    monkeypatch.setattr(browser_module, "_browser_session", None)
    monkeypatch.setattr(
        browser_module,
        "get_settings",
        lambda: SimpleNamespace(browser_headless=True),
    )

    visible_session = await browser_module.get_browser_session(visible=True)
    reused_session = await browser_module.get_browser_session()
    hidden_session = await browser_module.get_browser_session(visible=False)

    assert visible_session is reused_session
    assert hidden_session is not visible_session
    assert starts == [False, True]
    assert closes == [False]


@pytest.mark.asyncio
async def test_maybe_activate_visible_browser_brings_window_to_front(monkeypatch):
    import src.bridges.desktop_bridge_client as desktop_bridge_client_module

    calls = []

    class FakePage:
        def __init__(self):
            self.brought_to_front = False

        async def bring_to_front(self):
            self.brought_to_front = True

    class FakeSession:
        def __init__(self):
            self.headless = False
            self.page = FakePage()

    class FakeDesktopClient:
        async def focus_window(self, request):
            calls.append(request)
            from src.desktop import DesktopControlResult

            return DesktopControlResult(ok=True)

    monkeypatch.setattr(
        desktop_bridge_client_module,
        "get_desktop_client",
        lambda: FakeDesktopClient(),
    )

    session = FakeSession()
    await browser_module._maybe_activate_visible_browser(session, title="boiled-claw Control UI")

    assert session.page.brought_to_front is True
    assert len(calls) == 1
    assert calls[0].title == "boiled-claw Control UI"
    assert calls[0].app_name is None


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
async def test_current_tab_info_uses_host_bridge_when_enabled(monkeypatch):
    emitted = []

    class FakeClient:
        async def current_tab_info(self, request):
            from src.bridges.host_bridge_schema import HostCurrentTabInfoResult

            return HostCurrentTabInfoResult(
                ok=True,
                tab_id=21,
                window_id=4,
                url="https://example.com",
                title="Example",
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(
        current_tab_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(current_tab_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await current_tab_module.current_tab_info(tool_context=tool_context)

    assert result["success"] is True
    assert result["tab_id"] == 21
    assert emitted[0][1]["tool_name"] == "host.current_tab.info"
    assert emitted[1][1]["tool_name"] == "host.current_tab.info"


@pytest.mark.asyncio
async def test_current_tab_navigate_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    seen = {}
    emitted = []

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def current_tab_navigate(self, request):
            seen["approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostCurrentTabNavigateResult

            return HostCurrentTabNavigateResult(
                ok=True,
                tab_id=21,
                window_id=4,
                url=request.url,
                title="Search",
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    engine.set_notifier(notifier)
    monkeypatch.setattr(current_tab_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(
        current_tab_module,
        "get_settings",
        lambda: SimpleNamespace(host_bridge_enabled=True),
    )
    monkeypatch.setattr(current_tab_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await current_tab_module.current_tab_navigate(
        "https://www.google.com/search?q=tokyo+pollen",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["title"] == "Search"
    assert seen["approval_token"]
    assert emitted[0][1]["tool_name"] == "host.current_tab.navigate"
    assert emitted[1][1]["tool_name"] == "host.current_tab.navigate"


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
