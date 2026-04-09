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
from src.control_loop.root_workflow import (
    ControlLoop,
    _backfill_current_tab_verification_inputs,
    _build_final_text,
    _build_repair_patch_from_report,
    _build_replay_context_payload,
    _build_executor_message,
    _build_step_trace,
    _build_verification_inputs,
    _demote_browser_text_entry_report,
    _extract_latest_agent_json_output,
    _infer_tail_replay_from_step,
    _promote_visual_playback_report,
    _retarget_browser_text_entry_repair,
    _should_demote_browser_text_entry_report,
    _should_promote_visual_playback_report,
    _should_resume_existing_plan,
    _should_retarget_browser_text_entry_repair,
    planner_with_policy,
    verifier_with_hooks,
)
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
    assert "reusing the same tab for later" in planner
    assert "do not open more new tabs than the approved plan" in executor
    assert "isolated browser or managed browser page" in planner
    assert "existing browser tabs or forms" in executor
    assert "do NOT mark pass" in verifier
    assert "click/type/fill/press success" in verifier
    assert "CAPABILITY → TOOL MAPPING" in executor
    assert "guarded_browser_navigate" in executor
    assert "desktop.control.launch_app is PROHIBITED" in executor
    assert "PRIMARY visual" in verifier


@pytest.mark.asyncio
async def test_executor_instruction_mentions_replay_context():
    ctx = _make_readonly_context(
        {
            StateKeys.PLAN_APPROVED: {"plan_id": "plan-1"},
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.REPLAY_CONTEXT: {
                "source_task_id": "task_old",
                "from_step": "verify_visual_state",
                "mode": "tail",
            },
        }
    )

    executor = await build_executor_instruction(ctx)

    assert "Replay Context" in executor
    assert "verify_visual_state" in executor
    assert "Treat earlier approved steps as already satisfied" in executor


def test_build_executor_message_includes_replay_suffix_steps():
    message = _build_executor_message(
        approved_plan={
            "steps": [
                {"step_id": "step_1", "title": "Search"},
                {"step_id": "capture_current_tab_state", "title": "Capture current tab"},
            ]
        },
        replay_context={
            "source_task_id": "task_old",
            "from_step": "capture_current_tab_state",
            "mode": "tail",
        },
    )

    assert "Replay from step: capture_current_tab_state" in message
    assert "Replay suffix steps" in message
    assert '"step_id": "capture_current_tab_state"' in message


def test_should_resume_existing_plan_on_repair_replay():
    assert _should_resume_existing_plan(
        attempt=1,
        has_approved_plan=True,
        approval="human_approved",
        replay_context={"from_step": "capture_current_tab_state", "mode": "tail"},
        repair_patch={"repair_actions": [{"target_step_ids": ["capture_current_tab_state"]}]},
    ) is True


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
async def test_guarded_desktop_view_windows_requires_explicit_plan_capability(monkeypatch):
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

    with pytest.raises(
        PermissionError,
        match="Capability 'desktop.view.windows' is not in the approved plan.",
    ):
        await guarded_tools_module.guarded_desktop_view_windows(
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_launch_app_requires_explicit_plan_capability():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [
                    {"name": "desktop.view.frontmost_app"},
                    {"name": "desktop.view.windows"},
                    {"name": "desktop.wait.window"},
                ]
            },
        }
    )

    with pytest.raises(
        PermissionError,
        match="Capability 'desktop.control.launch_app' is not in the approved plan.",
    ):
        await guarded_tools_module.guarded_desktop_control_launch_app(
            app_name="Google Chrome",
            tool_context=tool_context,
        )


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
async def test_guarded_desktop_control_type_reuses_remembered_spreadsheet_target(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.type"}]
            },
            StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET: {
                "app_name": "Google Chrome",
                "window_id": "w7",
                "role": "AXTextField",
                "identifier": "cell-a1",
            },
        }
    )
    seen = {}

    async def _fake_type(**kwargs):
        seen.update(kwargs)
        return {"success": True, "target": {"identifier": "cell-a1"}}

    monkeypatch.setattr("src.tools.desktop.desktop_control_type", _fake_type)

    result = await guarded_tools_module.guarded_desktop_control_type(
        text="2026-04-09\tWTI",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert seen["identifier"] == "cell-a1"
    assert seen["window_id"] == "w7"


@pytest.mark.asyncio
async def test_guarded_desktop_click_remembers_spreadsheet_target(monkeypatch):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.click"}]
            },
        }
    )

    async def _fake_click(**_kwargs):
        return {
            "success": True,
            "target": {
                "app_name": "Google Chrome",
                "window_id": "w7",
                "role": "AXTextField",
                "identifier": "cell-a1",
            },
        }

    monkeypatch.setattr("src.tools.desktop.desktop_control_click", _fake_click)

    result = await guarded_tools_module.guarded_desktop_control_click(
        app_name="Google Chrome",
        window_id="w7",
        role="AXTextField",
        identifier="cell-a1",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET] == {
        "app_name": "Google Chrome",
        "window_id": "w7",
        "role": "AXTextField",
        "identifier": "cell-a1",
    }


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
            StateKeys.TASK_GOAL: "open the existing desktop app and click through the flow",
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
    assert callback_context.state[StateKeys.APPROVAL_REQUEST]["goal"] == "open the existing desktop app and click through the flow"


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


def test_policy_judge_keeps_current_browser_safety_for_current_browser_spreadsheet_goal():
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
                "steps": [
                    {
                        "step_id": "type_into_sheet",
                        "title": "スプレッドシートへ入力",
                        "description": "現在のブラウザで開いているスプレッドシートに内容を入力する。",
                        "capabilities": [
                            {"name": "desktop.control.click", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["スプレッドシートのセルに内容が入力されていること"],
                        "retryable": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}

    assert callback_context.state[StateKeys.APPROVAL_STATUS] == "needs_human"
    assert "browser.navigate" in required
    assert "web.search" in required
    assert "desktop.view.frontmost_app" in required
    assert "desktop.control.click" in required
    assert "desktop.control.type" in required
    assert "desktop.control.hotkey" in required
    assert "desktop.ax.find" in required
    assert "current_tab.info" in required
    assert "current_tab.extract_text" in required
    edit_step = next(step for step in approved["steps"] if step["step_id"] == "type_into_sheet")
    edit_capabilities = {cap["name"] for cap in edit_step["capabilities"]}
    assert edit_capabilities == {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.ax.find",
        "desktop.wait.element",
    }
    assert "A1" in edit_step["description"]
    assert "capture_current_tab_state" in {step["step_id"] for step in approved["steps"]}


def test_policy_judge_keeps_current_browser_spreadsheet_evidence_out_of_playback_verify_step():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザでスプレッドシートを更新して結果を確認して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "spreadsheet-evidence-001",
                "goal": "visible spreadsheet workflow",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.control.click", "mode": "execute"},
                    {"name": "desktop.control.type", "mode": "execute"},
                ],
                "steps": [
                    {
                        "step_id": "fill_sheet",
                        "title": "スプレッドシートへ入力",
                        "description": "現在のブラウザで開いているスプレッドシートを更新する。",
                        "capabilities": [
                            {"name": "desktop.control.click", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["スプレッドシートのセルが更新されていること"],
                        "retryable": True,
                    }
                ],
                "success_criteria": [
                    {
                        "name": "data_recorded",
                        "criterion_type": "evidence",
                        "description": "スプレッドシートに入力結果が見えていること",
                        "required": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    step_ids = {step["step_id"] for step in approved["steps"]}

    assert "capture_current_tab_state" in step_ids
    assert "verify_visual_state" not in step_ids


def test_policy_judge_adds_current_tab_navigate_without_playback_false_positive():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-001",
                "goal": "search oil prices in the current browser and write them into Google Sheets",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.control.hotkey", "mode": "execute"},
                    {"name": "desktop.control.type", "mode": "execute"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "Search Oil Prices",
                        "description": "Open a new tab and search for oil market trends.",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Search results page displayed"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_sheets",
                        "title": "Open Google Sheets",
                        "description": "Open another new tab and navigate to sheets.new.",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": ["search_oil_price"],
                        "expected_outputs": ["Google Sheets interface active"],
                        "retryable": True,
                    },
                ],
                "success_criteria": [
                    {
                        "name": "sheet_updated",
                        "criterion_type": "evidence",
                        "description": "The spreadsheet contains the weekly oil summary.",
                        "required": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    step_ids = {step["step_id"] for step in approved["steps"]}
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )
    open_step = next(
        step for step in approved["steps"] if step["step_id"] == "open_sheets"
    )

    assert "current_tab.navigate" in required
    assert "desktop.control.type" not in required
    assert "desktop.control.hotkey" not in required
    assert "capture_pre_playback_state" not in step_ids
    assert "verify_visual_state" not in step_ids
    assert {
        cap["name"] for cap in search_step["capabilities"]
    } == {
        "current_tab.navigate",
    }
    assert {
        cap["name"] for cap in open_step["capabilities"]
    } == {
        "current_tab.navigate",
    }
    assert "current_tab.navigate" in search_step["description"]
    assert "current_tab.navigate" in open_step["description"]
    assert "Google検索URL" in search_step["description"]
    assert "https://docs.google.com/spreadsheets/create" in open_step["description"]
    assert "Open a new tab" not in search_step["description"]
    assert "another new tab" not in open_step["description"]


def test_policy_judge_rewrites_generic_google_spreadsheet_open_step_to_current_tab_navigate():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-price-tracking-plan-001",
                "goal": "石油の一週間の値動きを調べて、google spreadsheetに記載して",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "desktop.ax.snapshot", "mode": "read"},
                    {"name": "desktop.control.type", "mode": "execute"},
                    {"name": "desktop.view.frontmost_app", "mode": "read"},
                    {"name": "desktop.view.screenshot", "mode": "read"},
                    {"name": "desktop.wait.element", "mode": "read"},
                    {"name": "web.search", "mode": "network"},
                ],
                "steps": [
                    {
                        "step_id": "search-oil-price",
                        "title": "石油価格の検索",
                        "description": "Google検索で「WTI原油先物 一週間 値動き」または金融サイトを使用して、直近7日間の価格データを取得する。",
                        "capabilities": [
                            {"name": "web.search", "mode": "network"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["過去7日間の日付と価格のリスト"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open-google-sheets",
                        "title": "Google Spreadsheetの起動",
                        "description": "ブラウザでGoogle Sheetsを開き、新規シートを作成するか、既存のシートにアクセスする。",
                        "capabilities": [
                            {"name": "desktop.view.frontmost_app", "mode": "read"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": ["search-oil-price"],
                        "expected_outputs": ["Google Spreadsheetの編集画面"],
                        "retryable": True,
                    },
                    {
                        "step_id": "input-data",
                        "title": "シートへのデータ入力",
                        "description": "取得した石油価格データをGoogle Spreadsheetのセルに記載する。",
                        "capabilities": [
                            {"name": "desktop.control.type", "mode": "execute"},
                            {"name": "desktop.ax.find", "mode": "read"},
                        ],
                        "depends_on": ["open-google-sheets"],
                        "expected_outputs": ["スプレッドシートへのデータ入力完了"],
                        "retryable": True,
                    },
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    open_step = next(
        step for step in approved["steps"] if step["step_id"] == "open-google-sheets"
    )

    assert "current_tab.navigate" in required
    assert "desktop.control.launch_app" not in required
    assert {cap["name"] for cap in open_step["capabilities"]} == {
        "desktop.view.frontmost_app",
        "current_tab.navigate",
    }
    assert "desktop.control.type" not in {cap["name"] for cap in open_step["capabilities"]}
    assert "current_tab.navigate" in open_step["description"]
    assert "https://docs.google.com/spreadsheets/create" in open_step["description"]


def test_policy_judge_strips_hotkey_language_from_current_browser_navigation_steps():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-ja-001",
                "goal": "current browser oil workflow",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.control.hotkey", "mode": "execute"},
                    {"name": "desktop.control.type", "mode": "execute"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "石油の動向を調査",
                        "description": "Ctrl+Tで新しいタブを開き、Googleで「石油 一週間の値動き」を検索する。",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["検索結果のテキスト情報"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_spreadsheet",
                        "title": "スプレッドシートを開く",
                        "description": "新しいタブを開き、https://docs.google.com/spreadsheets/create にアクセスする。",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": ["search_oil_price"],
                        "expected_outputs": ["Googleスプレッドシートが開かれた状態"],
                        "retryable": True,
                    },
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )
    open_step = next(
        step for step in approved["steps"] if step["step_id"] == "open_spreadsheet"
    )

    assert "Ctrl+T" not in search_step["description"]
    assert "新しいタブを開き" not in search_step["description"]
    assert "新しいタブを開き" not in open_step["description"]
    assert {cap["name"] for cap in search_step["capabilities"]} == {"current_tab.navigate"}
    assert {cap["name"] for cap in open_step["capabilities"]} == {"current_tab.navigate"}


def test_policy_judge_strips_parenthesized_ctrl_cmd_t_from_current_browser_navigation_steps():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-en-ctrl-cmd-001",
                "goal": "current browser oil workflow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.control.hotkey", "mode": "execute"},
                    {"name": "current_tab.navigate", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "Search for oil market trends",
                        "description": "(Ctrl/Cmd + T), navigate to a search engine, and search for 'oil price weekly trend'.",
                        "capabilities": [
                            {"name": "desktop.ax.snapshot", "mode": "read"},
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Search results page loaded"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_sheets",
                        "title": "Open Google Sheets",
                        "description": "(Ctrl/Cmd + T) and navigate to https://docs.google.com/spreadsheets/create to create or open a spreadsheet.",
                        "capabilities": [
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["search_oil_price"],
                        "expected_outputs": ["Google Sheets page loaded"],
                        "retryable": True,
                    },
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )
    open_step = next(
        step for step in approved["steps"] if step["step_id"] == "open_sheets"
    )

    assert "Ctrl/Cmd + T" not in search_step["description"]
    assert "Ctrl/Cmd + T" not in open_step["description"]
    assert not search_step["description"].startswith("(")
    assert not open_step["description"].startswith("(")
    assert search_step["description"].startswith("navigate to a search engine")
    assert open_step["description"].startswith("navigate to https://docs.google.com/spreadsheets/create")


def test_policy_judge_strips_slash_ctrl_cmd_t_from_current_browser_navigation_steps():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-ja-ctrl-slash-cmd-001",
                "goal": "current browser oil workflow",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "current_tab.navigate", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "石油の価格動向を検索",
                        "description": "Ctrl/Cmd + T でGoogleで「石油 価格 1週間 動向」を検索し、情報を抽出する。",
                        "capabilities": [
                            {"name": "desktop.ax.find", "mode": "read"},
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["石油の直近一週間の価格動向に関するテキスト情報"],
                        "retryable": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )

    assert "Ctrl/Cmd + T" not in search_step["description"]
    assert search_step["description"].startswith("Googleで")
    assert "current_tab.navigate" in search_step["description"]


def test_policy_judge_strips_using_ctrl_cmd_t_and_new_tab_phrasing_from_current_browser_steps():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動き動向を調べて、このブラウザの新しいタブからgoogleスプレッドシートを開いて、結果を記載して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-english-hotkey-001",
                "goal": "current browser oil workflow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "current_tab.navigate", "mode": "read"},
                    {"name": "desktop.control.type", "mode": "write"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "Search Oil Price",
                        "description": "using Ctrl/Cmd+T and search for 'crude oil price last 7 days trend'.",
                        "capabilities": [
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Search results page loaded"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_sheets",
                        "title": "Open Google Sheets",
                        "description": "Navigate to https://docs.google.com/spreadsheets/create in a new tab.",
                        "capabilities": [
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["search_oil_price"],
                        "expected_outputs": ["Google Sheets opened"],
                        "retryable": True,
                    },
                    {
                        "step_id": "input_data",
                        "title": "Enter Data into Sheet",
                        "description": "Type the gathered oil price information into the active spreadsheet cell.",
                        "capabilities": [
                            {"name": "desktop.control.type", "mode": "write"},
                        ],
                        "depends_on": ["open_sheets"],
                        "expected_outputs": ["Data entered in spreadsheet"],
                        "retryable": True,
                    },
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )
    open_step = next(
        step for step in approved["steps"] if step["step_id"] == "open_sheets"
    )
    input_step = next(
        step for step in approved["steps"] if step["step_id"] == "input_data"
    )

    assert "Ctrl/Cmd" not in search_step["description"]
    assert "new tab" not in open_step["description"].lower()
    assert search_step["description"].startswith("search for")
    assert open_step["description"].startswith("Navigate to https://docs.google.com/spreadsheets/create")
    assert {cap["name"] for cap in input_step["capabilities"]} == {
        "desktop.control.type",
        "desktop.control.click",
        "desktop.ax.find",
        "desktop.wait.element",
    }


def test_policy_judge_removes_current_browser_new_tab_step_and_focuses_sheet_input():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-repair-001",
                "goal": "current browser oil workflow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.control.focus_window", "mode": "execute"},
                    {"name": "desktop.control.hotkey", "mode": "execute"},
                    {"name": "desktop.control.click", "mode": "execute"},
                    {"name": "desktop.control.type", "mode": "execute"},
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "current_tab.navigate", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "focus_browser",
                        "title": "ブラウザの特定とフォーカス",
                        "description": "デスクトップ上のブラウザウィンドウを特定し、フォーカスを合わせる。",
                        "capabilities": [
                            {"name": "desktop.view.frontmost_app", "mode": "read"},
                            {"name": "desktop.view.windows", "mode": "read"},
                            {"name": "desktop.control.focus_window", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["ブラウザがアクティブな状態になること"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_new_tab",
                        "title": "新しいタブを開く",
                        "description": "Ctrl+T (Cmd+T) を使用して新しいタブを開く。",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                        ],
                        "depends_on": ["focus_browser"],
                        "expected_outputs": ["新しい空のタブが開くこと"],
                        "retryable": True,
                    },
                    {
                        "step_id": "search_oil_price",
                        "title": "石油の価格動向を調査",
                        "description": "アドレスバーに石油の価格動向に関する検索クエリを入力し、結果を確認する。",
                        "capabilities": [
                            {"name": "desktop.ax.find", "mode": "read"},
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["open_new_tab"],
                        "expected_outputs": ["検索結果画面が表示されること"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_sheets",
                        "title": "スプレッドシートを開く",
                        "description": "新しいタブで https://docs.google.com/spreadsheets/create を開く。",
                        "capabilities": [
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["search_oil_price"],
                        "expected_outputs": ["スプレッドシートの編集画面が表示されること"],
                        "retryable": True,
                    },
                    {
                        "step_id": "input_data",
                        "title": "結果を記入する",
                        "description": "調査した石油の値動きをスプレッドシートに入力する。",
                        "capabilities": [
                            {"name": "desktop.control.type", "mode": "execute"},
                            {"name": "desktop.control.click", "mode": "execute"},
                        ],
                        "depends_on": ["open_sheets"],
                        "expected_outputs": ["スプレッドシートにデータが記載されていること"],
                        "retryable": True,
                    },
                ],
                "success_criteria": [
                    {
                        "name": "verify_data_entry",
                        "criterion_type": "evidence",
                        "description": "スプレッドシートに石油の価格動向のデータが入力されていることをスクリーンショットで確認する。",
                        "required": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    step_ids = {step["step_id"] for step in approved["steps"]}
    search_step = next(
        step for step in approved["steps"] if step["step_id"] == "search_oil_price"
    )
    input_step = next(
        step for step in approved["steps"] if step["step_id"] == "input_data"
    )

    assert "open_new_tab" not in step_ids
    assert "verify_visual_state" not in step_ids
    assert "capture_current_tab_state" in step_ids
    assert "desktop.control.hotkey" not in required
    assert search_step["depends_on"] == ["focus_browser"]
    assert {cap["name"] for cap in input_step["capabilities"]} == {
        "desktop.control.type",
        "desktop.control.click",
        "desktop.ax.find",
        "desktop.wait.element",
    }
    assert "A1" in input_step["description"]
    assert "フォーカス" in input_step["description"]


def test_policy_judge_keeps_type_for_current_browser_spreadsheet_entry_step():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "oil-sheet-entry-001",
                "goal": "current browser oil spreadsheet workflow",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "web.search", "mode": "network"},
                    {"name": "desktop.control.click", "mode": "execute"},
                    {"name": "desktop.control.type", "mode": "execute"},
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "current_tab.navigate", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "search_oil_prices",
                        "title": "石油価格の調査",
                        "description": "Google検索で直近一週間のWTI原油価格を調べる。",
                        "capabilities": [
                            {"name": "web.search", "mode": "network"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["一週間の石油価格データ"],
                        "retryable": True,
                    },
                    {
                        "step_id": "open_spreadsheet",
                        "title": "Google Spreadsheetの準備",
                        "description": "Google Spreadsheet を開いて新規シートを作成する。",
                        "capabilities": [
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["search_oil_prices"],
                        "expected_outputs": ["Google Spreadsheetが表示されること"],
                        "retryable": True,
                    },
                    {
                        "step_id": "input_data",
                        "title": "データ入力",
                        "description": "調査した石油価格のデータを Google Spreadsheet のセルに記載する。",
                        "capabilities": [
                            {"name": "desktop.control.click", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                            {"name": "desktop.ax.find", "mode": "read"},
                            {"name": "current_tab.navigate", "mode": "read"},
                        ],
                        "depends_on": ["open_spreadsheet"],
                        "expected_outputs": ["Spreadsheetへのデータ入力完了"],
                        "retryable": True,
                    },
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    input_step = next(
        step for step in approved["steps"] if step["step_id"] == "input_data"
    )

    assert "desktop.control.type" in required
    assert {cap["name"] for cap in input_step["capabilities"]} == {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.ax.find",
        "desktop.wait.element",
        "current_tab.navigate",
    }


def test_policy_judge_prefers_isolated_browser_for_current_browser_form_fill_goal():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザのフォームに名前を入力して送信して",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "visible-form-fill",
                "goal": "visible form fill workflow",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "browser.navigate", "mode": "network"},
                ],
                "steps": [
                    {
                        "step_id": "fill_form",
                        "title": "フォームへ入力",
                        "description": "現在のブラウザのフォームに値を入力する。",
                        "capabilities": [
                            {"name": "desktop.control.click", "mode": "execute"},
                            {"name": "desktop.control.type", "mode": "execute"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["フォームに値が入力されていること"],
                        "retryable": True,
                    }
                ],
            },
        }
    )

    policy_judge_callback(callback_context)

    approved = callback_context.state[StateKeys.PLAN_APPROVED]
    required = {cap["name"] for cap in approved["required_capabilities"]}
    fill_step = next(step for step in approved["steps"] if step["step_id"] == "fill_form")

    assert callback_context.state[StateKeys.APPROVAL_STATUS] == "policy_approved"
    assert required == {"browser.navigate"}
    assert {cap["name"] for cap in fill_step["capabilities"]} == {"browser.navigate"}


def test_build_verification_inputs_includes_current_tab_location_for_browser_text_entry():
    payload = _build_verification_inputs(
        plan={"plan_id": "sheet-1"},
        goal="このブラウザでスプレッドシートに入力して",
        executor_outputs={
            "steps_executed": [
                {"step_id": "type_into_sheet", "artifact_ref": ""},
            ]
        },
        tool_responses=[
            {
                "name": "guarded_current_tab_info",
                "response": {
                    "success": True,
                    "url": "https://docs.google.com/spreadsheets/d/abc123/edit",
                    "title": "WTI prices - Google スプレッドシート",
                    "tab_id": 11,
                    "window_id": 7,
                },
            },
            {
                "name": "guarded_current_tab_extract_text",
                "response": {
                    "success": True,
                    "text": "WTI prices 2026-03-05",
                    "length": 21,
                },
            },
            {
                "name": "guarded_desktop_view_screenshot",
                "response": {"path": "data/screenshots/sheet-after.png"},
            },
        ],
    )

    assert payload["current_browser_goal"] is True
    assert payload["text_entry_goal"] is True
    assert payload["current_tab"]["url"] == "https://docs.google.com/spreadsheets/d/abc123/edit"
    assert payload["current_tab"]["title"] == "WTI prices - Google スプレッドシート"
    assert payload["current_tab"]["extract_text_succeeded"] is True
    assert payload["artifact_refs"] == ["data/screenshots/sheet-after.png"]


def test_build_verification_inputs_accepts_host_current_tab_tool_names():
    payload = _build_verification_inputs(
        plan={"plan_id": "sheet-host-1"},
        goal="このブラウザでスプレッドシートに入力して",
        executor_outputs=None,
        tool_responses=[
            {
                "name": "host.current_tab.info",
                "response": {
                    "ok": True,
                    "url": "https://docs.google.com/spreadsheets/d/host123/edit",
                    "title": "Oil weekly report - Google スプレッドシート",
                    "tab_id": 21,
                    "window_id": 8,
                },
            },
            {
                "name": "host.current_tab.extract_text",
                "response": {
                    "ok": True,
                    "text": "WTI weekly trend",
                    "length": 16,
                },
            },
        ],
    )

    assert payload["current_tab"]["info_succeeded"] is True
    assert payload["current_tab"]["url"] == "https://docs.google.com/spreadsheets/d/host123/edit"
    assert payload["current_tab"]["title"] == "Oil weekly report - Google スプレッドシート"
    assert payload["current_tab"]["extract_text_succeeded"] is True
    assert payload["current_tab"]["text_length"] == 16


def test_build_verification_inputs_treats_spreadsheet_goal_as_current_browser():
    payload = _build_verification_inputs(
        plan={"plan_id": "sheet-generic-1"},
        goal="石油の一週間の値動きを調べて、google spreadsheetに記載して",
        executor_outputs=None,
        tool_responses=[],
    )

    assert payload["current_browser_goal"] is True
    assert payload["text_entry_goal"] is True


@pytest.mark.asyncio
async def test_backfill_current_tab_verification_inputs_reads_missing_evidence(
    monkeypatch,
):
    async def fake_current_tab_info(tool_context=None):
        return {
            "success": True,
            "url": "https://docs.google.com/spreadsheets/d/backfill123/edit",
            "title": "Backfill sheet - Google スプレッドシート",
            "tab_id": 99,
            "window_id": 13,
        }

    async def fake_current_tab_extract_text(selector=None, tool_context=None):
        return {
            "success": True,
            "text": "Brent weekly trend summary",
            "length": 26,
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_extract_text",
        fake_current_tab_extract_text,
    )

    payload = await _backfill_current_tab_verification_inputs(
        {
            "current_browser_goal": True,
            "text_entry_goal": True,
            "current_tab": {
                "info_succeeded": False,
                "url": "",
                "title": "",
                "extract_text_succeeded": False,
                "text_excerpt": "",
                "text_length": 0,
            },
        }
    )

    assert payload["current_tab"]["info_succeeded"] is True
    assert payload["current_tab"]["url"] == "https://docs.google.com/spreadsheets/d/backfill123/edit"
    assert payload["current_tab"]["title"] == "Backfill sheet - Google スプレッドシート"
    assert payload["current_tab"]["extract_text_succeeded"] is True
    assert payload["current_tab"]["text_excerpt"] == "Brent weekly trend summary"
    assert payload["current_tab"]["text_length"] == 26


def test_build_final_text_includes_output_location_when_current_tab_url_exists():
    text = _build_final_text(
        {
            StateKeys.TASK_GOAL: "このブラウザでスプレッドシートにまとめて",
            StateKeys.TEMP_VERIFICATION_INPUTS: {
                "current_tab": {
                    "url": "https://docs.google.com/spreadsheets/d/abc123/edit",
                    "title": "WTI prices - Google スプレッドシート",
                }
            },
        },
        {"overall_score": 0.95, "summary": "入力後の状態が確認されました。"},
    )

    assert "Location: https://docs.google.com/spreadsheets/d/abc123/edit" in text
    assert "Page: WTI prices - Google スプレッドシート" in text


def test_browser_text_entry_report_is_demoted_without_destination_evidence():
    report = {
        "status": "pass",
        "overall_score": 0.96,
        "confidence": 0.9,
        "criterion_results": [
            {"name": "sheet_updated", "passed": True, "score": 0.96, "explanation": "typed successfully", "evidence_refs": []}
        ],
        "failure_type": None,
        "summary": "typed successfully",
        "repair_actions": [],
    }
    verification_inputs = {
        "current_browser_goal": True,
        "text_entry_goal": True,
        "current_tab": {
            "url": "",
            "title": "",
            "extract_text_succeeded": False,
            "text_length": 0,
        },
        "desktop": {"screenshot_paths": []},
    }

    assert _should_demote_browser_text_entry_report(
        report=report,
        verification_inputs=verification_inputs,
    ) is True

    demoted = _demote_browser_text_entry_report(
        report=report,
        verification_inputs=verification_inputs,
    )

    assert demoted["status"] == "fail"
    assert demoted["failure_type"] == "insufficient_evidence"
    assert demoted["repair_actions"][0]["target_step_ids"] == ["capture_current_tab_state"]


def test_browser_text_entry_partial_pass_retargets_repair_to_capture_current_tab_state():
    report = {
        "status": "partial_pass",
        "overall_score": 0.75,
        "confidence": 0.8,
        "criterion_results": [
            {
                "name": "data_recorded",
                "passed": False,
                "score": 0.5,
                "explanation": "evidence missing",
                "evidence_refs": ["final_spreadsheet.png"],
            }
        ],
        "failure_type": "insufficient_evidence",
        "summary": "not enough evidence",
        "repair_actions": [
            {
                "action_id": "verify_spreadsheet_content",
                "action_type": "gather_more_evidence",
                "description": "retry spreadsheet verification",
                "target_step_ids": ["fill_spreadsheet"],
                "priority": 2,
            }
        ],
    }
    verification_inputs = {
        "current_browser_goal": True,
        "text_entry_goal": True,
        "current_tab": {
            "url": "",
            "title": "",
            "extract_text_succeeded": False,
            "text_length": 0,
        },
    }

    assert _should_retarget_browser_text_entry_repair(
        report=report,
        verification_inputs=verification_inputs,
    ) is True

    retargeted = _retarget_browser_text_entry_repair(
        report=report,
        verification_inputs=verification_inputs,
    )

    assert retargeted["status"] == "partial_pass"
    assert retargeted["repair_actions"][0]["target_step_ids"] == ["capture_current_tab_state"]
    assert "capture_current_tab_state" in retargeted["summary"]


def test_policy_judge_adds_ax_snapshot_for_generic_desktop_ui_plan():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "Djayを開いて、曲をかけて",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "djay-launch-and-play-001",
                "goal": "Djayを開いて、曲をかけて",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "desktop.control.click", "mode": "execute"},
                    {"name": "desktop.control.launch_app", "mode": "execute"},
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
    assert "desktop.ax.snapshot" in required
    assert "desktop.ax.snapshot" in approval_required


def test_policy_judge_promotes_step_capabilities_and_hotkey_hints():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "もう一度 Djayを開いて、曲をかけて",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "djay-reopen-and-play-001",
                "goal": "もう一度 Djayを開いて、曲をかけて",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.view.windows", "mode": "read"},
                    {"name": "desktop.control.launch_app", "mode": "execute"},
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "desktop.control.click", "mode": "execute"},
                ],
                "steps": [
                    {
                        "step_id": "focus_djay",
                        "title": "Djayウィンドウを前面にする",
                        "description": "desktop.control.focus_windowを使用してDjayのウィンドウを最前面にする。",
                        "capabilities": [
                            {"name": "desktop.control.focus_window", "mode": "execute"},
                            {"name": "desktop.wait.window", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Djayウィンドウがフォーカスされている状態"],
                        "retryable": True,
                    },
                    {
                        "step_id": "play_music",
                        "title": "曲を再生",
                        "description": "DjayのUIから再生ボタンを探してクリックするか、スペースキーで再生を試みる。",
                        "capabilities": [
                            {"name": "desktop.ax.find", "mode": "read"},
                            {"name": "desktop.control.click", "mode": "execute"},
                            {"name": "desktop.view.screenshot", "mode": "read"},
                        ],
                        "depends_on": ["focus_djay"],
                        "expected_outputs": ["曲が再生状態になっていること"],
                        "retryable": True,
                    },
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

    assert "desktop.control.focus_window" in required
    assert "desktop.wait.window" in required
    assert "desktop.view.screenshot" in required
    assert "desktop.control.hotkey" in required
    assert "desktop.control.focus_window" in approval_required
    assert "desktop.wait.window" in approval_required
    assert "desktop.view.screenshot" in approval_required
    assert "desktop.control.hotkey" in approval_required


def test_policy_judge_adds_visual_evidence_capabilities_for_playback_plan():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "Djayを開いて、曲をかけて",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "djay-open-and-play-001",
                "goal": "Djayを開いて、曲をかけて",
                "risk_level": "high",
                "required_capabilities": [
                    {"name": "desktop.control.launch_app", "mode": "execute"},
                    {"name": "desktop.ax.find", "mode": "read"},
                    {"name": "desktop.control.click", "mode": "execute"},
                    {"name": "desktop.view.windows", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "launch_djay",
                        "title": "Djayの起動",
                        "description": "desktop.control.launch_appを使用してDjayを起動する",
                        "capabilities": [
                            {"name": "desktop.control.launch_app", "mode": "execute"},
                            {"name": "desktop.view.windows", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Djayアプリケーションが前面に表示される"],
                        "retryable": True,
                    },
                    {
                        "step_id": "select_track",
                        "title": "楽曲の選択",
                        "description": "Djayのインターフェース内で任意の楽曲を選択する",
                        "capabilities": [
                            {"name": "desktop.ax.find", "mode": "read"},
                            {"name": "desktop.control.click", "mode": "execute"},
                        ],
                        "depends_on": ["launch_djay"],
                        "expected_outputs": ["楽曲が選択状態になる"],
                        "retryable": True,
                    },
                    {
                        "step_id": "play_track",
                        "title": "再生の開始",
                        "description": "再生ボタンをクリックして楽曲を開始する",
                        "capabilities": [
                            {"name": "desktop.control.click", "mode": "execute"},
                        ],
                        "depends_on": ["select_track"],
                        "expected_outputs": ["楽曲が再生中であることの確認"],
                        "retryable": True,
                    },
                ],
                "success_criteria": [
                    {
                        "name": "playback_started",
                        "criterion_type": "evidence",
                        "description": "Djayが起動しており、楽曲の波形や再生インジケーターが動いていること",
                        "required": True,
                    }
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

    assert "desktop.wait.window" in required
    assert "desktop.wait.element" in required
    assert "desktop.ax.snapshot" in required
    assert "desktop.view.screenshot" in required
    assert "desktop.control.focus_window" in required
    assert "desktop.control.hotkey" in required
    assert "desktop.wait.window" in approval_required
    assert "desktop.wait.element" in approval_required
    assert "desktop.view.screenshot" in approval_required
    assert "desktop.control.focus_window" in approval_required
    assert "desktop.control.hotkey" in approval_required
    step_ids = [step["step_id"] for step in approved["steps"]]
    assert "capture_pre_playback_state" in step_ids
    assert "launch_djay_focus" in step_ids
    assert "verify_visual_state" in step_ids
    capture_step = next(
        step for step in approved["steps"] if step["step_id"] == "capture_pre_playback_state"
    )
    focus_step = next(step for step in approved["steps"] if step["step_id"] == "launch_djay_focus")
    verify_step = next(step for step in approved["steps"] if step["step_id"] == "verify_visual_state")
    play_step = next(step for step in approved["steps"] if step["step_id"] == "play_track")
    assert focus_step["depends_on"] == ["launch_djay"]
    assert capture_step["depends_on"] == ["select_track"]
    assert {cap["name"] for cap in capture_step["capabilities"]} == {
        "desktop.view.screenshot",
    }
    assert play_step["depends_on"] == ["capture_pre_playback_state"]
    assert verify_step["depends_on"] == ["play_track"]
    assert "スペースキー" in play_step["description"]
    assert {cap["name"] for cap in play_step["capabilities"]} == {
        "desktop.control.click",
        "desktop.control.hotkey",
    }
    assert {cap["name"] for cap in verify_step["capabilities"]} == {
        "desktop.ax.find",
        "desktop.wait.element",
        "desktop.ax.snapshot",
        "desktop.view.screenshot",
    }


def test_policy_judge_adds_launch_app_fallback_for_playback_stop_plan():
    callback_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "曲を止めて",
            StateKeys.TEMP_PLANNER_DRAFT: {
                "plan_id": "djay-stop-playback-001",
                "goal": "曲を止めて",
                "risk_level": "medium",
                "required_capabilities": [
                    {"name": "desktop.control.focus_window", "mode": "execute"},
                    {"name": "desktop.control.hotkey", "mode": "execute"},
                    {"name": "desktop.ax.snapshot", "mode": "read"},
                    {"name": "desktop.view.screenshot", "mode": "read"},
                    {"name": "desktop.view.windows", "mode": "read"},
                    {"name": "desktop.wait.element", "mode": "read"},
                    {"name": "desktop.wait.window", "mode": "read"},
                ],
                "steps": [
                    {
                        "step_id": "focus_djay",
                        "title": "Djayを前面にする",
                        "description": "Djayウィンドウを前面にして停止操作できる状態にする。",
                        "capabilities": [
                            {"name": "desktop.control.focus_window", "mode": "execute"},
                            {"name": "desktop.wait.window", "mode": "read"},
                        ],
                        "depends_on": [],
                        "expected_outputs": ["Djayウィンドウが前面で操作可能になっていること"],
                        "retryable": True,
                    },
                    {
                        "step_id": "stop_playback",
                        "title": "再生を止める",
                        "description": "スペースキーなどのホットキーで再生停止または一時停止を試みる。",
                        "capabilities": [
                            {"name": "desktop.control.hotkey", "mode": "execute"},
                            {"name": "desktop.wait.element", "mode": "read"},
                        ],
                        "depends_on": ["focus_djay"],
                        "expected_outputs": ["楽曲が停止または一時停止状態になっていること"],
                        "retryable": True,
                    },
                ],
                "success_criteria": [
                    {
                        "name": "playback_stopped",
                        "criterion_type": "evidence",
                        "description": "Djayの再生状態が停止または一時停止であること",
                        "required": True,
                    }
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

    assert "desktop.control.launch_app" in required
    assert "desktop.control.launch_app" in approval_required


def test_extract_latest_agent_json_output_reads_executor_event_text():
    events = [
        SimpleNamespace(
            author="executor",
            invocation_id="inv-123",
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "plan_id": "plan-1",
                                "steps_executed": [
                                    {"step_id": "launch", "status": "succeeded"}
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
            ),
        )
    ]

    parsed, invocation_id = _extract_latest_agent_json_output(events, "executor")

    assert invocation_id == "inv-123"
    assert parsed is not None
    assert parsed["plan_id"] == "plan-1"
    assert parsed["steps_executed"][0]["step_id"] == "launch"


def test_visual_playback_report_can_be_promoted_from_screenshot_change():
    plan = {
        "goal": "Djayを開いて、曲をかけて",
        "steps": [
            {"step_id": "select_and_play_track", "title": "曲の選択と再生", "description": "スペースキーで再生する"}
        ],
        "success_criteria": [
            {
                "name": "djay_is_running",
                "criterion_type": "evidence",
                "description": "Djayアプリが画面上に表示され、曲が再生されている",
                "required": True,
            }
        ],
    }
    report = {
        "report_id": "rep-1",
        "plan_id": "plan-1",
        "status": "fail",
        "overall_score": 0.3,
        "confidence": 0.8,
        "criterion_results": [
            {
                "name": "djay_is_running",
                "passed": False,
                "score": 0.3,
                "explanation": "evidence missing",
                "evidence_refs": ["verify_visual_state"],
            }
        ],
        "failure_type": "insufficient_evidence",
        "summary": "not enough evidence",
        "repair_actions": [{"action_id": "a1", "action_type": "gather_more_evidence", "description": "retry", "target_step_ids": ["verify_visual_state"], "priority": 1}],
    }
    verification_inputs = {
        "goal": "Djayを開いて、曲をかけて",
        "playback_goal": True,
        "desktop": {
            "launch_succeeded": True,
            "focus_succeeded": True,
            "playback_interaction_attempted": True,
            "visual_change": {
                "before_path": "before.png",
                "after_path": "after.png",
                "changed_ratio": 0.01,
                "normalized_rgb_delta": 0.001,
                "playback_ui_changed": True,
            },
        },
    }

    assert _should_promote_visual_playback_report(
        plan=plan,
        goal=plan["goal"],
        report=report,
        verification_inputs=verification_inputs,
    ) is True

    promoted = _promote_visual_playback_report(
        report=report,
        verification_inputs=verification_inputs,
    )

    assert promoted["status"] == "pass"
    assert promoted["failure_type"] is None
    assert promoted["repair_actions"] == []
    assert promoted["criterion_results"][0]["passed"] is True
    assert "before.png" in promoted["criterion_results"][0]["evidence_refs"]
    assert "after.png" in promoted["criterion_results"][0]["evidence_refs"]


def test_build_step_trace_marks_tail_replay_boundary():
    plan = {
        "steps": [
            {"step_id": "launch_djay", "title": "Launch Djay"},
            {"step_id": "verify_visual_state", "title": "Verify visual state"},
        ]
    }
    executor_outputs = {
        "steps_executed": [
            {
                "step_id": "verify_visual_state",
                "status": "succeeded",
                "tool": "guarded_desktop_view_screenshot",
                "output_summary": "captured fresh evidence",
                "artifact_ref": "after.png",
            }
        ]
    }
    report = {
        "status": "partial_pass",
        "summary": "verification improved",
        "overall_score": 0.7,
        "criterion_results": [
            {
                "name": "music_playing",
                "passed": False,
                "evidence_refs": ["verify_visual_state"],
            }
        ],
        "repair_actions": [
            {
                "action_id": "r1",
                "description": "rerun final verification",
                "target_step_ids": ["verify_visual_state"],
            }
        ],
    }

    trace = _build_step_trace(
        plan=plan,
        executor_outputs=executor_outputs,
        report=report,
        replay_context={"from_step": "verify_visual_state", "mode": "tail"},
    )

    assert trace[0]["step_id"] == "launch_djay"
    assert trace[0]["replay_scope"] == "preserved"
    assert trace[1]["step_id"] == "verify_visual_state"
    assert trace[1]["replay_scope"] == "replayed"
    assert trace[1]["artifact_ref"] == "after.png"
    assert _infer_tail_replay_from_step(step_trace=trace, report=report) == "verify_visual_state"


def test_infer_tail_replay_uses_retargeted_browser_text_entry_capture_step():
    plan = {
        "steps": [
            {"step_id": "fill_spreadsheet", "title": "Fill spreadsheet"},
            {"step_id": "capture_current_tab_state", "title": "Capture current tab"},
        ]
    }
    report = {
        "status": "partial_pass",
        "summary": "not enough evidence",
        "overall_score": 0.75,
        "failure_type": "insufficient_evidence",
        "criterion_results": [
            {"name": "data_recorded", "passed": False, "evidence_refs": ["final_spreadsheet.png"]}
        ],
        "repair_actions": [
            {
                "action_id": "verify_spreadsheet_content",
                "action_type": "gather_more_evidence",
                "description": "retry spreadsheet verification",
                "target_step_ids": ["fill_spreadsheet"],
                "priority": 2,
            }
        ],
    }
    verification_inputs = {
        "current_browser_goal": True,
        "text_entry_goal": True,
        "current_tab": {
            "url": "",
            "title": "",
            "extract_text_succeeded": False,
            "text_length": 0,
        },
    }

    retargeted = _retarget_browser_text_entry_repair(
        report=report,
        verification_inputs=verification_inputs,
    )
    trace = _build_step_trace(
        plan=plan,
        executor_outputs={"steps_executed": []},
        report=retargeted,
        replay_context=None,
    )

    assert _infer_tail_replay_from_step(step_trace=trace, report=retargeted) == "capture_current_tab_state"


def test_build_repair_patch_from_report_uses_normalized_repair_actions():
    report = {
        "status": "partial_pass",
        "plan_id": "plan-1",
        "criterion_results": [
            {"name": "data_recorded", "passed": False},
        ],
        "repair_actions": [
            {
                "action_id": "verify_spreadsheet_content",
                "target_step_ids": ["capture_current_tab_state"],
            }
        ],
    }
    state = {
        StateKeys.REPAIR_COUNT: 1,
        StateKeys.PLAN_APPROVED: {"plan_id": "plan-1"},
    }

    patch = _build_repair_patch_from_report(report=report, state=state)

    assert patch is not None
    assert patch["repair_actions"][0]["target_step_ids"] == ["capture_current_tab_state"]
    assert patch["previous_plan_id"] == "plan-1"


def test_build_replay_context_payload_sets_tail_from_step():
    report = {
        "status": "fail",
        "criterion_results": [
            {"name": "data_recorded", "passed": False},
        ],
    }
    step_trace = [
        {
            "step_id": "capture_current_tab_state",
            "title": "Capture current tab",
            "description": "",
            "step_type": "plan",
            "status": "pending",
            "tool": "",
            "artifact_ref": "",
            "output_summary": "not executed in this attempt",
            "replay_scope": "replayed",
            "failed_criteria": [],
            "repair_actions": [],
        }
    ]

    replay_context = _build_replay_context_payload(
        source_task_id="req_123",
        from_step="capture_current_tab_state",
        report=report,
        step_trace=step_trace,
    )

    assert replay_context["source_task_id"] == "req_123"
    assert replay_context["from_step"] == "capture_current_tab_state"
    assert replay_context["mode"] == "tail"


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
async def test_guarded_desktop_control_launch_app_redirects_to_focus_for_playback_task(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "曲を止めて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "plan_id": "djay-stop-playback-001",
                "goal": "曲を止めて",
                "required_capabilities": [
                    {"name": "desktop.control.focus_window"},
                    {"name": "desktop.wait.window"},
                    {"name": "desktop.control.hotkey"},
                ],
                "steps": [
                    {
                        "step_id": "focus_djay",
                        "title": "Djayを前面にする",
                        "description": "Djayウィンドウを前面にして停止操作できる状態にする。",
                        "capabilities": [
                            {"name": "desktop.control.focus_window"},
                            {"name": "desktop.wait.window"},
                        ],
                    }
                ],
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
        app_name="djay Pro",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["target"]["app_name"] == "djay Pro"


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

    async def fake_sleep(_seconds: float) -> None:
        return None

    responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": True,
                "tab_id": 21,
                "url": "chrome://newtab/",
                "title": "New Tab",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(responses)

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )
    monkeypatch.setattr(
        "src.control_loop.guarded_tools.asyncio.sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "t"],
        tool_context=tool_context,
    )

    assert result == {"ok": True, "keys": ["cmd", "t"]}
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] == 1
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [21]


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_retries_current_tab_verification_after_disconnect(
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
    calls = {"count": 0}

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    async def fake_sleep(_seconds: float) -> None:
        return None

    responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": False,
                "url": "",
                "title": "",
                "error": "Current Tab extension disconnected",
            },
            {
                "success": True,
                "tab_id": 22,
                "url": "chrome://newtab/",
                "title": "New Tab",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        calls["count"] += 1
        return next(responses)

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )
    monkeypatch.setattr(
        "src.control_loop.guarded_tools.asyncio.sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "t"],
        tool_context=tool_context,
    )

    assert result == {"ok": True, "keys": ["cmd", "t"]}
    assert calls["count"] == 3
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [22]


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_waits_for_new_tab_id_change(
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
    events: list[str] = []
    sleep_calls = {"count": 0}
    responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": True,
                "tab_id": 21,
                "url": "chrome://newtab/",
                "title": "New Tab",
            },
        ]
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        events.append("hotkey")
        return {"success": True}

    async def fake_sleep(_seconds: float) -> None:
        sleep_calls["count"] += 1
        return None

    async def fake_current_tab_info(tool_context=None):
        events.append("info")
        return next(responses)

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )
    monkeypatch.setattr(
        "src.control_loop.guarded_tools.asyncio.sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "t"],
        tool_context=tool_context,
    )

    assert result == {"success": True}
    assert events == ["info", "hotkey", "info", "info"]
    assert sleep_calls["count"] == 1
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [21]


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_rejects_second_new_tab_when_preserving_control_ui(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザで午後の東京の花粉を調べて",
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
            StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT: 1,
        }
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )

    with pytest.raises(PermissionError, match="Only one new-tab hotkey is allowed"):
        await guarded_tools_module.guarded_desktop_control_hotkey(
            keys=["cmd", "t"],
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_allows_second_new_tab_when_plan_requires_it(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザで石油の値動きを調べて、その後別タブで参考ページを開いて",
            StateKeys.TASK_CONSTRAINTS: [
                (
                    "If the current tab is the boiled-claw Control UI chat, preserve "
                    "that tab and open a new tab in the same browser window for "
                    "browsing or search. Otherwise stay on the current tab."
                )
            ],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.hotkey"}],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "Search Oil Market Trends",
                        "description": "Open a new tab and search for oil market trends.",
                    },
                    {
                        "step_id": "open_spreadsheet",
                        "title": "Open Google Spreadsheet",
                        "description": "Open another new tab and navigate to sheets.new.",
                    },
                ],
            },
            StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT: 1,
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [21],
        }
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    responses = iter(
        [
            {
                "success": True,
                "tab_id": 21,
                "url": "chrome://newtab/",
                "title": "New Tab",
            },
            {
                "success": True,
                "tab_id": 22,
                "url": "chrome://newtab/",
                "title": "New Tab",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(responses)

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    result = await guarded_tools_module.guarded_desktop_control_hotkey(
        keys=["cmd", "t"],
        tool_context=tool_context,
    )

    assert result == {"ok": True, "keys": ["cmd", "t"]}
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] == 2
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [21, 22]


@pytest.mark.asyncio
async def test_guarded_desktop_control_hotkey_rejects_third_new_tab_beyond_plan_requirement(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザで石油の値動きを調べて、その後別タブで参考ページを開いて",
            StateKeys.TASK_CONSTRAINTS: [
                (
                    "If the current tab is the boiled-claw Control UI chat, preserve "
                    "that tab and open a new tab in the same browser window for "
                    "browsing or search. Otherwise stay on the current tab."
                )
            ],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "desktop.control.hotkey"}],
                "steps": [
                    {
                        "step_id": "search_oil_price",
                        "title": "Search Oil Market Trends",
                        "description": "Open a new tab and search for oil market trends.",
                    },
                    {
                        "step_id": "open_spreadsheet",
                        "title": "Open Google Spreadsheet",
                        "description": "Open another new tab and navigate to sheets.new.",
                    },
                ],
            },
            StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT: 2,
        }
    )

    async def fake_hotkey(keys: list[str]) -> dict:
        return {"ok": True, "keys": keys}

    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_hotkey",
        fake_hotkey,
    )

    with pytest.raises(PermissionError, match="Only 2 new-tab hotkeys are allowed"):
        await guarded_tools_module.guarded_desktop_control_hotkey(
            keys=["cmd", "t"],
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_type_blocks_unrelated_current_browser_form(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って午後の東京の花粉を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [11],
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [
                    {"name": "desktop.control.type"},
                    {"name": "current_tab.navigate"},
                ]
            },
        }
    )

    async def fake_current_tab_info(tool_context=None):
        return {
            "success": True,
            "tab_id": 11,
            "url": "https://example.com/contact",
            "title": "Contact Us",
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    with pytest.raises(PermissionError, match="does not match the expected search/spreadsheet destination"):
        await guarded_tools_module.guarded_desktop_control_type(
            text="WTI crude oil: weekly trend",
            role="textbox",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_desktop_control_click_allows_google_sheets_destination(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザのスプレッドシートに石油の一週間の値動きを記入して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [42],
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [
                    {"name": "desktop.control.click"},
                    {"name": "current_tab.navigate"},
                ]
            },
        }
    )

    async def fake_current_tab_info(tool_context=None):
        return {
            "success": True,
            "tab_id": 42,
            "url": "https://docs.google.com/spreadsheets/d/abc123/edit",
            "title": "Oil weekly report - Google Sheets",
        }

    async def fake_click(**kwargs):
        return {"ok": True}

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )
    monkeypatch.setattr(
        "src.tools.desktop.desktop_control_click",
        fake_click,
    )

    result = await guarded_tools_module.guarded_desktop_control_click(
        role="gridcell",
        title="A1",
        tool_context=tool_context,
    )

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_guarded_desktop_control_click_blocks_google_sheets_landing_page(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザのスプレッドシートに石油の一週間の値動きを記入して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [42],
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [
                    {"name": "desktop.control.click"},
                    {"name": "current_tab.navigate"},
                ]
            },
        }
    )

    async def fake_current_tab_info(tool_context=None):
        return {
            "success": True,
            "tab_id": 42,
            "url": "https://docs.google.com/spreadsheets/u/0/",
            "title": "Google Sheets",
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    with pytest.raises(
        PermissionError,
        match="does not match the expected search/spreadsheet destination",
    ):
        await guarded_tools_module.guarded_desktop_control_click(
            role="button",
            title="Blank spreadsheet",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_current_tab_fill_rejects_tab_not_opened_by_task(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザを使って午後の東京の花粉を調べて",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [7],
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}]
            },
        }
    )

    async def fake_current_tab_info(tool_context=None):
        return {
            "success": True,
            "tab_id": 99,
            "url": "https://docs.google.com/spreadsheets/d/abc123/edit",
            "title": "Oil weekly report - Google Sheets",
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )

    with pytest.raises(PermissionError, match="active tab was not opened by this task"):
        await guarded_tools_module.guarded_current_tab_fill(
            selector="#cell-a1",
            text="WTI weekly trend",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_current_tab_navigate_remembers_opened_tab_id(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザのスプレッドシートに石油の一週間の値動きを記入して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}]
            },
        }
    )

    async def fake_current_tab_navigate(url, timeout_ms=15000, new_tab=False, tool_context=None):
        return {
            "success": True,
            "tab_id": 314,
            "url": url,
            "title": "Google Sheets",
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_navigate",
        fake_current_tab_navigate,
    )

    result = await guarded_tools_module.guarded_current_tab_navigate(
        url="https://docs.google.com/spreadsheets/d/abc123/edit",
        tool_context=tool_context,
    )

    assert result["tab_id"] == 314
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [314]


@pytest.mark.asyncio
async def test_guarded_current_tab_navigate_opens_new_tab_from_control_ui(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザの新しいタブで石油の一週間の値動きを調べてスプレッドシートに記入して",
            StateKeys.TASK_CONSTRAINTS: [
                (
                    "If the current tab is the boiled-claw Control UI chat, preserve "
                    "that tab and open a new tab in the same browser window for "
                    "browsing or search. Otherwise stay on the current tab."
                )
            ],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}],
                "steps": [
                    {
                        "step_id": "search_oil_trend",
                        "title": "Search oil price trends",
                        "description": "Open a new tab and search for oil market trends.",
                    }
                ],
            },
        }
    )
    navigate_calls: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(responses)

    async def fake_current_tab_navigate(url, timeout_ms=15000, new_tab=False, tool_context=None):
        navigate_calls.append({"url": url, "new_tab": new_tab})
        return {
            "success": True,
            "tab_id": 21,
            "url": url,
            "title": "Google Sheets",
        }

    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_info",
        fake_current_tab_info,
    )
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_navigate",
        fake_current_tab_navigate,
    )

    result = await guarded_tools_module.guarded_current_tab_navigate(
        url="https://docs.google.com/spreadsheets/create",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert navigate_calls == [
        {
            "url": "https://docs.google.com/spreadsheets/create",
            "new_tab": True,
        }
    ]
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] == 1
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [21]


@pytest.mark.asyncio
async def test_guarded_current_tab_navigate_preserves_control_ui_without_constraint(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.TASK_CONSTRAINTS: [],
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}],
                "steps": [
                    {
                        "step_id": "search_oil_trend",
                        "title": "Search oil price trends",
                        "description": "Search for oil market trends.",
                    }
                ],
            },
        }
    )
    navigate_calls: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(responses)

    async def fake_current_tab_navigate(url, timeout_ms=15000, new_tab=False, tool_context=None):
        navigate_calls.append({"url": url, "new_tab": new_tab})
        return {
            "success": True,
            "tab_id": 22,
            "url": url,
            "title": "Google Sheets",
        }

    monkeypatch.setattr("src.tools.current_tab.current_tab_info", fake_current_tab_info)
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_navigate",
        fake_current_tab_navigate,
    )

    result = await guarded_tools_module.guarded_current_tab_navigate(
        url="https://docs.google.com/spreadsheets/create",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert navigate_calls == [
        {
            "url": "https://docs.google.com/spreadsheets/create",
            "new_tab": True,
        }
    ]
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] == 1
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] == [22]


@pytest.mark.asyncio
async def test_guarded_current_tab_navigate_reuses_existing_opened_tab_from_control_ui(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}],
            },
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [21],
        }
    )
    activate_calls: list[int] = []
    navigate_calls: list[dict[str, object]] = []
    info_responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": True,
                "tab_id": 21,
                "url": "https://www.eemarket.net/commodity/crudeoil.html",
                "title": "Oil prices",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(info_responses)

    async def fake_current_tab_activate(tab_id, tool_context=None):
        activate_calls.append(tab_id)
        return {
            "success": True,
            "tab_id": tab_id,
            "url": "https://www.eemarket.net/commodity/crudeoil.html",
            "title": "Oil prices",
        }

    async def fake_current_tab_navigate(url, timeout_ms=15000, new_tab=False, tool_context=None):
        navigate_calls.append({"url": url, "new_tab": new_tab})
        return {
            "success": True,
            "tab_id": 21,
            "url": url,
            "title": "Google Sheets",
        }

    monkeypatch.setattr("src.tools.current_tab.current_tab_info", fake_current_tab_info)
    monkeypatch.setattr("src.tools.current_tab.current_tab_activate", fake_current_tab_activate)
    monkeypatch.setattr("src.tools.current_tab.current_tab_navigate", fake_current_tab_navigate)

    result = await guarded_tools_module.guarded_current_tab_navigate(
        url="https://docs.google.com/spreadsheets/create",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert activate_calls == [21]
    assert navigate_calls == [
        {
            "url": "https://docs.google.com/spreadsheets/create",
            "new_tab": False,
        }
    ]
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] == 21


@pytest.mark.asyncio
async def test_guarded_current_tab_extract_text_reuses_existing_opened_tab_from_control_ui(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "石油の一週間の値動きを調べて、google spreadsheetに記載して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}],
            },
            StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: [31],
        }
    )
    activate_calls: list[int] = []
    info_responses = iter(
        [
            {
                "success": True,
                "tab_id": 10,
                "url": "http://localhost:18789/chat",
                "title": "boiled-claw Control UI",
            },
            {
                "success": True,
                "tab_id": 31,
                "url": "https://www.eemarket.net/commodity/crudeoil.html",
                "title": "Oil prices",
            },
        ]
    )

    async def fake_current_tab_info(tool_context=None):
        return next(info_responses)

    async def fake_current_tab_activate(tab_id, tool_context=None):
        activate_calls.append(tab_id)
        return {
            "success": True,
            "tab_id": tab_id,
            "url": "https://www.eemarket.net/commodity/crudeoil.html",
            "title": "Oil prices",
        }

    async def fake_current_tab_extract_text(selector=None, tool_context=None):
        return {
            "success": True,
            "selector": selector or "body",
            "text": "WTI 71.5",
            "length": 8,
        }

    monkeypatch.setattr("src.tools.current_tab.current_tab_info", fake_current_tab_info)
    monkeypatch.setattr("src.tools.current_tab.current_tab_activate", fake_current_tab_activate)
    monkeypatch.setattr(
        "src.tools.current_tab.current_tab_extract_text",
        fake_current_tab_extract_text,
    )

    result = await guarded_tools_module.guarded_current_tab_extract_text(
        selector="body",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert activate_calls == [31]
    assert tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] == 31


@pytest.mark.asyncio
async def test_guarded_browser_navigate_redirects_to_current_tab_for_current_browser_task(
    monkeypatch,
):
    tool_context = SimpleNamespace(
        state={
            StateKeys.TASK_GOAL: "このブラウザで石油の一週間の値動きを調べて google spreadsheet に記載して",
            StateKeys.APPROVAL_STATUS: "human_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "current_tab.navigate"}],
            },
        }
    )
    navigate_calls: list[dict[str, object]] = []

    async def fake_current_tab_navigate(url, timeout_ms=15000, new_tab=False, tool_context=None):
        navigate_calls.append({"url": url, "new_tab": new_tab})
        return {
            "success": True,
            "tab_id": 55,
            "url": url,
            "title": "Google Sheets",
        }

    async def fake_browser_navigate(url):
        raise AssertionError("browser_navigate should not be used for current-browser tasks")

    monkeypatch.setattr("src.tools.current_tab.current_tab_navigate", fake_current_tab_navigate)
    monkeypatch.setattr("src.tools.browser.browser_navigate", fake_browser_navigate)

    result = await guarded_tools_module.guarded_browser_navigate(
        url="https://docs.google.com/spreadsheets/create",
        tool_context=tool_context,
    )

    assert result["success"] is True
    assert result["tab_id"] == 55
    assert navigate_calls == [
        {
            "url": "https://docs.google.com/spreadsheets/create",
            "new_tab": False,
        }
    ]


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

    async def fake_run_agent(agent, *, session_id, user_id, message, image_paths=None):
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
async def test_run_agent_attaches_png_screenshots(tmp_path):
    image_path = tmp_path / "evidence.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["runner_kwargs"] = kwargs

        async def run_async(self, *, user_id, session_id, new_message):
            captured["user_id"] = user_id
            captured["session_id"] = session_id
            captured["new_message"] = new_message
            if False:
                yield None
            return

    loop = ControlLoop(max_repair_attempts=0)

    from src.control_loop import root_workflow as root_workflow_module

    original_runner = root_workflow_module.Runner
    root_workflow_module.Runner = FakeRunner
    try:
        await loop._run_agent(
            planner_with_policy,
            session_id="sess-attach",
            user_id="user-attach",
            message="Verify execution results.",
            image_paths=[str(image_path)],
        )
    finally:
        root_workflow_module.Runner = original_runner

    message = captured["new_message"]
    assert message.role == "user"
    assert len(message.parts) == 2
    assert message.parts[0].text == "Verify execution results."
    assert message.parts[1].inline_data.mime_type == "image/png"
    assert message.parts[1].inline_data.data == image_path.read_bytes()


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
        reset_if_terminal=False,
    )

    assert created is False
    assert session.id == existing.id
    assert session.state[StateKeys.TASK_GOAL] == "New goal"
    assert session.state[StateKeys.TASK_CONSTRAINTS] == ["fresh"]
    assert session.state.get(StateKeys.PLAN_APPROVED) is None
    assert session.state.get(StateKeys.APPROVAL_STATUS) is None
    assert session.state.get(StateKeys.VERIFY_LAST_REPORT) is None
    assert session.state.get(StateKeys.TEMP_REPAIR_PATCH) is None
