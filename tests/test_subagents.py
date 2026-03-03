import asyncio
from types import SimpleNamespace

import pytest

from src.tools import subagents as subagent_tools


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

    run = await _wait_for_status(spawn["run_id"], ctx, {"completed"})
    assert run is not None
    assert run["messages_processed"] == 1
    assert "processed:collect facts" in run["last_result"]


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
