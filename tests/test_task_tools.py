from types import SimpleNamespace

import pytest

from src.tools.tasks import task_create, task_get, task_list, task_update


def _tool_context(user_id: str = "test-user", session_id: str = "sess-1"):
    session = SimpleNamespace(id=session_id, app_name="boiled-claw")
    return SimpleNamespace(user_id=user_id, session=session)


@pytest.mark.asyncio
async def test_task_create_get_update_and_list_round_trip():
    ctx = _tool_context()
    created = await task_create(
        kind="demo",
        title="Track a workflow",
        status="running",
        artifacts_json='{"step":"prepare"}',
        metadata_json='{"owner":"demo"}',
        approval_dependencies_json='["apr_1"]',
        tool_context=ctx,
    )

    assert created["success"] is True
    task_id = created["task_id"]
    assert created["task"]["approval_dependencies"] == ["apr_1"]

    fetched = await task_get(task_id=task_id, tool_context=ctx)
    assert fetched["success"] is True
    assert fetched["task"]["artifacts"]["step"] == "prepare"

    updated = await task_update(
        task_id=task_id,
        status="completed",
        artifacts_json='{"package":{"promotable":true}}',
        loser_task_ids_json='["task_loser"]',
        winner_task_id="task_winner",
        tool_context=ctx,
    )
    assert updated["success"] is True
    assert updated["task"]["status"] == "completed"
    assert updated["task"]["winner_task_id"] == "task_winner"
    assert updated["task"]["loser_task_ids"] == ["task_loser"]
    assert updated["task"]["artifacts"]["step"] == "prepare"
    assert updated["task"]["artifacts"]["package"]["promotable"] is True

    listed = await task_list(tool_context=ctx)
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["tasks"][0]["task_id"] == task_id


@pytest.mark.asyncio
async def test_task_get_rejects_other_session():
    created = await task_create(
        kind="demo",
        title="Private task",
        tool_context=_tool_context(session_id="owner"),
    )
    result = await task_get(task_id=created["task_id"], tool_context=_tool_context(session_id="other"))
    assert result["success"] is False
    assert "not owned by this session" in result["error"]
