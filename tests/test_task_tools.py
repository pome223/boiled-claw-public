import sqlite3
from types import SimpleNamespace

import pytest

from src.runtime import task_store as task_store_module
from src.tools import tasks as tasks_module
from src.tools.tasks import task_create, task_get, task_list, task_update


def _tool_context(user_id: str = "test-user", session_id: str = "sess-1"):
    session = SimpleNamespace(id=session_id, app_name="boiled-claw")
    return SimpleNamespace(user_id=user_id, session=session)


def test_tasks_module_uses_public_task_store_unset_sentinel():
    assert tasks_module.TASK_UPDATE_ERROR_UNSET is task_store_module.TASK_STORE_UNSET


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


@pytest.mark.asyncio
async def test_task_store_timeline_records_create_and_update():
    ctx = _tool_context()
    created = await task_create(
        kind="repair",
        title="Timeline demo",
        status="pending",
        artifacts_json='{"selector":"#save"}',
        tool_context=ctx,
    )

    updated = await task_update(
        task_id=created["task_id"],
        status="running",
        artifacts_json='{"step":"observe"}',
        tool_context=ctx,
    )

    timeline = task_store_module.get_task_store().query_timeline(created["task_id"], page=1, page_size=10)

    assert updated["success"] is True
    assert timeline["pagination"]["total"] == 2
    assert timeline["events"][0]["event_type"] == "status_changed"
    assert timeline["events"][0]["payload"]["changes"]["status"]["after"] == "running"
    assert timeline["events"][1]["event_type"] == "created"


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


def test_task_store_supports_custom_step_events(tmp_path):
    db_path = tmp_path / "tasks.db"
    store = task_store_module.TaskStore(str(db_path))
    task = store.create(
        kind="control_loop",
        title="Desktop playback",
        status="running",
        owner_session_id="sess-step",
        owner_user_id="alice",
    )

    event = store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="verify_visual_state",
        status="pending",
        payload={
            "summary": "waiting for visual confirmation",
            "step": {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "status": "pending",
            },
        },
    )

    timeline = store.query_timeline(task["task_id"], page=1, page_size=10)

    assert event is not None
    assert timeline["pagination"]["total"] == 2
    assert timeline["events"][0]["event_type"] == "step_plan"
    assert timeline["events"][0]["payload"]["step"]["step_id"] == "verify_visual_state"
    assert timeline["events"][0]["payload"]["summary"] == "waiting for visual confirmation"


def test_task_store_backfills_split_search_fragments_for_legacy_rows(tmp_path):
    db_path = tmp_path / "tasks.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_session_id TEXT,
                owner_user_id TEXT,
                parent_task_id TEXT,
                run_id TEXT,
                winner_task_id TEXT,
                loser_task_ids_json TEXT,
                approval_dependencies_json TEXT,
                artifacts_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                search_text TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                ended_at REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, kind, title, status, owner_session_id, owner_user_id,
                parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                approval_dependencies_json, artifacts_json, metadata_json, search_text,
                error, created_at, updated_at, started_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task_legacy",
                "repair",
                "Legacy repair selector",
                "failed",
                "sess-legacy",
                "alice",
                None,
                "run-legacy",
                None,
                "[]",
                '["apr_legacy"]',
                '{"selector":"#save","summary":"save button moved"}',
                '{"failure_reason":"selector drift","surface":"current_tab"}',
                "",
                "selector failed",
                1.0,
                1.0,
                1.0,
                1.0,
            ),
        )
        conn.commit()

    reopened = task_store_module.TaskStore(str(db_path))
    result = reopened.query(q="#save", page=1, page_size=10)

    assert result["pagination"]["total"] == 1
    assert result["tasks"][0]["task_id"] == "task_legacy"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT artifacts_search_text, metadata_search_text, search_text
            FROM tasks
            WHERE task_id = ?
            """,
            ("task_legacy",),
        ).fetchone()

    assert row is not None
    assert "#save" in row[0]
    assert "selector drift" in row[1]
    assert "legacy repair selector" in row[2]
