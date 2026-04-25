import asyncio
from datetime import datetime, timezone

import pytest

from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import (
    ControlLoopSupervisor,
    _SupervisorHandle,
    build_maintenance_goal,
)
from src.runtime.durable_execution_schema import (
    DurableTaskNodeStatus,
    GuardrailBudgetPolicy,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)
from src.runtime.mission_contract import (
    MissionAbortConditionType,
    MissionContract,
    build_mission_contract,
)
from src.runtime.task_store import TaskStore


def test_build_maintenance_goal_wraps_objective():
    goal = build_maintenance_goal("Keep the desktop media session healthy")

    assert "Maintain the following long-running objective" in goal
    assert "Keep the desktop media session healthy" in goal


def _create_running_supervisor_task(
    store: TaskStore,
    mission_contract: MissionContract,
    *,
    control_session_id: str = "ctrlsup_resume_hardened",
    now: float = 1000.0,
    status: str = "running",
    stop_requested: bool = False,
) -> dict:
    return store.create(
        kind="control_supervisor",
        title=mission_contract.objective,
        status=status,
        owner_session_id="sess-owner",
        owner_user_id="alice",
        artifacts={
            "mission_contract": mission_contract.model_dump(mode="json"),
            "supervisor": {
                "objective": mission_contract.objective,
                "loop_goal": build_maintenance_goal(
                    mission_contract.objective,
                    mission_contract,
                ),
                "constraints": [],
                "mission_contract_id": mission_contract.contract_id,
                "duration_seconds": 120,
                "interval_seconds": 0,
                "control_session_id": control_session_id,
                "started_at": now,
                "ends_at": now + 120,
                "max_iterations": 1,
            },
            "progress": {
                "iteration": 0,
                "completed_iterations": 0,
                "next_run_at": now,
                "child_task_ids": [],
                "stop_requested": stop_requested,
            },
        },
        metadata={
            "type": "control_supervisor",
            "control_session_id": control_session_id,
            "mission_contract_id": mission_contract.contract_id,
        },
    )


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

    async def _emit_session_event(
        session_id: str,
        *,
        source: str,
        status: str,
        message: str,
        **kwargs,
    ):
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
    assert durable["mission_contract"]["abort_conditions"][0]["type"] == (
        MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED.value
    )
    assert durable["task_graph"]["metadata"]["mission_contract_id"] == "mission-live-test"
    node = durable["task_graph"]["nodes"][0]
    assert node["completion_criteria"] == ["target cell evidence is visible"]
    assert node["metadata"]["evidence_requirements"] == ["post-action screenshot"]
    assert node["metadata"]["abort_condition_types"] == [
        MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED.value
    ]
    assert node["artifacts"][0]["kind"] == "mission_contract"
    queue_entry = durable["scheduler_state"]["completed_queue"][0]
    assert queue_entry["metadata"]["mission_contract_id"] == "mission-live-test"
    assert queue_entry["metadata"]["abort_conditions"][0]["type"] == (
        MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED.value
    )


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
    assert "mission_review" not in parent["artifacts"]
    assert parent["error"] is None


@pytest.mark.asyncio
async def test_control_supervisor_shutdown_leaves_running_task_resumable(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    waiting_for_next_iteration = asyncio.Event()

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        if kwargs.get("event_type") == "supervisor_waiting":
            waiting_for_next_iteration.set()
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_task = store.create(
            kind="control_loop",
            title="iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
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
    await waiting_for_next_iteration.wait()
    await supervisor.shutdown()

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "running"
    assert parent["artifacts"]["progress"]["completed_iterations"] == 1
    assert parent["artifacts"]["progress"].get("stop_requested") is not True
    assert parent["artifacts"]["progress"]["next_run_at"] is not None
    events = store.query_timeline(started.task["task_id"])["events"]
    assert any(event["event_type"] == "supervisor_shutdown_deferred" for event in events)


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
    decisions = durable["recovery_decisions"]
    assert len(decisions) == 2
    assert [decision["selected_step"] for decision in decisions] == [
        "alternate_capability",
        "pause_or_block",
    ]
    assert [decision["attempt_index"] for decision in decisions] == [1, 2]
    assert decisions[-1]["outcome"] == "blocked"
    assert decisions[-1]["budget_before"]["retry_budget_remaining"] == 0
    assert decisions[-1]["budget_after"]["same_failure_retry_count"] == 2


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
    decision = durable["recovery_decisions"][0]
    assert decision["failure_type"] == "weak_evidence"
    assert decision["selected_step"] == "verify_state"
    assert decision["outcome"] == "paused"
    assert decision["attempt_index"] == 1
    assert decision["budget_before"]["retry_budget_remaining"] == 3
    assert decision["budget_after"]["same_failure_retry_count"] == 1
    assert decision["source_refs"][0].startswith("task:")


@pytest.mark.asyncio
async def test_control_supervisor_success_with_weak_evidence_enters_recovery(tmp_path):
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
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-weak-success",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Action completed, but destination-bound evidence is weak.",
                success=True,
                metadata={
                    "verification_status": "partial_pass",
                    "verification_report": {"failure_type": "weak_evidence"},
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
    durable = parent["artifacts"]["durable_execution"]
    verdict = durable["job_runs"][0]["verifier_verdict"]
    assert verdict["verdict"] == "uncertain"
    assert verdict["failure_type"] == "weak_evidence"
    decision = durable["recovery_decisions"][0]
    assert decision["failure_type"] == "weak_evidence"
    assert decision["selected_step"] == "verify_state"
    assert decision["outcome"] == "paused"
    assert durable["scheduler_state"]["waiting_for_approval_queue"]


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
    decision = durable["recovery_decisions"][0]
    assert decision["selected_step"] == "request_approval"
    assert decision["outcome"] == "paused"
    assert any(ref.startswith("runtime_error:") for ref in decision["source_refs"])


@pytest.mark.asyncio
async def test_control_supervisor_tool_failure_persists_recovery_decision_and_recovers(tmp_path):
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
        )
        if call_index == 1:
            return (
                ExecutionResult(
                    request_id="req-tool-timeout",
                    session_id=kwargs["session_id"],
                    user_id=kwargs["user_id"],
                    final_text="Tool call timed out.",
                    success=False,
                    metadata={
                        "normalized_failure_type": "tool_timeout",
                        "error": "tool timeout",
                    },
                ),
                child_task["task_id"],
            )
        return (
            ExecutionResult(
                request_id="req-recovered",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Recovered after retry.",
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
        objective="Keep the tool-backed mission healthy",
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
    decision = durable["recovery_decisions"][0]
    assert decision["failure_type"] == "tool_timeout"
    assert decision["selected_step"] == "retry_smaller_step"
    assert decision["outcome"] == "recovery_scheduled"
    assert decision["next_scheduler_queue"] == "retry_later"
    assert any(ref.startswith("runtime_error:") for ref in decision["source_refs"])
    assert durable["mission_contract"]["recovery_policy"]["ladder"]


@pytest.mark.asyncio
async def test_control_supervisor_mission_recovery_policy_can_escalate_to_approval(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-recovery-policy-approval",
        objective="Keep the tool-backed mission healthy",
        recovery_policy={
            "max_retries_per_step": 0,
            "ladder": ["retry_smaller_step", "request_approval", "pause_or_block"],
        },
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
                request_id="req-tool-timeout",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Tool call timed out.",
                success=False,
                metadata={
                    "normalized_failure_type": "tool_timeout",
                    "error": "tool timeout",
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
        objective="legacy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=2,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "pending"
    durable = parent["artifacts"]["durable_execution"]
    decision = durable["recovery_decisions"][0]
    assert decision["selected_step"] == "request_approval"
    assert decision["chosen_action"] == "request_human_approval"
    assert decision["outcome"] == "paused"
    assert decision["reason"].startswith(
        "mission_recovery_step_retry_limit_exhausted:"
    )
    assert durable["scheduler_state"]["waiting_for_approval_queue"][0][
        "metadata"
    ]["selected_step"] == "request_approval"


@pytest.mark.asyncio
async def test_control_supervisor_approval_step_respects_pending_approval_budget(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = {
        "contract_id": "legacy-mission-approval-budget",
        "objective": "Keep the tool-backed mission healthy",
        "recovery_policy": {
            "max_retries_per_step": 0,
            "ladder": ["retry_smaller_step", "request_approval", "pause_or_block"],
        },
    }

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
                request_id="req-tool-timeout",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Tool call timed out.",
                success=False,
                metadata={
                    "normalized_failure_type": "tool_timeout",
                    "error": "tool timeout",
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
        budget_policy=GuardrailBudgetPolicy(max_pending_approvals=0),
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-owner",
        objective="legacy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=2,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "blocked"
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["waiting_for_approval_queue"] == []
    assert durable["scheduler_state"]["blocked_queue"][0]["reason"] == "guardrail_budget_exhausted"
    assert durable["checkpoints"][-1]["budget"]["pending_approvals_count"] == 0
    decision = durable["recovery_decisions"][0]
    assert decision["selected_step"] == "pause_or_block"
    assert decision["outcome"] == "blocked"
    assert decision["budget_exhausted_reasons"] == ["max_pending_approvals_exhausted"]
    assert decision["budget_after"]["pending_approvals_count"] == 0


@pytest.mark.asyncio
async def test_control_supervisor_resume_uses_due_scheduler_queue(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    now = {"value": 1000.0}
    mission_contract = build_mission_contract(
        contract_id="mission-resume-test",
        objective="Keep the browser session healthy",
        allowed_actions=["current_tab.info"],
        completion_criteria=["current tab remains inspectable"],
        evidence_requirements=["current_tab_info result"],
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
        child_task = store.create(
            kind="control_loop",
            title="resumed iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
            artifacts={"result": {"success": True, "final_text": "ok after resume"}},
        )
        return (
            ExecutionResult(
                request_id="req-resumed",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok after resume",
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
        now_fn=lambda: now["value"],
    )
    runtime_state = supervisor._initial_runtime_state(
        objective=mission_contract.objective,
        loop_goal=build_maintenance_goal(mission_contract.objective, mission_contract),
        control_session_id="ctrlsup_resume_test",
        created_at=now["value"],
        next_run_at=now["value"],
        mission_contract=mission_contract,
    )
    first_report = supervisor._record_runtime_iteration(
        runtime_state,
        objective=mission_contract.objective,
        iteration=1,
        max_iterations=2,
        result=ExecutionResult(
            request_id="req-1",
            session_id="ctrlsup_resume_test",
            user_id="alice",
            final_text="ok before restart",
            success=True,
        ),
        child_task_id="task_child_before_restart",
        next_run_at=now["value"] + 60,
        has_more_iterations=True,
    )
    task = store.create(
        kind="control_supervisor",
        title=mission_contract.objective,
        status="running",
        owner_session_id="sess-owner",
        owner_user_id="alice",
        artifacts={
            "mission_contract": mission_contract.model_dump(mode="json"),
            "supervisor": {
                "objective": mission_contract.objective,
                "loop_goal": build_maintenance_goal(mission_contract.objective, mission_contract),
                "constraints": [],
                "mission_contract_id": mission_contract.contract_id,
                "duration_seconds": 120,
                "interval_seconds": 60,
                "control_session_id": "ctrlsup_resume_test",
                "started_at": now["value"],
                "ends_at": now["value"] + 120,
                "max_iterations": 2,
            },
            "progress": {
                "iteration": 1,
                "completed_iterations": 1,
                "next_run_at": now["value"] + 60,
                "child_task_ids": ["task_child_before_restart"],
                "last_child_task_id": "task_child_before_restart",
            },
            "durable_execution": first_report["durable_execution"],
        },
        metadata={
            "type": "control_supervisor",
            "control_session_id": "ctrlsup_resume_test",
            "mission_contract_id": mission_contract.contract_id,
        },
    )
    now["value"] += 60

    assert await supervisor.resume_task(task) is True
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(task["task_id"])
    assert parent is not None
    assert parent["status"] == "completed"
    assert parent["artifacts"]["progress"]["completed_iterations"] == 2
    assert parent["artifacts"]["progress"]["child_task_ids"][0] == "task_child_before_restart"
    durable = parent["artifacts"]["durable_execution"]
    assert len(durable["job_runs"]) == 2
    assert durable["job_runs"][1]["scheduler_queue"] == "completed"
    events = store.query_timeline(task["task_id"])["events"]
    assert any(event["event_type"] == "supervisor_resumed" for event in events)
    assert any(event["event_type"] == "scheduler_worker_tick" for event in events)


@pytest.mark.asyncio
async def test_control_supervisor_resume_skips_duplicate_active_handle(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-resume-duplicate",
        objective="Keep the browser session healthy",
        allowed_actions=["current_tab.info"],
    )
    release_iteration = asyncio.Event()
    child_calls: list[str] = []

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_calls.append(kwargs["parent_task_id"])
        await release_iteration.wait()
        child_task = store.create(
            kind="control_loop",
            title="resumed iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-resumed",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok after resume",
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
    task = _create_running_supervisor_task(
        store,
        mission_contract,
        control_session_id="ctrlsup_resume_duplicate",
    )

    assert await supervisor.resume_task(task) is True
    assert await supervisor.resume_task(task) is False
    assert len(supervisor._handles) == 1
    release_iteration.set()
    await asyncio.gather(*(handle.task for handle in list(supervisor._handles.values())))

    assert child_calls == [task["task_id"]]
    events = store.query_timeline(task["task_id"])["events"]
    assert any(
        event["event_type"] == "supervisor_resume_duplicate_skipped"
        and event["payload"]["reason"] == "active_handle_exists"
        for event in events
    )


@pytest.mark.asyncio
async def test_control_supervisor_resume_clears_stale_handle(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-resume-stale",
        objective="Keep the browser session healthy",
        allowed_actions=["current_tab.info"],
    )
    child_calls: list[str] = []

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_calls.append(kwargs["parent_task_id"])
        child_task = store.create(
            kind="control_loop",
            title="resumed iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-resumed",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok after resume",
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
    task = _create_running_supervisor_task(
        store,
        mission_contract,
        control_session_id="ctrlsup_resume_stale",
    )

    async def _finished_task():
        return None

    stale_task = asyncio.create_task(_finished_task())
    await stale_task
    supervisor._handles[task["task_id"]] = _SupervisorHandle(
        task_id=task["task_id"],
        owner_session_id="sess-owner",
        user_id="alice",
        stop_requested=asyncio.Event(),
        task=stale_task,
    )

    assert await supervisor.resume_task(task) is True
    await asyncio.gather(*(handle.task for handle in list(supervisor._handles.values())))

    assert child_calls == [task["task_id"]]
    events = store.query_timeline(task["task_id"])["events"]
    assert any(
        event["event_type"] == "supervisor_resume_stale_handle"
        and event["payload"]["reason"] == "stale_handle_done"
        for event in events
    )
    assert any(event["event_type"] == "supervisor_resumed" for event in events)


@pytest.mark.asyncio
async def test_control_supervisor_resume_skips_explicitly_stopped_task(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-resume-stopped",
        objective="Keep the browser session healthy",
        allowed_actions=["current_tab.info"],
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
        raise AssertionError("explicitly stopped supervisor should not resume")

    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        create_task_record_fn=_create_task_record,
        update_task_record_fn=_update_task_record,
        append_task_event_record_fn=_append_task_event_record,
    )
    task = _create_running_supervisor_task(
        store,
        mission_contract,
        control_session_id="ctrlsup_resume_stopped",
        stop_requested=True,
    )

    assert await supervisor.resume_task(task) is False
    assert supervisor._handles == {}
    events = store.query_timeline(task["task_id"])["events"]
    assert any(
        event["event_type"] == "supervisor_resume_skipped"
        and event["payload"]["reason"] == "explicit_stop_requested"
        for event in events
    )


@pytest.mark.asyncio
async def test_control_supervisor_aborts_when_contract_forbids_human_approval(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-abort-test",
        objective="Keep the current tab healthy",
        allowed_actions=["current_tab.info"],
        abort_conditions=["human approval required"],
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
        child_task = store.create(
            kind="control_loop",
            title="needs approval",
            status="pending",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-approval",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="Human approval is required.",
                success=False,
                metadata={
                    "needs_human": True,
                    "approval_request": {"request_id": "approval-test"},
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
        objective="legacy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "failed"
    assert parent["error"] == "mission_aborted:human_approval_required"
    assert parent["artifacts"]["result"]["mission_aborted"] is True
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["blocked_queue"][0]["reason"] == (
        "mission_aborted:human_approval_required"
    )
    assert durable["scheduler_state"]["waiting_for_approval_queue"] == []


@pytest.mark.asyncio
async def test_control_supervisor_aborts_when_typed_budget_condition_matches(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_calls: list[int] = []
    mission_contract = build_mission_contract(
        contract_id="mission-budget-abort-test",
        objective="Keep the sheet healthy",
        abort_conditions=[
            {"type": MissionAbortConditionType.GUARDRAIL_BUDGET_EXHAUSTED.value}
        ],
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
        objective="legacy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=3,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "failed"
    assert parent["error"] == "mission_aborted:guardrail_budget_exhausted"
    assert child_calls == [1, 2]
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["blocked_queue"][0]["reason"] == (
        "mission_aborted:guardrail_budget_exhausted"
    )


@pytest.mark.asyncio
async def test_control_supervisor_aborts_when_typed_current_tab_condition_matches(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    mission_contract = build_mission_contract(
        contract_id="mission-current-tab-abort-test",
        objective="Keep the current tab healthy",
        abort_conditions=[
            {"type": MissionAbortConditionType.CURRENT_TAB_CONNECTION_UNAVAILABLE.value}
        ],
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
        child_task = store.create(
            kind="control_loop",
            title="current tab unavailable",
            status="failed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-current-tab",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="All connection attempts failed.",
                success=False,
                metadata={"error": "Current Tab extension disconnected"},
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
        objective="legacy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "failed"
    assert parent["error"] == "mission_aborted:current_tab_connection_unavailable"
    durable = parent["artifacts"]["durable_execution"]
    assert durable["scheduler_state"]["blocked_queue"][0]["reason"] == (
        "mission_aborted:current_tab_connection_unavailable"
    )


@pytest.mark.asyncio
async def test_control_supervisor_links_current_tab_evidence_refs(tmp_path):
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
            title="inspect current tab",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-evidence",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok",
                success=True,
                verification_report_id="verify-1",
                metadata={
                    "verification_inputs": {
                        "current_tab": {
                            "info_succeeded": True,
                            "url": "http://127.0.0.1:18789/chat",
                            "title": "boiled-claw Control UI",
                            "tab_id": 10,
                            "window_id": 1,
                        }
                    }
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
        objective="Keep the current tab healthy",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    durable = parent["artifacts"]["durable_execution"]
    job = durable["job_runs"][0]
    child_task_id = parent["artifacts"]["progress"]["last_child_task_id"]
    assert f"{child_task_id}#verification_inputs.current_tab" in job["verifier_verdict"]["evidence_refs"]
    assert "verification_report:verify-1" in job["verifier_verdict"]["evidence_refs"]
    artifact_kinds = {item["kind"] for item in durable["task_graph"]["nodes"][0]["artifacts"]}
    assert "current_tab_info" in artifact_kinds
    assert "verification_report" in artifact_kinds


def _scheduler_entry(
    *,
    entry_id: str,
    node_id: str,
    queue: SchedulerQueueKind,
    available_at: float | None = None,
    expires_at: float | None = None,
) -> SchedulerQueueEntry:
    metadata = {}
    if expires_at is not None:
        metadata["expires_at"] = datetime.fromtimestamp(
            expires_at,
            tz=timezone.utc,
        ).isoformat()
    return SchedulerQueueEntry(
        entry_id=entry_id,
        node_id=node_id,
        queue=queue,
        available_at=(
            datetime.fromtimestamp(available_at, tz=timezone.utc)
            if available_at is not None
            else None
        ),
        metadata=metadata,
    )


def test_scheduler_selection_prioritizes_due_entries_and_skips_stale():
    async def _run_control_loop_with_task(**kwargs):
        raise AssertionError("selection test should not execute the child loop")

    async def _emit_session_event(session_id: str, *, source: str, status: str, message: str, **kwargs):
        return None

    now = {"value": 1000.0}
    mission_contract = build_mission_contract(objective="Keep scheduler deterministic")
    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        now_fn=lambda: now["value"],
    )
    runtime_state = supervisor._initial_runtime_state(
        objective=mission_contract.objective,
        loop_goal=build_maintenance_goal(mission_contract.objective, mission_contract),
        control_session_id="ctrlsup_scheduler_priority",
        created_at=now["value"],
        next_run_at=now["value"],
        mission_contract=mission_contract,
    )
    node_id = runtime_state["node_id"]
    runtime_state["queue_state"] = SchedulerQueueState(
        ready_queue=[
            _scheduler_entry(
                entry_id="a-stale-ready",
                node_id="missing-node",
                queue=SchedulerQueueKind.READY,
            ),
            _scheduler_entry(
                entry_id="b-ready",
                node_id=node_id,
                queue=SchedulerQueueKind.READY,
            ),
        ],
        retry_later_queue=[
            _scheduler_entry(
                entry_id="c-due-retry",
                node_id=node_id,
                queue=SchedulerQueueKind.RETRY_LATER,
                available_at=900.0,
            )
        ],
        periodic_check_queue=[
            _scheduler_entry(
                entry_id="d-due-periodic",
                node_id=node_id,
                queue=SchedulerQueueKind.PERIODIC_CHECK,
                available_at=800.0,
            )
        ],
    )

    selection = supervisor._select_scheduler_entry(runtime_state, now=now["value"])

    assert selection.entry is not None
    assert selection.entry.entry_id == "b-ready"
    assert selection.skipped_entries[0]["reason"] == "stale_unknown_node"
    assert runtime_state["queue_state"].ready_queue[0].entry_id == "b-ready"

    runtime_state["queue_state"] = SchedulerQueueState(
        retry_later_queue=[
            _scheduler_entry(
                entry_id="retry",
                node_id=node_id,
                queue=SchedulerQueueKind.RETRY_LATER,
                available_at=900.0,
            )
        ],
        periodic_check_queue=[
            _scheduler_entry(
                entry_id="periodic",
                node_id=node_id,
                queue=SchedulerQueueKind.PERIODIC_CHECK,
                available_at=800.0,
            )
        ],
    )
    assert supervisor._select_scheduler_entry(
        runtime_state,
        now=now["value"],
    ).entry.entry_id == "retry"

    runtime_state["queue_state"] = SchedulerQueueState(
        retry_later_queue=[
            _scheduler_entry(
                entry_id="future-retry",
                node_id=node_id,
                queue=SchedulerQueueKind.RETRY_LATER,
                available_at=1300.0,
            )
        ],
        periodic_check_queue=[
            _scheduler_entry(
                entry_id="future-periodic",
                node_id=node_id,
                queue=SchedulerQueueKind.PERIODIC_CHECK,
                available_at=1200.0,
            )
        ],
    )
    assert supervisor._select_scheduler_entry(
        runtime_state,
        now=now["value"],
    ).entry.entry_id == "future-periodic"


@pytest.mark.asyncio
async def test_control_supervisor_records_heartbeat_and_watchdog_warnings(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    now = {"value": 1000.0}

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
            title="heartbeat iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id="req-heartbeat",
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
        now_fn=lambda: now["value"],
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-owner",
        objective="Keep heartbeat visible",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    progress = parent["artifacts"]["progress"]
    assert progress["heartbeat"]["last_heartbeat_at"] == now["value"]
    assert parent["artifacts"]["durable_execution"]["supervisor_health"][
        "active_node_id"
    ].endswith("/maintain-objective")

    stale_task = store.create(
        kind="control_supervisor",
        title="stale supervisor",
        status="running",
        owner_session_id="sess-owner",
        owner_user_id="alice",
        artifacts={
            "progress": {
                "heartbeat": {
                    "last_heartbeat_at": 500.0,
                    "status": "running",
                },
                "stop_requested": False,
            }
        },
        metadata={"type": "control_supervisor"},
    )
    finding = supervisor.watchdog_task(stale_task, stale_after_seconds=100.0)
    assert finding is not None
    assert finding.reason == "stale_heartbeat"
    assert finding.action == "resume"
    updated = store.get(stale_task["task_id"])
    assert updated["artifacts"]["progress"]["watchdog"]["reason"] == "stale_heartbeat"
    assert any(
        event["event_type"] == "supervisor_watchdog_warning"
        for event in store.query_timeline(stale_task["task_id"])["events"]
    )

    stopped_task = store.create(
        kind="control_supervisor",
        title="stopped supervisor",
        status="running",
        owner_session_id="sess-owner",
        owner_user_id="alice",
        artifacts={
            "progress": {
                "heartbeat": {"last_heartbeat_at": 100.0},
                "stop_requested": True,
            }
        },
        metadata={"type": "control_supervisor"},
    )
    assert supervisor.watchdog_task(stopped_task, stale_after_seconds=100.0) is None
    stopped = store.get(stopped_task["task_id"])
    assert stopped["metadata"].get("watchdog_action") is None


@pytest.mark.asyncio
async def test_control_supervisor_runs_multi_node_graph_in_dependency_order(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_goals: list[str] = []

    mission_contract = build_mission_contract(
        contract_id="mission-multi-node",
        objective="Run a staged inspection",
        task_nodes=[
            {
                "node_id": "inspect",
                "title": "Inspect target",
                "description": "Inspect the visible state",
                "completion_criteria": ["inspection captured"],
            },
            {
                "node_id": "repair",
                "title": "Repair target",
                "description": "Repair any drift",
                "depends_on": ["inspect"],
            },
            {
                "node_id": "verify",
                "title": "Verify target",
                "description": "Verify the repaired state",
                "depends_on": ["repair"],
            },
        ],
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
            title=f"node {len(child_goals)}",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id=f"req-node-{len(child_goals)}",
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
        objective="legacy objective",
        constraints=[],
        duration_seconds=60,
        interval_seconds=60,
        source="test",
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "completed"
    assert parent["artifacts"]["progress"]["completed_iterations"] == 3
    assert [goal.splitlines()[0] for goal in child_goals] == [
        "Inspect the visible state",
        "Repair any drift",
        "Verify the repaired state",
    ]
    durable = parent["artifacts"]["durable_execution"]
    assert [run["node_id"].split("/")[-1] for run in durable["job_runs"]] == [
        "inspect",
        "repair",
        "verify",
    ]
    assert {node["status"] for node in durable["task_graph"]["nodes"]} == {
        DurableTaskNodeStatus.DONE.value
    }
    assert durable["resume_state"]["reason"] == "graph_complete"
    assert durable["mission_scorecard"]["objective_progress"] == "satisfied"
    assert durable["mission_scorecard"]["verification_pass_rate"] == 1.0
    assert parent["artifacts"]["mission_scorecard"]["objective_progress"] == "satisfied"
    assert parent["artifacts"]["mission_review"]["final_status"] == "completed"
    assert parent["artifacts"]["mission_review"]["scorecard_snapshot"] == parent[
        "artifacts"
    ]["mission_scorecard"]
    timeline = store.query_timeline(started.task["task_id"], page_size=100)
    assert "post_mission_review_recorded" in {
        event["event_type"] for event in timeline["events"]
    }


@pytest.mark.asyncio
async def test_control_supervisor_resumes_multi_node_graph_at_next_actionable_node(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    now = {"value": 1000.0}
    child_goals: list[str] = []
    mission_contract = build_mission_contract(
        contract_id="mission-multi-resume",
        objective="Run a resumable staged inspection",
        task_nodes=[
            {"node_id": "inspect", "description": "Inspect state"},
            {"node_id": "repair", "description": "Repair state", "depends_on": ["inspect"]},
            {"node_id": "verify", "description": "Verify state", "depends_on": ["repair"]},
        ],
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
            title=f"resumed node {len(child_goals)}",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        return (
            ExecutionResult(
                request_id=f"req-resume-node-{len(child_goals)}",
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
        now_fn=lambda: now["value"],
    )
    runtime_state = supervisor._initial_runtime_state(
        objective=mission_contract.objective,
        loop_goal=build_maintenance_goal(mission_contract.objective, mission_contract),
        control_session_id="ctrlsup_multi_resume",
        created_at=now["value"],
        next_run_at=now["value"],
        mission_contract=mission_contract,
    )
    first_report = supervisor._record_runtime_iteration(
        runtime_state,
        objective=mission_contract.objective,
        iteration=1,
        max_iterations=3,
        result=ExecutionResult(
            request_id="req-before-restart",
            session_id="ctrlsup_multi_resume",
            user_id="alice",
            final_text="ok before restart",
            success=True,
        ),
        child_task_id="task_child_before_restart",
        next_run_at=None,
        has_more_iterations=True,
    )
    assert first_report["checkpoint"]["next_actionable_task_node_id"].endswith("/repair")
    task = store.create(
        kind="control_supervisor",
        title=mission_contract.objective,
        status="running",
        owner_session_id="sess-owner",
        owner_user_id="alice",
        artifacts={
            "mission_contract": mission_contract.model_dump(mode="json"),
            "supervisor": {
                "objective": mission_contract.objective,
                "loop_goal": build_maintenance_goal(mission_contract.objective, mission_contract),
                "constraints": [],
                "mission_contract_id": mission_contract.contract_id,
                "duration_seconds": 120,
                "interval_seconds": 0,
                "control_session_id": "ctrlsup_multi_resume",
                "started_at": now["value"],
                "ends_at": now["value"] + 120,
                "max_iterations": 3,
            },
            "progress": {
                "iteration": 1,
                "completed_iterations": 1,
                "next_run_at": now["value"],
                "child_task_ids": ["task_child_before_restart"],
                "last_child_task_id": "task_child_before_restart",
            },
            "durable_execution": first_report["durable_execution"],
        },
        metadata={
            "type": "control_supervisor",
            "control_session_id": "ctrlsup_multi_resume",
            "mission_contract_id": mission_contract.contract_id,
        },
    )

    assert await supervisor.resume_task(task) is True
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(task["task_id"])
    durable = parent["artifacts"]["durable_execution"]
    assert parent["status"] == "completed"
    assert parent["artifacts"]["progress"]["child_task_ids"][0] == "task_child_before_restart"
    assert [goal.splitlines()[0] for goal in child_goals] == [
        "Repair state",
        "Verify state",
    ]
    assert [run["node_id"].split("/")[-1] for run in durable["job_runs"]] == [
        "inspect",
        "repair",
        "verify",
    ]


@pytest.mark.asyncio
async def test_control_supervisor_blocked_node_does_not_stop_independent_ready_node(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    child_nodes: list[str] = []
    mission_contract = build_mission_contract(
        contract_id="mission-multi-independent",
        objective="Run independent branches",
        task_nodes=[
            {"node_id": "blocked_branch", "description": "Run branch that blocks"},
            {"node_id": "independent_branch", "description": "Run independent branch"},
            {
                "node_id": "dependent_branch",
                "description": "Run dependent branch",
                "depends_on": ["blocked_branch"],
            },
        ],
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
        node_name = kwargs["goal"].splitlines()[0]
        child_nodes.append(node_name)
        child_task = store.create(
            kind="control_loop",
            title=node_name,
            status="failed" if "blocks" in node_name else "completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
        )
        if "blocks" in node_name:
            return (
                ExecutionResult(
                    request_id="req-blocked-branch",
                    session_id=kwargs["session_id"],
                    user_id=kwargs["user_id"],
                    final_text="manual triage required",
                    success=False,
                    metadata={"normalized_failure_type": "unknown"},
                ),
                child_task["task_id"],
            )
        return (
            ExecutionResult(
                request_id="req-independent-branch",
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
        objective="legacy objective",
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        mission_contract=mission_contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent["status"] == "blocked"
    assert child_nodes == ["Run branch that blocks", "Run independent branch"]
    durable = parent["artifacts"]["durable_execution"]
    statuses = {
        node["node_id"].split("/")[-1]: node["status"]
        for node in durable["task_graph"]["nodes"]
    }
    assert statuses == {
        "blocked_branch": "blocked",
        "independent_branch": "done",
        "dependent_branch": "blocked",
    }
    assert durable["mission_scorecard"]["objective_progress"] == "blocked"
    assert parent["artifacts"]["mission_review"]["final_status"] == "blocked"
    assert parent["artifacts"]["mission_review"]["improvement_candidates"][0][
        "candidate_type"
    ] == "diagnostic_task"
    assert parent["artifacts"]["mission_memory_links"]["memory_promotion_candidates"][0][
        "type"
    ] == "failure_pattern"
    timeline = store.query_timeline(started.task["task_id"], page_size=100)
    events = timeline["events"]
    assert "post_mission_review_recorded" in {event["event_type"] for event in events}
    iteration_events = [
        event
        for event in events
        if event["event_type"] == "supervisor_iteration_completed"
    ]
    assert any(
        event["payload"]["runtime"]["recovery_decision"]["recovery_ladder_step"]
        == "diagnostic_task"
        for event in iteration_events
    )
