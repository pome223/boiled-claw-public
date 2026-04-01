from types import SimpleNamespace

import pytest

from src.runtime import task_store as task_store_module
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


@pytest.mark.asyncio
async def test_task_list_supports_query_and_pagination():
    ctx = _tool_context()
    await task_create(
        kind="repair",
        title="Repair selector for save button",
        artifacts_json='{"selector":"#save"}',
        tool_context=ctx,
    )
    await task_create(
        kind="repair",
        title="Repair query field",
        artifacts_json='{"selector":"#query"}',
        tool_context=ctx,
    )

    result = await task_list(
        query="#save",
        page=1,
        page_size=1,
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["tasks"][0]["title"] == "Repair selector for save button"
    assert result["pagination"]["page"] == 1
    assert result["pagination"]["page_size"] == 1
    assert result["pagination"]["total"] == 1


def test_task_store_reopen_skips_full_search_rebuild(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.db"
    store = task_store_module.TaskStore(str(db_path))
    store.create(
        kind="repair",
        title="Repair selector for save button",
        artifacts={"selector": "#save"},
    )

    rebuild_calls = 0
    original_rebuild = task_store_module.TaskStore._rebuild_search_index

    def _spy(self, cursor):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return original_rebuild(self, cursor)

    monkeypatch.setattr(task_store_module.TaskStore, "_rebuild_search_index", _spy)
    reopened = task_store_module.TaskStore(str(db_path))

    result = reopened.query(q="#save", page=1, page_size=10)

    assert rebuild_calls == 0
    assert result["pagination"]["total"] == 1
    assert result["tasks"][0]["title"] == "Repair selector for save button"
