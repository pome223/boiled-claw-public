from types import SimpleNamespace

import pytest

import src.bridges.host_bridge_exec as bridge_exec_module
from src.security.tool_policy import ToolPolicyEngine
from src.tools import browser as browser_module
from src.tools import control_ui_chat as control_ui_chat_module


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status


class _FakeLocator:
    def __init__(self, page, selector, index=None):
        self.page = page
        self.selector = selector
        self.index = index

    def _items(self):
        if self.selector == "#statusText":
            return [self.page.status_text]
        if self.selector == "#connectBtn":
            return ["Connect"]
        if self.selector == "#messageInput":
            return [self.page.input_value]
        if self.selector == "#chatForm .send":
            return ["Send"]
        if self.selector == "#messages":
            return [self.page.messages_text()]
        if self.selector == "#messages .bubble.user":
            return list(self.page.user_messages)
        if self.selector == "#messages .bubble.agent":
            return list(self.page.agent_messages)
        if self.selector == "#approvalList .approve-btn":
            return ["Approve"] * self.page.pending_approvals
        return []

    async def count(self):
        return len(self._items())

    def nth(self, index):
        return _FakeLocator(self.page, self.selector, index=index)

    async def inner_text(self):
        items = self._items()
        if not items:
            raise RuntimeError(f"No element for selector: {self.selector}")
        index = 0 if self.index is None else self.index
        return items[index]

    async def is_enabled(self):
        if self.selector == "#messageInput":
            return self.page.input_enabled
        if self.selector == "#connectBtn":
            return self.page.status_text != "online"
        if self.selector == "#chatForm .send":
            return self.page.input_enabled
        return True

    async def click(self, timeout=30000):
        if self.selector == "#connectBtn":
            self.page.status_text = "online"
            return
        if self.selector == "#chatForm .send":
            message = self.page.input_value
            self.page.user_messages.append(message)
            self.page.input_enabled = False
            if self.page.requires_inner_approval:
                self.page.pending_approvals = 1
            else:
                self.page.agent_messages.append(self.page.next_reply)
                self.page.input_enabled = True
            return
        if self.selector == "#approvalList .approve-btn":
            if self.page.pending_approvals <= 0:
                raise RuntimeError("No pending approvals")
            self.page.pending_approvals -= 1
            self.page.agent_messages.append(self.page.next_reply)
            self.page.input_enabled = True
            return
        raise RuntimeError(f"Unexpected click selector: {self.selector}")

    async def fill(self, text, timeout=30000):
        if self.selector != "#messageInput":
            raise RuntimeError(f"Unexpected fill selector: {self.selector}")
        self.page.input_value = text


class _FakePage:
    def __init__(self):
        self.url = ""
        self.title_text = "boiled-claw Control UI"
        self.status_text = "offline"
        self.input_value = ""
        self.input_enabled = True
        self.user_messages = []
        self.agent_messages = []
        self.next_reply = "Hello! I am boiled-claw, your personal AI assistant."
        self.pending_approvals = 0
        self.requires_inner_approval = False

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def goto(self, url, wait_until="load", timeout=30000):
        self.url = url
        return _FakeResponse(status=200)

    async def title(self):
        return self.title_text

    def messages_text(self):
        return "\n".join(
            [*self.user_messages, *self.agent_messages]
        )


class _FakeSession:
    def __init__(self, page):
        self.page = page


@pytest.mark.asyncio
async def test_control_ui_chat_send_message_local_success(monkeypatch):
    page = _FakePage()
    async def fake_get_browser_session():
        return _FakeSession(page)

    monkeypatch.setattr(browser_module, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser_module, "_validate_url", lambda url: (True, None))
    monkeypatch.setattr(browser_module, "get_browser_session", fake_get_browser_session)

    result = await control_ui_chat_module._control_ui_chat_send_message_local(
        "http://localhost:18789/chat",
        "Hello World",
    )

    assert result["success"] is True
    assert result["connected"] is True
    assert result["assistant_reply"] == page.next_reply
    assert page.user_messages == ["Hello World"]


@pytest.mark.asyncio
async def test_control_ui_chat_send_message_local_auto_approves_inner_requests(monkeypatch):
    page = _FakePage()
    page.requires_inner_approval = True

    async def fake_get_browser_session():
        return _FakeSession(page)

    monkeypatch.setattr(browser_module, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser_module, "_validate_url", lambda url: (True, None))
    monkeypatch.setattr(browser_module, "get_browser_session", fake_get_browser_session)

    result = await control_ui_chat_module._control_ui_chat_send_message_local(
        "http://localhost:18789/chat",
        "東京の今日の天気は？",
    )

    assert result["success"] is True
    assert result["assistant_reply"] == page.next_reply
    assert page.pending_approvals == 0


@pytest.mark.asyncio
async def test_control_ui_chat_send_message_rejects_non_chat_path(monkeypatch):
    monkeypatch.setattr(browser_module, "_validate_url", lambda url: (True, None))

    result = await control_ui_chat_module._control_ui_chat_send_message_local(
        "http://localhost:18789/",
        "Hello World",
    )

    assert result["success"] is False
    assert "/chat" in result["error"]


@pytest.mark.asyncio
async def test_control_ui_chat_send_message_uses_host_bridge_when_enabled(monkeypatch):
    engine = ToolPolicyEngine()
    emitted = []
    seen = {}

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    class FakeClient:
        async def send_control_ui_chat_message(self, request):
            seen["approval_token"] = request.approval_token
            from src.bridges.host_bridge_schema import HostControlUiChatSendMessageResult

            return HostControlUiChatSendMessageResult(
                ok=True,
                url=request.url,
                title="boiled-claw Control UI",
                message=request.message,
                assistant_reply="bridge reply",
                connected=True,
                agent_bubble_count=1,
            )

    async def _emit_start(**payload):
        emitted.append(("start", payload))

    async def _emit_result(**payload):
        emitted.append(("result", payload))

    monkeypatch.setattr(control_ui_chat_module.browser_tools, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(control_ui_chat_module, "get_settings", lambda: SimpleNamespace(host_bridge_enabled=True))
    monkeypatch.setattr(control_ui_chat_module, "get_host_bridge_client", lambda: FakeClient())
    monkeypatch.setattr(bridge_exec_module, "emit_tool_start", _emit_start)
    monkeypatch.setattr(bridge_exec_module, "emit_tool_result", _emit_result)
    engine.set_notifier(notifier)

    tool_context = SimpleNamespace(
        agent_name="browser_automator",
        session=SimpleNamespace(id="session-1"),
    )

    result = await control_ui_chat_module.control_ui_chat_send_message(
        "http://localhost:18789/chat",
        "Hello World",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["assistant_reply"] == "bridge reply"
    assert seen["approval_token"]
    assert emitted[0][1]["tool_name"] == "host.control_ui_chat.send_message"
    assert emitted[0][1]["metadata"]["executor"] == "host_bridge"
