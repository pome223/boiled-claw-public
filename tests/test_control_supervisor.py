import asyncio

import pytest

from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import ControlLoopSupervisor, build_maintenance_goal
from src.runtime.durable_execution_schema import GuardrailBudgetPolicy
from src.runtime.mission_contract import MissionContract, build_mission_contract
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
    assert child_calls[0][2] == build_maintenance_goal(
        "Keep the session healthy",
        MissionContract.model_validate(started.mission_contract),
    )
    assert session_events[0][1] == "accepted"
    assert session_events[-1][1] == "completed"


@pytest.mark.asyncio
async def test_control_supervisor_persists_mission_contract_in_live_artifacts(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_goals: list[str] = []
    mission_contract = build_mission_contract(
        contract_id="mission-live-test",
        objective="Keep the current-tab sheet healthy",
        allowed_actions=["current_tab.read", "current_tab.fill"],
        forbidden_actions=["switch away from the target sheet"],
        abort_conditions=["human approval required"],
        completion_criteria=["target cell evidence is visible"],
        evidence_requirements=["post-action screenshot"],
    )

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_goals.append(kwargs["goal"])
        child_task = store.create(
            kind="control_loop",
            title="iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
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
        objective="legacy objective should not win",
        constraints=["legacy constraint"],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["title"] == "Keep the current-tab sheet healthy"
    assert parent["artifacts"]["mission_contract"]["contract_id"] == "mission-live-test"
    assert parent["metadata"]["mission_contract_id"] == "mission-live-test"
    assert "Mission contract:" in child_goals[0]
    assert "target cell evidence is visible" in child_goals[0]
    durable = parent["artifacts"]["durable_execution"]
    assert durable["mission_contract"]["contract_id"] == "mission-live-test"
    assert durable["task_graph"]["metadata"]["mission_contract_id"] == "mission-live-test"
    node = durable["task_graph"]["nodes"][0]
    assert node["completion_criteria"] == ["target cell evidence is visible"]
    assert node["metadata"]["evidence_requirements"] == ["post-action screenshot"]
    assert node["artifacts"][0]["kind"] == "mission_contract"
    queue_entry = durable["scheduler_state"]["completed_queue"][0]
    assert queue_entry["metadata"]["mission_contract_id"] == "mission-live-test"


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


@pytest.mark.asyncio
async def test_control_supervisor_retries_retry_later_queue_and_completes(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_calls: list[int] = []

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        call_index = len(child_calls) + 1
        child_calls.append(call_index)
        child_task = store.create(
            kind="control_loop",
            title=f"iteration {call_index}",
            status="failed" if call_index == 1 else "completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
            artifacts={
                "result": {
                    "success": call_index != 1,
                    "normalized_failure_type": (
                        "target_context_mismatch" if call_index == 1 else None
                    ),
                }
            },
        )
        if call_index == 1:
            return (
                ExecutionResult(
                    request_id="req-1",
                    session_id=kwargs["session_id"],
                    user_id=kwargs["user_id"],
                    final_text="Current tab lost the target context.",
                    success=False,
                    metadata={"normalized_failure_type": "target_context_mismatch"},
                ),
                child_task["task_id"],
            )
        return (
            ExecutionResult(
                request_id="req-2",
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
        objective="Keep the sheet healthy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=2,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "completed"
    assert child_calls == [1, 2]
    durable = parent["artifacts"]["durable_execution"]
    assert len(durable["job_runs"]) == 2
    assert durable["job_runs"][0]["scheduler_queue"] == "retry_later"
    assert durable["job_runs"][0]["verifier_verdict"]["failure_type"] == "target_context_mismatch"
    assert durable["job_runs"][1]["status"] == "completed"
    assert durable["scheduler_state"]["completed_queue"][0]["node_id"].endswith("/maintain-objective")
    timeline = store.query_timeline(started.task["task_id"])
    waiting_events = [
        event
        for event in timeline["events"]
        if event["event_type"] == "supervisor_waiting"
    ]
    assert waiting_events
    assert waiting_events[0]["payload"]["scheduler_queue"] == "retry_later"


@pytest.mark.asyncio
async def test_control_supervisor_blocks_when_retry_budget_is_exhausted(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_calls: list[int] = []

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        call_index = len(child_calls) + 1
        child_calls.append(call_index)
        child_task = store.create(
            kind="control_loop",
            title=f"iteration {call_index}",
            status="failed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id=f"req-{call_index}",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Current tab lost the target context.",
                success=False,
                metadata={"normalized_failure_type": "target_context_mismatch"},
            ),
            child_task["task_id"],
        )

    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        create_task_record_fn=_create_task_record,
        update_task_record_fn=_update_task_record,
        append_task_event_record_fn=_append_task_event_record,
        budget_policy=GuardrailBudgetPolicy(max_same_failure_retries=1),
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-owner",
        objective="Keep the sheet healthy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=3,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "blocked"
    assert child_calls == [1, 2]
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["blocked_queue"][0]["reason"] == "guardrail_budget_exhausted"
    assert durable["checkpoints"][-1]["budget"]["budget_exhausted"] is True
    assert durable["checkpoints"][-1]["budget"]["budget_exhausted_reasons"] == [
        "max_same_failure_retries_exhausted"
    ]
    assert durable["resume_state"]["reason"] == "awaiting_unblock_or_human_input"


@pytest.mark.asyncio
async def test_control_supervisor_weak_evidence_waits_for_approval(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))

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
            status="failed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-1",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Verifier could not confirm destination evidence.",
                success=False,
                metadata={
                    "verification_status": "partial_pass",
                    "verification_report": {"failure_type": "insufficient_evidence"},
                },
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
        objective="Keep the sheet healthy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=2,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "pending"
    assert parent["artifacts"]["result"]["scheduler_queue"] == "waiting_for_approval"
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["waiting_for_approval_queue"][0]["node_id"].endswith("/maintain-objective")
    assert durable["escalations"][0]["approval_request_id"].startswith("approval:")
    assert durable["resume_state"]["reason"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_control_supervisor_classifies_child_policy_exception(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        raise PermissionError("Capability 'current_tab.navigate' is not in the approved plan.")

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
        objective="Keep the current browser session healthy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=2,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "pending"
    assert parent["artifacts"]["result"]["failure_type"] == "policy_blocked"
    assert parent["artifacts"]["result"]["scheduler_queue"] == "waiting_for_approval"
    assert parent["artifacts"]["progress"]["last_child_task_id"].endswith(
        "/supervisor-exception-1"
    )
    durable = parent["artifacts"]["durable_execution"]
    assert durable["job_runs"][0]["verifier_verdict"]["failure_type"] == "policy_blocked"
    assert durable["scheduler_state"]["waiting_for_approval_queue"][0]["reason"] == (
        "human_approval_required"
    )
