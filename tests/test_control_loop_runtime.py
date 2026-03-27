import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.memory.base_memory_service import MemoryEntry, SearchMemoryResponse
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.sessions import InMemorySessionService
from google.genai import types

from src.control_loop import guarded_tools as guarded_tools_module
from src.control_loop.callbacks import policy_judge_callback
from src.control_loop.instructions import (
    build_executor_instruction,
    build_planner_instruction,
    build_verifier_instruction,
)
from src.control_loop.root_workflow import ControlLoop, planner_with_policy, verifier_with_hooks
import src.memory_lifecycle.candidate_store as candidate_store_module
from src.memory_lifecycle.adk_memory_service import PromotedMemoryService
from src.memory_lifecycle.candidate_store import CandidateStore
from src.memory_lifecycle.curator import Curator
from src.memory_lifecycle.memory_schema import (
    MemoryCandidate,
    MemoryType,
    OriginatorType,
    PromotedMemory,
    Provenance,
    SensitivityLevel,
)
from src.memory_lifecycle.promoted_store import PromotedMemoryStore
from src.runtime.state_keys import StateKeys
from src.tools.context import resolve_callback_context, resolve_tool_context


def _make_runtime_context():
    return SimpleNamespace(
        agent_name="executor",
        invocation_id="inv-1",
        _invocation_context=SimpleNamespace(
            app_name="boiled_claw_v2",
            user_id="user-1",
            session=SimpleNamespace(id="session-1"),
        ),
    )


def _make_readonly_context(state: dict[str, object]) -> ReadonlyContext:
    return ReadonlyContext(
        SimpleNamespace(
            session=SimpleNamespace(state=state),
            user_id="user-1",
            invocation_id="inv-1",
            user_content=None,
            run_config=None,
            agent=SimpleNamespace(name="planner"),
        )
    )


def test_resolve_tool_context_uses_invocation_context():
    resolved = resolve_tool_context(_make_runtime_context())

    assert resolved == {
        "agent_name": "executor",
        "session_id": "session-1",
        "user_id": "user-1",
        "app_name": "boiled_claw_v2",
        "invocation_id": "inv-1",
    }


def test_resolve_callback_context_falls_back_to_legacy_session():
    callback_context = SimpleNamespace(
        agent_name="verifier",
        invocation_id="inv-2",
        session=SimpleNamespace(id="legacy-session"),
    )

    resolved = resolve_callback_context(callback_context)

    assert resolved["agent_name"] == "verifier"
    assert resolved["session_id"] == "legacy-session"
    assert resolved["user_id"] == ""


@pytest.mark.asyncio
async def test_dynamic_instructions_render_custom_state_keys():
    ctx = _make_readonly_context(
        {
            StateKeys.TASK_GOAL: "Ship the ADK alignment",
            StateKeys.TASK_CONSTRAINTS: ["keep session state stable"],
            StateKeys.TEMP_REPAIR_PATCH: {"note": "retry verifier"},
            StateKeys.PLAN_APPROVED: {"plan_id": "plan-1"},
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.TEMP_EXECUTOR_OUTPUTS: {"summary": "done"},
        }
    )

    planner = await build_planner_instruction(ctx)
    executor = await build_executor_instruction(ctx)
    verifier = await build_verifier_instruction(ctx)

    assert "Ship the ADK alignment" in planner
    assert "{task:goal}" not in planner
    assert '"plan_id": "plan-1"' in executor
    assert "policy_approved" in executor
    assert '"summary": "done"' in verifier
    assert "current_tab.info / current_tab.navigate / current_tab.extract_text" in planner
    assert "Prefer current_tab.info / current_tab.navigate / current_tab.extract_text" in executor


@pytest.mark.asyncio
async def test_guarded_memory_read_requires_capability():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: json.dumps(
                {"required_capabilities": [{"name": "web.search"}]}
            ),
        }
    )

    with pytest.raises(PermissionError, match="memory.read"):
        await guarded_tools_module.guarded_memory_read(
            query="release notes",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_memory_read_prefers_adk_memory():
    class FakeToolContext(SimpleNamespace):
        async def search_memory(self, query: str) -> SearchMemoryResponse:
            assert query == "project history"
            return SearchMemoryResponse(
                memories=[
                    MemoryEntry(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text="remembered fact")],
                        ),
                        author="memory",
                        timestamp="2026-03-10T00:00:00Z",
                    )
                ]
            )

    tool_context = FakeToolContext(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "memory.read"}]
            },
        }
    )

    result = await guarded_tools_module.guarded_memory_read(
        query="project history",
        tool_context=tool_context,
    )

    assert result["source"] == "adk_memory"
    assert result["count"] == 1
    assert result["results"][0]["content"] == "remembered fact"


@pytest.mark.asyncio
async def test_guarded_browser_fill_uses_browser_navigate_capability(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "browser.navigate"}]
            },
        }
    )
    seen = {}

    async def _fake_fill(selector, text, timeout=30000, tool_context=None):
        seen["selector"] = selector
        seen["text"] = text
        return {"success": True}

    monkeypatch.setattr("src.tools.browser.browser_fill", _fake_fill)

    result = await guarded_tools_module.guarded_browser_fill(
        "textarea",
        "Hello World",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["selector"] == "textarea"
    assert seen["text"] == "Hello World"


@pytest.mark.asyncio
async def test_guarded_current_tab_extract_text_uses_current_tab_navigate_capability(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}]
            },
        }
    )
    seen = {}

    async def _fake_extract(selector=None, tool_context=None):
        seen["selector"] = selector
        return {"success": True, "text": "hello"}

    monkeypatch.setattr("src.tools.current_tab.current_tab_extract_text", _fake_extract)

    result = await guarded_tools_module.guarded_current_tab_extract_text(
        selector="#main",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["selector"] == "#main"


@pytest.mark.asyncio
async def test_guarded_current_tab_fill_requires_human_approved(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}]
            },
        }
    )

    async def _fake_fill(selector, text, tool_context=None):
        return {"success": True}

    monkeypatch.setattr("src.tools.current_tab.current_tab_fill", _fake_fill)

    with pytest.raises(PermissionError, match="current_tab.fill requires human_approved"):
        await guarded_tools_module.guarded_current_tab_fill(
            "#query",
            "hello",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_browser_press_passes_selector(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "browser.navigate"}]
            },
        }
    )
    seen = {}

    async def _fake_press(key, selector=None, timeout=30000, tool_context=None):
        seen["key"] = key
        seen["selector"] = selector
        return {"success": True}

    monkeypatch.setattr("src.tools.browser.browser_press", _fake_press)

    result = await guarded_tools_module.guarded_browser_press(
        "Enter",
        selector="textarea",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["key"] == "Enter"
    assert seen["selector"] == "textarea"


@pytest.mark.asyncio
async def test_guarded_desktop_view_windows_allows_policy_approved(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.view.windows"}]
            },
        }
    )

    async def _fake_windows(*, include_minimized=False):
        assert include_minimized is False
        return {"windows": [{"window_id": "w1", "app_name": "Safari"}]}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_view_windows",
        _fake_windows,
    )

    result = await guarded_tools_module.guarded_desktop_view_windows(
        tool_context=tool_context,
    )

    assert result["windows"][0]["app_name"] == "Safari"


@pytest.mark.asyncio
async def test_guarded_desktop_view_windows_allows_launch_app_plan(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [
                    {"name": "desktop.control.launch_app"}
                ]
            },
        }
    )

    async def _fake_windows(*, include_minimized=False):
        assert include_minimized is False
        return {"windows": [{"window_id": "w1", "app_name": "Google Chrome"}]}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_view_windows",
        _fake_windows,
    )

    result = await guarded_tools_module.guarded_desktop_view_windows(
        tool_context=tool_context,
    )

    assert result["windows"][0]["app_name"] == "Google Chrome"


@pytest.mark.asyncio
async def test_guarded_desktop_ax_find_allows_policy_approved(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.ax.find"}]
            },
        }
    )

    async def _fake_find(**kwargs):
        assert kwargs["identifier"] == "open-button"
        return {"matched": True, "target": {"identifier": "open-button"}}

    monkeypatch.setattr("src.tools.desktop.desktop_ax_find", _fake_find)

    result = await guarded_tools_module.guarded_desktop_ax_find(
        app_name="Safari",
        window_id="w1",
        identifier="open-button",
        tool_context=tool_context,
    )

    assert result["matched"] is True


@pytest.mark.asyncio
async def test_guarded_desktop_wait_element_allows_click_plan(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.click"}]
            },
        }
    )

    async def _fake_wait(**kwargs):
        assert kwargs["identifier"] == "open-button"
        return {"matched": True, "target": {"identifier": "open-button"}}

    monkeypatch.setattr("src.tools.desktop.desktop_wait_element", _fake_wait)

    result = await guarded_tools_module.guarded_desktop_wait_element(
        app_name="Safari",
        window_id="w1",
        identifier="open-button",
        tool_context=tool_context,
    )

    assert result["matched"] is True


@pytest.mark.asyncio
async def test_guarded_desktop_wait_element_allows_policy_approved(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.wait.element"}]
            },
        }
    )

    async def _fake_wait(**kwargs):
        assert kwargs["identifier"] == "open-button"
        return {"matched": True, "target": {"identifier": "open-button"}}

    monkeypatch.setattr("src.tools.desktop.desktop_wait_element", _fake_wait)

    result = await guarded_tools_module.guarded_desktop_wait_element(
        app_name="Safari",
        window_id="w1",
        identifier="open-button",
        tool_context=tool_context,
    )

    assert result["matched"] is True


@pytest.mark.asyncio
async def test_guarded_desktop_control_click_requires_human_approved():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.click"}]
            },
        }
    )

    with pytest.raises(PermissionError, match="human_approved"):
        await guarded_tools_module.guarded_desktop_control_click(
            10,
            20,
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_launch_app_requires_human_approved():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.launch_app"}]
            },
        }
    )

    with pytest.raises(PermissionError, match="human_approved"):
        await guarded_tools_module.guarded_desktop_control_launch_app(
            app_name="Safari",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_scroll_requires_human_approved():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.scroll"}]
            },
        }
    )

    with pytest.raises(PermissionError, match="human_approved"):
        await guarded_tools_module.guarded_desktop_control_scroll(
            delta_y=-4,
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_type_passes_selector(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.type"}]
            },
        }
    )
    seen = {}

    async def _fake_type(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr("src.tools.desktop.desktop_control_type", _fake_type)

    result = await guarded_tools_module.guarded_desktop_control_type(
        text="hello",
        app_name="Safari",
        window_id="w1",
        role="AXTextField",
        identifier="search-field",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["identifier"] == "search-field"


@pytest.mark.asyncio
async def test_guarded_desktop_control_type_rewrites_current_browser_search_to_url(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザをつかって午後の東京の花粉を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.type"}]
            },
            "temp:current_browser_address_bar_focused": True,
        }
    )
    seen = {}

    async def _fake_type(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr("src.tools.desktop.desktop_control_type", _fake_type)

    result = await guarded_tools_module.guarded_desktop_control_type(
        text="午後の東京の花粉",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert (
        seen["text"]
        == "https://www.google.com/search?q=%E5%8D%88%E5%BE%8C%E3%81%AE%E6%9D%B1%E4%BA%AC%E3%81%AE%E8%8A%B1%E7%B2%89"
    )
    assert tool_context.state["temp:current_browser_address_bar_focused"] is False


@pytest.mark.asyncio
async def test_guarded_desktop_control_type_rewrites_current_browser_search_without_focus_flag(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザをつかって明日の東京の花粉を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.type"}]
            },
        }
    )
    seen = {}

    async def _fake_type(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr("src.tools.desktop.desktop_control_type", _fake_type)

    result = await guarded_tools_module.guarded_desktop_control_type(
        text="明日の東京の花粉",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["text"].startswith("https://www.google.com/search?q=")
    assert "%E6%98%8E%E6%97%A5" in seen["text"]


def test_policy_judge_requires_human_for_desktop_control():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "open the existing browser spreadsheet",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "plan-desktop-1",
                "goal": "click through the desktop flow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.control.click"},
                ],
            }
        }
    )

    policy_judge_callback(callback_context)

    assert callback_context.state[StateKeys.APPROVAL_STATUS] == "needs_human"
    assert callback_context.state[StateKeys.PLAN_APPROVED]["plan_id"] == "plan-desktop-1"
    assert callback_context.state[StateKeys.APPROVAL_REQUEST]["goal"] == "open the existing browser spreadsheet"


def test_policy_judge_expands_current_browser_capabilities():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを操作して、",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "browser-op-init-v1",
                "goal": "このブラウザを操作して、",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.ax.snapshot", "mode": "read"},
                    {"name": "desktop.control.focus_window", "mode": "execute"},
                    {"name": "desktop.view.windows", "mode": "read"},
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    approval_required = set(
        callback_context.state[StateKeys.APPROVAL_REQUEST]["required_capabilities"]
    )

    assert callback_context.state[StateKeys.APPROVAL_STATUS] == "needs_human"
    assert "desktop.view.frontmost_app" in required
    assert "desktop.control.click" in required
    assert "desktop.control.hotkey" in required
    assert "desktop.control.scroll" in required
    assert "desktop.ax.find" in required
    assert "desktop.wait.element" in required
    assert "desktop.view.screenshot" in required
    assert "desktop.ax.snapshot" in required
    assert "desktop.control.launch_app" not in required
    assert "desktop.view.frontmost_app" in approval_required
    assert "desktop.control.click" in approval_required


def test_policy_judge_expands_type_for_current_browser_spreadsheet_goal():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: (
                "このブラウザを使ってNvidaのGTCで紹介される可能性のある技術を"
                "調べてスプレッドシーートにまとめて"
            ),
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "nvidia-gtc-research-spreadsheet",
                "goal": "visible spreadsheet workflow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "web.search", "mode": "network"},
                    {"name": "browser.navigate", "mode": "network"},
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}

    assert "desktop.view.frontmost_app" in required
    assert "desktop.control.click" in required
    assert "desktop.control.type" in required
    assert "desktop.control.hotkey" in required
    assert "desktop.control.scroll" in required
    assert "desktop.ax.find" in required
    assert "desktop.wait.element" in required
    assert "desktop.view.screenshot" in required
    assert "desktop.ax.snapshot" in required
    assert "desktop.control.launch_app" not in required


def test_planner_after_agent_callback_accepts_callback_context_only():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "plan-simple-1",
                "goal": "inspect the page",
                "risk_level": "low",
                "required_capabilities": [{"name": "browser.navigate"}],
            }
        }
    )

    planner_with_policy.after_agent_callback(callback_context=callback_context)

    assert callback_context.state[StateKeys.APPROVAL_STATUS] == "policy_approved"
    assert callback_context.state[StateKeys.PLAN_APPROVED]["plan_id"] == "plan-simple-1"


def test_verifier_after_agent_callback_accepts_callback_context_only():
    callback_context = SimpleNamespace(
        state={
            StateKeys.VERIFY_LAST_REPORT: {
                "report_id": "report-1",
                "plan_id": "plan-1",
                "status": "pass",
            }
        }
    )

    verifier_with_hooks.after_agent_callback(callback_context=callback_context)

    assert callback_context.state[StateKeys.REPAIR_COUNT] == 0
    assert callback_context.state[StateKeys.TEMP_REPAIR_PATCH] is None


@pytest.mark.asyncio
async def test_planner_instruction_mentions_current_browser_desktop_capabilities():
    ctx = _make_readonly_context(
        {
            StateKeys.TASK_GOAL: "このブラウザを操作して、",
            StateKeys.TASK_CONSTRAINTS: [],
            StateKeys.TEMP_REPAIR_PATCH: None,
        }
    )

    planner = await build_planner_instruction(ctx)

    assert "desktop.view.frontmost_app" in planner
    assert "desktop.control.click" in planner
    assert "desktop.control.type" in planner
    assert "desktop.control.hotkey" in planner
    assert "desktop.control.scroll" in planner
    assert "desktop.view.screenshot" in planner
    assert "desktop.ax.snapshot" in planner
    assert "desktop-backed browser task" in planner
    assert "do not include desktop.control.launch_app" in planner.lower()
    assert "preserving the boiled-claw Control UI chat tab" in planner


@pytest.mark.asyncio
async def test_guarded_desktop_control_launch_app_rejects_current_browser_task():
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って明日の天気を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.launch_app"}]
            },
        }
    )

    with pytest.raises(PermissionError, match="not allowed for current-browser tasks"):
        await guarded_tools_module.guarded_desktop_control_launch_app(
            app_name="TextEdit",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_launch_app_redirects_to_focus_for_current_browser(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って明日の天気を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.launch_app"}]
            },
        }
    )

    async def fake_focus_window(
        app_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
        tool_context=None,
    ) -> dict:
        return {"success": True, "target": {"app_name": app_name, "window_id": window_id, "title": title}}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_focus_window",
        fake_focus_window,
    )

    result = await guarded_tools_module.guarded_desktop_control_launch_app(
        app_name="Google Chrome",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["target"]["app_name"] == "Google Chrome"


@pytest.mark.asyncio
async def test_guarded_desktop_control_focus_window_prefers_control_ui_title_when_preserving_tab(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザをつかって明日の東京の花粉を調べて",
            StateKeys.TASK_CONSTRAINTS: [
                (
                    "If the current tab is the boiled-claw Control UI chat, preserve "
                    "that tab and open a new tab in the same browser window for "
                    "browsing or search. Otherwise stay on the current tab."
                )
            ],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.focus_window"}]
            },
        }
    )
    calls: list[dict[str, str | None]] = []

    async def fake_focus_window(
        app_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
        tool_context=None,
    ) -> dict:
        calls.append(
            {
                "app_name": app_name,
                "window_id": window_id,
                "title": title,
            }
        )
        return {
            "success": True,
            "target": {
                "app_name": app_name,
                "window_id": window_id,
                "title": title,
            },
        }

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_focus_window",
        fake_focus_window,
    )

    result = await guarded_tools_module.guarded_desktop_control_focus_window(
        app_name="Google Chrome",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert calls[0]["app_name"] == "Google Chrome"
    assert calls[0]["title"] == "boiled-claw Control UI"


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_rejects_new_tab_for_current_browser():
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って明日の天気を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.hotkey"}]
            },
        }
    )

    with pytest.raises(PermissionError, match="focus-address-bar or submit hotkeys"):
        await guarded_tools_module.guarded_desktop_control_hotkey(
            keys=["cmd", "t"],
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_allows_browser_search_shortcuts_for_current_browser(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って午後の東京の花粉を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.hotkey"}]
            },
        }
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "k"],
        tool_context=tool_context,
    )

    assert result == {"ok": True, "keys": ["meta", "l"]}


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_allows_new_tab_when_preserving_control_ui(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って午後の東京の花粉を調べて",
            StateKeys.TASK_CONSTRAINTS: [
                (
                    "If the current tab is the boiled-claw Control UI chat, preserve "
                    "that tab and open a new tab in the same browser window for "
                    "browsing or search. Otherwise stay on the current tab."
                )
            ],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.hotkey"}]
            },
        }
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "t"],
        tool_context=tool_context,
    )

    assert result == {"ok": True, "keys": ["cmd", "t"]}


@pytest.mark.asyncio
async def test_control_loop_resumes_after_human_approval(monkeypatch, tmp_path):
    session_service = InMemorySessionService()
    candidate_store = CandidateStore(
        str(tmp_path / "candidates.db")
    )
    monkeypatch.setattr(candidate_store_module, "_candidate_store", candidate_store)
    memory_service = PromotedMemoryService(
        PromotedMemoryStore(str(tmp_path / "promoted.db"))
    )
    await session_service.create_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
        state={
            StateKeys.TASK_GOAL: "Ship the patch",
            StateKeys.TASK_CONSTRAINTS: [],
            StateKeys.REPAIR_COUNT: 0,
            StateKeys.APPROVAL_STATUS: "needs_human",
            StateKeys.PLAN_APPROVED: {
                "plan_id": "plan-1",
                "required_capabilities": [{"name": "file.read"}],
            },
            "custom:key": "keep",
        },
    )
    candidate_store.save(
        MemoryCandidate(
            candidate_id="cand-1",
            session_id="sess-1",
            user_id="user-1",
            memory_type=MemoryType.PROCEDURAL,
            content="Ship the patch via control loop",
            subject="Ship the patch",
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="test",
                captured_at=datetime.now(timezone.utc),
            ),
            confidence=0.9,
            trust_score=0.9,
            sensitivity=SensitivityLevel.INTERNAL,
        )
    )

    loop = ControlLoop(
        session_service=session_service,
        memory_service=memory_service,
    )
    calls: list[str] = []

    async def fake_run_agent(agent, *, session_id, user_id, message):
        calls.append(agent.name)
        session = await session_service.get_session(
            app_name="boiled_claw_v2",
            user_id=user_id,
            session_id=session_id,
        )
        assert session is not None

        if agent.name == "executor":
            await session_service.append_event(
                session,
                Event(
                    invocation_id="test:executor",
                    author=agent.name,
                    actions=EventActions(
                        state_delta={
                            StateKeys.TEMP_EXECUTOR_OUTPUTS: {
                                "plan_id": "plan-1",
                                "summary": "executed",
                            }
                        }
                    ),
                ),
            )
            return

        if agent.name == "verifier":
            await session_service.append_event(
                session,
                Event(
                    invocation_id="test:verifier",
                    author=agent.name,
                    actions=EventActions(
                        state_delta={
                            StateKeys.VERIFY_LAST_REPORT: {
                                "report_id": "report-1",
                                "plan_id": "plan-1",
                                "status": "pass",
                                "overall_score": 0.9,
                                "summary": "verified",
                            }
                        }
                    ),
                ),
            )
            return

        raise AssertionError("planner should not run when resuming an approved plan")

    monkeypatch.setattr(loop, "_run_agent", fake_run_agent)

    resolved = await loop.resolve_human_approval(
        user_id="user-1",
        session_id="sess-1",
        approved=True,
    )
    assert resolved is True

    result = await loop.run(
        goal="Ship the patch",
        user_id="user-1",
        session_id="sess-1",
    )

    session = await session_service.get_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
    )
    assert session is not None
    assert session.state["custom:key"] == "keep"
    assert calls == ["executor", "verifier"]
    assert result.success is True
    assert result.plan_id == "plan-1"
    assert len(result.promoted_memory_ids) == 1
    assert result.metadata["session_created"] is False

    memory_result = await memory_service.search_memory(
        app_name="boiled_claw_v2",
        user_id="user-1",
        query="control loop",
    )
    assert memory_result.memories


@pytest.mark.asyncio
async def test_curator_deprecates_conflicting_promoted_memory(tmp_path):
    candidate_store = CandidateStore(str(tmp_path / "candidates.db"))
    promoted_store = PromotedMemoryStore(str(tmp_path / "promoted.db"))
    existing = PromotedMemory(
        memory_id="mem-existing",
        user_id="user-1",
        memory_type=MemoryType.SEMANTIC,
        content="gateway port is 18789 for production",
        subject="gateway port",
        provenance=Provenance(
            originator_type=OriginatorType.SYSTEM,
            capture_method="test",
            captured_at=datetime.now(timezone.utc),
        ),
        confidence=0.7,
        trust_score=0.7,
        sensitivity=SensitivityLevel.INTERNAL,
    )
    promoted_store.save(existing, app_name="boiled_claw_v2")

    candidate = MemoryCandidate(
        candidate_id="cand-1",
        session_id="sess-1",
        user_id="user-1",
        memory_type=MemoryType.SEMANTIC,
        content="gateway port is 19789 for production",
        subject="gateway port",
        provenance=Provenance(
            originator_type=OriginatorType.SYSTEM,
            capture_method="test",
            captured_at=datetime.now(timezone.utc),
        ),
        confidence=0.95,
        trust_score=0.95,
        sensitivity=SensitivityLevel.INTERNAL,
    )

    curation = await Curator(
        candidate_store,
        existing_promoted=promoted_store.list_memories(
            app_name="boiled_claw_v2",
            user_id="user-1",
        ),
    ).curate_candidates([candidate], user_id="user-1")

    assert len(curation.promoted) == 1
    assert len(curation.updated) == 1
    assert curation.promoted[0].supersedes == ["mem-existing"]
    assert curation.updated[0].review_status.value == "deprecated"

    promoted_store.bulk_save(curation.persisted_memories, app_name="boiled_claw_v2")
    memories = promoted_store.search(
        app_name="boiled_claw_v2",
        user_id="user-1",
        query="gateway port",
    )
    assert len(memories) == 1
    assert memories[0].content == "gateway port is 19789 for production"


@pytest.mark.asyncio
async def test_control_loop_rejects_goal_change_for_existing_session():
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
        state={StateKeys.TASK_GOAL: "Old goal"},
    )

    loop = ControlLoop(session_service=session_service)

    with pytest.raises(ValueError, match="different task goal"):
        await loop.run(
            goal="New goal",
            user_id="user-1",
            session_id="sess-1",
        )


@pytest.mark.asyncio
async def test_control_loop_allows_goal_change_after_terminal_report():
    session_service = InMemorySessionService()
    existing = await session_service.create_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
        state={
            StateKeys.TASK_GOAL: "Old goal",
            StateKeys.TASK_CONSTRAINTS: ["legacy"],
            StateKeys.PLAN_APPROVED: {"plan_id": "plan-old"},
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.VERIFY_LAST_REPORT: {"status": "pass", "summary": "done"},
            StateKeys.TEMP_REPAIR_PATCH: {"note": "stale"},
        },
    )

    loop = ControlLoop(session_service=session_service)
    session, created = await loop._get_or_create_session(
        user_id="user-1",
        session_id="sess-1",
        goal="New goal",
        init_state={
            StateKeys.TASK_GOAL: "New goal",
            StateKeys.TASK_CONSTRAINTS: ["fresh"],
            StateKeys.REPAIR_COUNT: 0,
        },
    )

    assert created is False
    assert session.id == existing.id
    assert session.state[StateKeys.TASK_GOAL] == "New goal"
    assert session.state[StateKeys.TASK_CONSTRAINTS] == ["fresh"]
    assert session.state.get(StateKeys.PLAN_APPROVED) is None
    assert session.state.get(StateKeys.APPROVAL_STATUS) is None
    assert session.state.get(StateKeys.VERIFY_LAST_REPORT) is None
    assert session.state.get(StateKeys.TEMP_REPAIR_PATCH) is None
