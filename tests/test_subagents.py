import asyncio
from types import SimpleNamespace
import time

import pytest

from src.tools import subagents as subagent_tools
from src.security.tool_policy import ToolPolicyEngine
from src.tools.tasks import task_get


def _tool_context(user_id: str = "test-user", session_id: str = "sess-1"):
    session = SimpleNamespace(id=session_id, app_name="boiled-claw")
    return SimpleNamespace(user_id=user_id, session=session)


async def _wait_for_status(
    run_id: str,
    ctx,
    target_statuses: set[str],
    timeout_seconds: float = 1.0,
):
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_seconds:
        result = await subagent_tools.subagents_list(tool_context=ctx)
        run = next((item for item in result["runs"] if item["run_id"] == run_id), None)
        if run and run["status"] in target_statuses:
            return run
        await asyncio.sleep(0.02)
    return None


@pytest.fixture(autouse=True)
async def _cleanup_subagent_manager():
    await subagent_tools.reset_subagent_manager_for_tests()
    yield
    await subagent_tools.reset_subagent_manager_for_tests()


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_unknown_agent():
    ctx = _tool_context()
    result = await subagent_tools.sessions_spawn(
        task="hello",
        agent_id="does_not_exist",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "available_agents" in result


@pytest.mark.asyncio
async def test_sessions_spawn_run_mode_completes(monkeypatch):
    manager = subagent_tools.get_subagent_manager()

    async def fake_turn(self, *, runner, user_id, session_id, message, run_timeout_seconds):
        return f"processed:{message}"

    monkeypatch.setattr(
        manager,
        "_run_agent_turn",
        fake_turn.__get__(manager, type(manager)),
    )

    ctx = _tool_context(session_id="sess-run")
    spawn = await subagent_tools.sessions_spawn(
        task="collect facts",
        agent_id="web_researcher",
        mode="run",
        tool_context=ctx,
    )
    assert spawn["status"] == "accepted"
    assert spawn["task_id"].startswith("task_")

    run = await _wait_for_status(spawn["run_id"], ctx, {"completed"})
    assert run is not None
    assert run["task_id"] == spawn["task_id"]
    assert run["messages_processed"] == 1
    assert "processed:collect facts" in run["last_result"]

    task = await task_get(task_id=spawn["task_id"], tool_context=ctx)
    assert task["success"] is True
    assert task["task"]["status"] == "completed"
    assert task["task"]["run_id"] == spawn["run_id"]


@pytest.mark.asyncio
async def test_session_mode_supports_steer_and_kill(monkeypatch):
    manager = subagent_tools.get_subagent_manager()

    async def fake_turn(self, *, runner, user_id, session_id, message, run_timeout_seconds):
        return f"ok:{message}"

    monkeypatch.setattr(
        manager,
        "_run_agent_turn",
        fake_turn.__get__(manager, type(manager)),
    )

    ctx = _tool_context(session_id="sess-session")
    spawn = await subagent_tools.sessions_spawn(
        task="first task",
        agent_id="web_researcher",
        mode="session",
        tool_context=ctx,
    )
    assert spawn["status"] == "accepted"

    run = await _wait_for_status(spawn["run_id"], ctx, {"idle"})
    assert run is not None
    assert run["task_id"] == spawn["task_id"]

    steer = await subagent_tools.subagents_steer(
        run_id=spawn["run_id"],
        message="second task",
        tool_context=ctx,
    )
    assert steer["success"] is True

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < 1.0:
        listed = await subagent_tools.subagents_list(tool_context=ctx)
        run = next((item for item in listed["runs"] if item["run_id"] == spawn["run_id"]), None)
        if run and run["messages_processed"] >= 2:
            break
        await asyncio.sleep(0.02)

    assert run is not None
    assert run["messages_processed"] >= 2

    killed = await subagent_tools.subagents_kill(run_id=spawn["run_id"], tool_context=ctx)
    assert killed["success"] is True
    assert killed["status"] == "cancelled"
    assert killed["task_id"] == spawn["task_id"]


@pytest.mark.asyncio
async def test_sessions_spawn_propagates_parent_approvals(monkeypatch):
    manager = subagent_tools.get_subagent_manager()
    engine = ToolPolicyEngine()
    monkeypatch.setattr(subagent_tools, "get_tool_policy_engine", lambda: engine)

    approval = engine.create_approval_request(
        request_id="req-prop",
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo hi"},
        session_id="sess-parent",
        reason="shell commands need approval",
    )
    engine.resolve_approval(
        approval.request_id,
        True,
        "approved for session",
        scope="session",
        tool_pattern="run_shell",
        expires_at=time.time() + 60,
        propagate_to_subagents=True,
    )

    async def fake_turn(self, *, runner, user_id, session_id, message, run_timeout_seconds):
        return f"processed:{message}"

    monkeypatch.setattr(
        manager,
        "_run_agent_turn",
        fake_turn.__get__(manager, type(manager)),
    )

    ctx = _tool_context(session_id="sess-parent")
    spawn = await subagent_tools.sessions_spawn(
        task="collect facts",
        agent_id="web_researcher",
        mode="run",
        tool_context=ctx,
    )

    run = await _wait_for_status(spawn["run_id"], ctx, {"completed"})
    assert run is not None
    assert run["propagated_approvals"] == 1
    task = await task_get(task_id=spawn["task_id"], tool_context=ctx)
    assert task["success"] is True
    assert task["task"]["approval_dependencies"] == ["req-prop"]

    approvals = engine.list_approvals(
        session_id=run["session_id"],
        state="propagated",
    )
    assert len(approvals) == 1
    assert approvals[0]["source_request_id"] == "req-prop"
