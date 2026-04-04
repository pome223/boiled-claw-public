import asyncio

import pytest

from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import ControlLoopSupervisor, build_maintenance_goal
from src.runtime.task_store import TaskStore


def test_build_maintenance_goal_wraps_objective():
    goal = build_maintenance_goal("Keep the desktop media session healthy")

    assert "Maintain the following long-running objective" in goal
    assert "Keep the desktop media session healthy" in goal


@pytest.mark.asyncio
async def test_control_supervisor_completes_and_records_child_tasks(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    session_events: list[tuple[str, str, str]] = []
    child_calls: list[tuple[str, str, str]] = []

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        session_events.append((session_id, status, message))

    async def _run_control_loop_with_task(**kwargs):
        call_index = len(child_calls) + 1
        child_calls.append(
            (
                kwargs["session_id"],
                kwargs["owner_session_id"],
                kwargs["goal"],
            )
        )
        child_task = store.create(
            kind="control_loop",
            title=f"iteration {call_index}",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
            artifacts={"result": {"success": True, "final_text": "ok"}},
        )
        return (
            ExecutionResult(
                request_id=f"req-{call_index}",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok",
                success=True,
                metadata={"task_id": child_task["task_id"]},
            ),
            child_task["task_id"],
        )

    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        create_task_record_fn=_create_task_record,
        update_task_record_fn=_update_task_record,
        append_task_event_record_fn=_append_task_event_record,
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-owner",
        objective="Keep the session healthy",
        constraints=["Do not leave the current application"],
        duration_seconds=120,
        interval_seconds=0,
        source="test",
        max_iterations=2,
    )
    handles = list(supervisor._handles.values())
    await asyncio.gather(*(handle.task for handle in handles))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "completed"
    assert parent["artifacts"]["progress"]["completed_iterations"] == 2
    assert len(parent["artifacts"]["progress"]["child_task_ids"]) == 2
    assert child_calls[0][0].startswith("ctrlsup_")
    assert child_calls[0][1] == "sess-owner"
    assert child_calls[0][2] == build_maintenance_goal("Keep the session healthy")
    assert session_events[0][1] == "accepted"
    assert session_events[-1][1] == "completed"


@pytest.mark.asyncio
async def test_control_supervisor_graceful_stop_marks_task_cancelled(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    release_first_iteration = asyncio.Event()

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_task = store.create(
            kind="control_loop",
            title="iteration",
            status="running",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        if not release_first_iteration.is_set():
            release_first_iteration.set()
            await asyncio.sleep(0)
        store.update(
            child_task["task_id"],
            status="completed",
            artifacts={"result": {"success": True, "final_text": "ok"}},
        )
        return (
            ExecutionResult(
                request_id="req-1",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok",
                success=True,
            ),
            child_task["task_id"],
        )

    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        create_task_record_fn=_create_task_record,
        update_task_record_fn=_update_task_record,
        append_task_event_record_fn=_append_task_event_record,
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-owner",
        objective="Keep the session healthy",
        constraints=[],
        duration_seconds=120,
        interval_seconds=60,
        source="test",
        max_iterations=3,
    )
    await release_first_iteration.wait()
    updated = await supervisor.request_stop(started.task["task_id"])
    assert updated is not None
    await supervisor.shutdown()

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "cancelled"
    assert parent["artifacts"]["progress"]["stop_requested"] is True
    assert parent["artifacts"]["progress"]["completed_iterations"] == 1
