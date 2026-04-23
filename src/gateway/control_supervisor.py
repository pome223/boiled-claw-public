"""Opt-in long-running supervisor for repeated control-loop maintenance runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.control_loop.live_failure_taxonomy import classify_control_loop_failure
from src.control_loop.root_workflow import ExecutionResult
from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableArtifactRef,
    DurableCheckpoint,
    DurableJobRun,
    DurableResumeState,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryDecision,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)
from src.runtime.orchestration_policy import (
    append_scheduler_queue_entry,
    budget_exhaustion_reasons,
    build_escalation_record,
    default_guardrail_budget_policy,
    default_recovery_policies,
    job_run_status_from_verdict,
    recovery_policy_for_failure_type,
    repair_depth_increment,
    scheduler_queue_reason,
)
from src.tools.tasks import (
    append_task_event_record,
    create_task_record,
    update_task_record,
)

_SUPERVISOR_AGENT_NAME = "control_supervisor"


def build_maintenance_goal(objective: str) -> str:
    normalized = str(objective or "").strip()
    if not normalized:
        raise ValueError("objective is required")
    return (
        "Maintain the following long-running objective for the active session.\n"
        f"Objective: {normalized}\n\n"
        "Inspect the current state, keep the objective satisfied, and perform only the "
        "next minimal action required. If the objective already looks healthy, prefer "
        "verification over disruptive changes."
    )


RunControlLoopWithTaskFn = Callable[..., Awaitable[tuple[ExecutionResult, str]]]
EmitSessionEventFn = Callable[..., Awaitable[None]]
TaskCreateFn = Callable[..., dict[str, Any]]
TaskUpdateFn = Callable[..., dict[str, Any] | None]
TaskAppendEventFn = Callable[..., dict[str, Any] | None]


def _utc_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _supervisor_node_id(control_session_id: str) -> str:
    return f"{control_session_id}/maintain-objective"


def _queue_entry_for_node(
    *,
    node_id: str,
    queue: SchedulerQueueKind,
    checkpoint_id: str | None,
    available_at: datetime | None,
    failure_type: str | None,
    chosen_action: RecoveryActionType | None,
    escalation_id: str | None = None,
) -> SchedulerQueueEntry:
    return SchedulerQueueEntry(
        entry_id=f"{node_id}/queue",
        node_id=node_id,
        queue=queue,
        checkpoint_id=checkpoint_id,
        available_at=available_at,
        escalation_id=escalation_id,
        metadata={
            "failure_type": str(failure_type or ""),
            "chosen_action": chosen_action.value if chosen_action is not None else "",
        },
    )


def _available_at_for_queue(
    queue: SchedulerQueueKind,
    *,
    next_run_at: float | None,
) -> datetime | None:
    if next_run_at is None or queue not in {
        SchedulerQueueKind.RETRY_LATER,
        SchedulerQueueKind.PERIODIC_CHECK,
    }:
        return None
    return _utc_datetime(next_run_at)


def _task_node_status_for_queue(
    queue: SchedulerQueueKind,
) -> DurableTaskNodeStatus:
    if queue == SchedulerQueueKind.COMPLETED:
        return DurableTaskNodeStatus.DONE
    if queue in {
        SchedulerQueueKind.WAITING_FOR_APPROVAL,
        SchedulerQueueKind.BLOCKED,
    }:
        return DurableTaskNodeStatus.BLOCKED
    if queue == SchedulerQueueKind.RETRY_LATER:
        return DurableTaskNodeStatus.FAILED
    return DurableTaskNodeStatus.READY


def _verdict_for_result(
    *,
    result: ExecutionResult,
    failure_type: str | None,
) -> DurableVerifierVerdictValue:
    if result.success:
        return DurableVerifierVerdictValue.PASS
    if str(failure_type or "") == "weak_evidence":
        return DurableVerifierVerdictValue.UNCERTAIN
    return DurableVerifierVerdictValue.FAIL


def _artifact_refs_for_result(
    *,
    child_task_id: str,
    result: ExecutionResult,
    failure_type: str | None,
) -> list[DurableArtifactRef]:
    refs = [
        DurableArtifactRef(
            kind="task",
            ref=child_task_id,
            label=f"child-task:{child_task_id}",
            metadata={"task_id": child_task_id},
        )
    ]
    if result.verification_report_id:
        refs.append(
            DurableArtifactRef(
                kind="verification_report",
                ref=result.verification_report_id,
                label="verification_report",
                metadata={"failure_type": failure_type},
            )
        )
    approval_request = result.metadata.get("approval_request")
    if isinstance(approval_request, dict) and str(approval_request.get("request_id") or "").strip():
        refs.append(
            DurableArtifactRef(
                kind="approval_request",
                ref=str(approval_request["request_id"]),
                label="approval_request",
                metadata={"plan_id": approval_request.get("plan_id")},
            )
        )
    return refs


def _build_live_verifier_verdict(
    *,
    result: ExecutionResult,
    failure_type: str | None,
    child_task_id: str,
    created_at: float,
) -> DurableVerifierVerdict:
    verdict = _verdict_for_result(result=result, failure_type=failure_type)
    confidence = 0.95 if verdict == DurableVerifierVerdictValue.PASS else (
        0.45 if verdict == DurableVerifierVerdictValue.UNCERTAIN else 0.8
    )
    report = result.metadata.get("verification_report")
    evidence_refs = []
    if isinstance(report, dict):
        refs = report.get("artifact_refs") or report.get("evidence_refs") or []
        if isinstance(refs, list):
            evidence_refs = [str(item).strip() for item in refs if str(item).strip()]
    replay_reference = {"child_task_id": child_task_id}
    if result.verification_report_id:
        replay_reference["verification_report_id"] = result.verification_report_id
    return DurableVerifierVerdict(
        verdict=verdict,
        evidence_refs=evidence_refs,
        failure_type=failure_type,
        confidence=confidence,
        confidence_source="synthetic_default",
        verifier_source="control_supervisor_phase0",
        recommended_repair_target=(str(result.final_text or "").strip() or None),
        replay_reference=replay_reference,
        created_at=_utc_datetime(created_at),
    )


@dataclass(frozen=True)
class SupervisorStartResult:
    task: dict[str, Any]
    control_session_id: str
    max_iterations: int
    ends_at: float
    next_run_at: float


@dataclass
class _SupervisorHandle:
    task_id: str
    owner_session_id: str
    user_id: str
    stop_requested: asyncio.Event
    task: asyncio.Task[None]


class ControlLoopSupervisor:
    def __init__(
        self,
        *,
        run_control_loop_with_task: RunControlLoopWithTaskFn,
        emit_session_event: EmitSessionEventFn,
        create_task_record_fn: TaskCreateFn = create_task_record,
        update_task_record_fn: TaskUpdateFn = update_task_record,
        append_task_event_record_fn: TaskAppendEventFn = append_task_event_record,
        budget_policy: GuardrailBudgetPolicy | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._run_control_loop_with_task = run_control_loop_with_task
        self._emit_session_event = emit_session_event
        self._create_task_record = create_task_record_fn
        self._update_task_record = update_task_record_fn
        self._append_task_event_record = append_task_event_record_fn
        self._budget_policy = budget_policy or default_guardrail_budget_policy()
        self._now = now_fn
        self._handles: dict[str, _SupervisorHandle] = {}

    async def start(
        self,
        *,
        user_id: str,
        owner_session_id: str,
        objective: str,
        constraints: list[str],
        duration_seconds: int,
        interval_seconds: int,
        source: str,
        maintenance_goal: Optional[str] = None,
        request_id: Optional[str] = None,
        max_iterations: Optional[int] = None,
    ) -> SupervisorStartResult:
        started_at = self._now()
        resolved_duration = max(1, int(duration_seconds))
        resolved_interval = max(0, int(interval_seconds))
        resolved_max_iterations = max_iterations or max(
            1,
            math.ceil(resolved_duration / max(resolved_interval, 1)),
        )
        control_session_id = f"ctrlsup_{uuid.uuid4().hex[:12]}"
        loop_goal = (
            str(maintenance_goal or "").strip()
            or build_maintenance_goal(objective)
        )
        ends_at = started_at + float(resolved_duration)
        task = self._create_task_record(
            kind="control_supervisor",
            title=objective,
            status="running",
            owner_session_id=owner_session_id,
            owner_user_id=user_id,
            artifacts={
                "supervisor": {
                    "objective": objective,
                    "loop_goal": loop_goal,
                    "constraints": list(constraints),
                    "duration_seconds": resolved_duration,
                    "interval_seconds": resolved_interval,
                    "control_session_id": control_session_id,
                    "started_at": started_at,
                    "ends_at": ends_at,
                    "max_iterations": resolved_max_iterations,
                },
                "progress": {
                    "iteration": 0,
                    "completed_iterations": 0,
                    "next_run_at": started_at,
                    "child_task_ids": [],
                    "last_child_task_id": None,
                    "last_result": None,
                    "stop_requested": False,
                },
                "durable_execution": self._initial_durable_execution_payload(
                    objective=objective,
                    loop_goal=loop_goal,
                    control_session_id=control_session_id,
                    created_at=started_at,
                    next_run_at=started_at,
                ),
            },
            metadata={
                "source": source,
                "request_id": request_id,
                "type": "control_supervisor",
                "control_session_id": control_session_id,
            },
        )
        task_id = str(task["task_id"])
        stop_requested = asyncio.Event()
        runner_task = asyncio.create_task(
            self._run_supervisor(
                task_id=task_id,
                owner_session_id=owner_session_id,
                user_id=user_id,
                objective=objective,
                loop_goal=loop_goal,
                constraints=list(constraints),
                control_session_id=control_session_id,
                interval_seconds=resolved_interval,
                max_iterations=resolved_max_iterations,
                ends_at=ends_at,
                stop_requested=stop_requested,
            ),
            name=f"control-supervisor:{task_id}",
        )
        self._handles[task_id] = _SupervisorHandle(
            task_id=task_id,
            owner_session_id=owner_session_id,
            user_id=user_id,
            stop_requested=stop_requested,
            task=runner_task,
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="accepted",
            message=(
                "Started long-running control supervisor "
                f"for {resolved_duration}s (interval {resolved_interval}s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return SupervisorStartResult(
            task=task,
            control_session_id=control_session_id,
            max_iterations=resolved_max_iterations,
            ends_at=ends_at,
            next_run_at=started_at,
        )

    async def request_stop(self, task_id: str) -> dict[str, Any] | None:
        handle = self._handles.get(task_id)
        if handle is None:
            return None
        handle.stop_requested.set()
        updated = self._update_task_record(
            task_id,
            artifacts={
                "progress": {
                    "stop_requested": True,
                    "stop_requested_at": self._now(),
                }
            },
            metadata={"stop_requested": True},
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_stop_requested",
            status="running",
            title="Stop requested",
            payload={
                "summary": "Graceful stop requested; the supervisor will stop after the current iteration.",
            },
        )
        await self._emit_session_event(
            handle.owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="stop_requested",
            message=(
                "Graceful stop requested; the supervisor will stop after the current iteration."
            ),
            user_id=handle.user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return updated

    async def shutdown(self) -> None:
        handles = list(self._handles.values())
        for handle in handles:
            handle.stop_requested.set()
        if handles:
            await asyncio.gather(
                *(handle.task for handle in handles),
                return_exceptions=True,
            )

    def _initial_runtime_state(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        created_at: float,
        next_run_at: float | None,
    ) -> dict[str, Any]:
        graph_id = f"control_supervisor:{control_session_id}"
        node_id = _supervisor_node_id(control_session_id)
        node = DurableTaskNode(
            node_id=node_id,
            title=objective,
            description=loop_goal,
            status=DurableTaskNodeStatus.READY,
            scheduler_queue=SchedulerQueueKind.READY,
        )
        queue_state = SchedulerQueueState()
        append_scheduler_queue_entry(
            queue_state,
            _queue_entry_for_node(
                node_id=node_id,
                queue=SchedulerQueueKind.READY,
                checkpoint_id=None,
                available_at=_available_at_for_queue(
                    SchedulerQueueKind.READY,
                    next_run_at=next_run_at,
                ),
                failure_type=None,
                chosen_action=None,
            ),
        )
        return {
            "created_at": created_at,
            "graph_id": graph_id,
            "node_id": node_id,
            "node": node,
            "queue_state": queue_state,
            "job_runs": [],
            "checkpoints": [],
            "escalations": [],
            "successful_artifacts": {},
            "retry_counters": Counter(),
            "llm_calls_used": 0,
            "tool_calls_used": 0,
            "repair_depth_used": 0,
            "pending_approvals_count": 0,
        }

    def _serialize_runtime_state(
        self,
        runtime_state: dict[str, Any],
        *,
        objective: str,
    ) -> dict[str, Any]:
        # `queue_state` is the current actionable snapshot for the live supervisor,
        # not a cumulative history of every queue the node visited. Historical
        # transitions remain in `job_runs`, `checkpoints`, and task timeline events.
        created_at = _utc_datetime(runtime_state["created_at"])
        node: DurableTaskNode = runtime_state["node"]
        task_graph = DurableTaskGraph(
            graph_id=runtime_state["graph_id"],
            goal=objective,
            nodes=[node],
            created_at=created_at,
            updated_at=_utc_datetime(self._now()),
            metadata={
                "runtime_mode": "live_supervisor_phase0",
                "scheduler_phase": "live_worker",
            },
        )
        checkpoints: list[DurableCheckpoint] = runtime_state["checkpoints"]
        if checkpoints:
            resume_state = checkpoints[-1].resume_state(task_graph).model_copy(
                update={
                    "scheduler_queue_counts": runtime_state["queue_state"].counts(),
                    "pending_approval_ids": list(checkpoints[-1].pending_approval_ids),
                }
            )
        else:
            resume_state = DurableResumeState(
                checkpoint_id=f"{runtime_state['graph_id']}/checkpoint-0",
                graph_id=runtime_state["graph_id"],
                next_actionable_task_node_id=node.node_id,
                open_task_node_ids=task_graph.open_task_node_ids(),
                blocked_task_node_ids=task_graph.blocked_task_node_ids(),
                pending_approval_ids=[],
                scheduler_queue_counts=runtime_state["queue_state"].counts(),
                reason="resume_from_open_task",
            )
        return {
            "task_graph": task_graph.model_dump(mode="json"),
            "job_runs": [
                item.model_dump(mode="json") for item in runtime_state["job_runs"]
            ],
            "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
            "verifier_verdicts": [
                node.verifier_verdict.model_dump(mode="json")
                for node in task_graph.nodes
                if node.verifier_verdict is not None
            ],
            "recovery_policies": {
                key: value.model_dump(mode="json")
                for key, value in default_recovery_policies().items()
            },
            "scheduler_state": runtime_state["queue_state"].model_dump(mode="json"),
            "escalations": [
                item.model_dump(mode="json") for item in runtime_state["escalations"]
            ],
            "resume_state": resume_state.model_dump(mode="json"),
        }

    def _initial_durable_execution_payload(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        created_at: float,
        next_run_at: float | None,
    ) -> dict[str, Any]:
        runtime_state = self._initial_runtime_state(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=created_at,
            next_run_at=next_run_at,
        )
        return self._serialize_runtime_state(runtime_state, objective=objective)

    def _runtime_failure_classification(self, result: ExecutionResult) -> dict[str, Any]:
        existing = str(result.metadata.get("normalized_failure_type") or "").strip() or None
        classification = classify_control_loop_failure(
            success=result.success,
            needs_human=bool(result.metadata.get("needs_human")),
            final_text=result.final_text,
            verification_status=result.metadata.get("verification_status"),
            verification_report=(
                result.metadata.get("verification_report")
                if isinstance(result.metadata.get("verification_report"), dict)
                else None
            ),
            error=str(result.metadata.get("error") or "").strip() or None,
            existing_failure_type=existing,
        )
        result.metadata.update(classification)
        return classification

    def _record_runtime_iteration(
        self,
        runtime_state: dict[str, Any],
        *,
        objective: str,
        iteration: int,
        max_iterations: int,
        result: ExecutionResult,
        child_task_id: str,
        next_run_at: float | None,
        has_more_iterations: bool,
    ) -> dict[str, Any]:
        classification = self._runtime_failure_classification(result)
        failure_type = classification["normalized_failure_type"]
        verifier_verdict = _build_live_verifier_verdict(
            result=result,
            failure_type=failure_type,
            child_task_id=child_task_id,
            created_at=self._now(),
        )
        policy = None
        chosen_action = None
        if result.success:
            next_queue = (
                SchedulerQueueKind.PERIODIC_CHECK
                if has_more_iterations
                else SchedulerQueueKind.COMPLETED
            )
        else:
            policy = recovery_policy_for_failure_type(failure_type)
            chosen_action = policy.allowed_actions[0] if policy and policy.allowed_actions else None
            next_queue = policy.next_scheduler_queue if policy is not None else SchedulerQueueKind.BLOCKED
        retry_count = runtime_state["retry_counters"][failure_type] if failure_type else 0
        runtime_state["llm_calls_used"] += 1 + (
            policy.budget_impact.llm_calls if policy is not None else 0
        )
        runtime_state["tool_calls_used"] += max(1, len(verifier_verdict.evidence_refs)) + (
            policy.budget_impact.tool_calls if policy is not None else 0
        )
        runtime_state["repair_depth_used"] += repair_depth_increment(chosen_action)
        pending_approvals_after = runtime_state["pending_approvals_count"] + (
            1 if next_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL else 0
        )
        # Phase 0 uses a coarse hourly budget counter so live supervisor artifacts
        # stay shape-compatible with eval-derived substrate reports. If a future
        # worker needs shorter smoke budgets, promote this to a finer-grained clock.
        runtime_hours_used = max(
            1,
            math.ceil(max(0.0, self._now() - runtime_state["created_at"]) / 3600.0),
        )
        budget_reasons = budget_exhaustion_reasons(
            budget_policy=self._budget_policy,
            runtime_hours_used=runtime_hours_used,
            llm_calls_used=runtime_state["llm_calls_used"],
            tool_calls_used=runtime_state["tool_calls_used"],
            repair_depth_used=runtime_state["repair_depth_used"],
            pending_approvals_count=pending_approvals_after,
            next_scheduler_queue=next_queue,
            failure_type=failure_type,
            retry_count=retry_count,
        )
        budget_exhausted = bool(budget_reasons)
        if budget_exhausted:
            next_queue = SchedulerQueueKind.BLOCKED
        decision = RecoveryDecision(
            node_id=runtime_state["node_id"],
            failure_type=failure_type,
            chosen_action=chosen_action,
            policy=policy,
            next_scheduler_queue=next_queue,
            budget_exhausted=budget_exhausted,
            budget_exhausted_reasons=budget_reasons,
        )
        queue_reason = scheduler_queue_reason(
            decision,
            verifier_verdict=verifier_verdict,
        )
        available_at = _available_at_for_queue(
            next_queue,
            next_run_at=next_run_at,
        )
        checkpoint_id = f"{runtime_state['graph_id']}/checkpoint-{iteration}"
        escalation_record = None
        if next_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
            approval_request = result.metadata.get("approval_request")
            approval_request_id = None
            if isinstance(approval_request, dict):
                approval_request_id = str(approval_request.get("request_id") or "").strip() or None
            escalation_record = build_escalation_record(
                run_id=f"{runtime_state['graph_id']}/run-{iteration}",
                node_id=runtime_state["node_id"],
                checkpoint_id=checkpoint_id,
                created_at=_utc_datetime(self._now()),
                failure_type=failure_type,
                reason=queue_reason,
                approval_request_id=approval_request_id,
            )
            runtime_state["escalations"].append(escalation_record)
            runtime_state["pending_approvals_count"] = pending_approvals_after

        queue_entry = _queue_entry_for_node(
            node_id=runtime_state["node_id"],
            queue=next_queue,
            checkpoint_id=checkpoint_id,
            available_at=available_at,
            failure_type=failure_type,
            chosen_action=chosen_action,
            escalation_id=(
                escalation_record.escalation_id if escalation_record is not None else None
            ),
        )
        queue_entry.reason = queue_reason
        runtime_state["queue_state"] = SchedulerQueueState()
        append_scheduler_queue_entry(runtime_state["queue_state"], queue_entry)

        if failure_type and not result.success:
            runtime_state["retry_counters"][failure_type] += 1

        node: DurableTaskNode = runtime_state["node"]
        node.status = _task_node_status_for_queue(next_queue)
        node.retry_count = runtime_state["retry_counters"][failure_type] if failure_type else 0
        node.next_retry_at = available_at
        node.scheduler_queue = next_queue
        node.verifier_verdict = verifier_verdict
        node.checkpoint_refs.append(checkpoint_id)
        node.artifacts = _artifact_refs_for_result(
            child_task_id=child_task_id,
            result=result,
            failure_type=failure_type,
        )

        if result.success:
            runtime_state["successful_artifacts"][node.node_id] = list(node.artifacts)

        task_graph = DurableTaskGraph(
            graph_id=runtime_state["graph_id"],
            goal=objective,
            nodes=[node],
            created_at=_utc_datetime(runtime_state["created_at"]),
            updated_at=_utc_datetime(self._now()),
            metadata={
                "runtime_mode": "live_supervisor_phase0",
                "scheduler_phase": "live_worker",
            },
        )
        checkpoint = DurableCheckpoint(
            checkpoint_id=checkpoint_id,
            graph_id=runtime_state["graph_id"],
            run_id=f"{runtime_state['graph_id']}/run-{iteration}",
            current_goal=objective,
            current_task_node_id=node.node_id,
            open_task_node_ids=task_graph.open_task_node_ids(),
            blocked_task_node_ids=task_graph.blocked_task_node_ids(),
            pending_approval_ids=[
                escalation.approval_request_id
                for escalation in runtime_state["escalations"]
                if escalation.approval_request_id
                and escalation.status.name.lower() == "waiting_for_approval"
            ],
            last_successful_artifacts=dict(runtime_state["successful_artifacts"]),
            budget=CheckpointBudget(
                run_budget_remaining=max(0, max_iterations - iteration),
                retry_budget_remaining={
                    key: max(
                        0,
                        self._budget_policy.max_same_failure_retries - value,
                    )
                    for key, value in runtime_state["retry_counters"].items()
                },
                policy=self._budget_policy,
                runtime_hours_used=runtime_hours_used,
                llm_calls_used=runtime_state["llm_calls_used"],
                tool_calls_used=runtime_state["tool_calls_used"],
                same_failure_retries=dict(runtime_state["retry_counters"]),
                repair_depth_used=runtime_state["repair_depth_used"],
                pending_approvals_count=runtime_state["pending_approvals_count"],
                budget_exhausted=budget_exhausted,
                budget_exhausted_reasons=budget_reasons,
            ),
            retry_counters={
                runtime_state["node_id"]: node.retry_count,
            } if node.retry_count > 0 else {},
            trajectory_ids=[],
            replay_references=[{"child_task_id": child_task_id}],
            next_actionable_task_node_id=(
                runtime_state["node_id"]
                if next_queue == SchedulerQueueKind.READY
                else None
            ),
            created_at=_utc_datetime(self._now()),
        )
        job_run = DurableJobRun(
            run_id=f"{runtime_state['graph_id']}/run-{iteration}",
            graph_id=runtime_state["graph_id"],
            node_id=runtime_state["node_id"],
            goal=objective,
            status=job_run_status_from_verdict(verifier_verdict.verdict),
            attempt=iteration,
            trajectory_id=None,
            replay_reference={"child_task_id": child_task_id},
            checkpoint_id=checkpoint_id,
            scheduler_queue=next_queue,
            verifier_verdict=verifier_verdict,
            started_at=_utc_datetime(self._now()),
            ended_at=_utc_datetime(self._now()),
        )
        runtime_state["checkpoints"].append(checkpoint)
        runtime_state["job_runs"].append(job_run)
        durable_execution = self._serialize_runtime_state(
            runtime_state,
            objective=objective,
        )
        return {
            "durable_execution": durable_execution,
            "failure_type": failure_type,
            "recovery_policy": policy.model_dump(mode="json") if policy is not None else None,
            "recovery_decision": decision.model_dump(mode="json"),
            "scheduler_queue": next_queue.value,
            "scheduler_queue_entry": queue_entry.model_dump(mode="json"),
            "budget_state": checkpoint.budget.model_dump(mode="json"),
            "checkpoint": checkpoint.model_dump(mode="json"),
            "job_run": job_run.model_dump(mode="json"),
            "escalation_record": (
                escalation_record.model_dump(mode="json")
                if escalation_record is not None
                else None
            ),
        }

    async def _run_supervisor(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        objective: str,
        loop_goal: str,
        constraints: list[str],
        control_session_id: str,
        interval_seconds: int,
        max_iterations: int,
        ends_at: float,
        stop_requested: asyncio.Event,
    ) -> None:
        child_task_ids: list[str] = []
        completed_iterations = 0
        runtime_state = self._initial_runtime_state(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=self._now(),
            next_run_at=self._now(),
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_started",
            status="running",
            title="Supervisor started",
            payload={
                "summary": (
                    f"Maintaining objective for up to {max_iterations} iteration(s)."
                ),
                "supervisor": {
                    "control_session_id": control_session_id,
                    "max_iterations": max_iterations,
                    "ends_at": ends_at,
                },
            },
        )
        try:
            try:
                for iteration in range(1, max_iterations + 1):
                    if stop_requested.is_set():
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                    now = self._now()
                    if now >= ends_at and completed_iterations > 0:
                        break

                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "next_run_at": now,
                                "child_task_ids": child_task_ids,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_started",
                        status="running",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": f"Starting iteration {iteration}.",
                            "iteration": iteration,
                        },
                    )

                    result, child_task_id = await self._run_control_loop_with_task(
                        user_id=user_id,
                        session_id=control_session_id,
                        owner_session_id=owner_session_id,
                        goal=loop_goal,
                        constraints=constraints,
                        request_id=None,
                        source="supervisor",
                        preserve_control_ui_tab=False,
                        parent_task_id=task_id,
                        reset_if_terminal=False,
                    )
                    child_task_ids.append(child_task_id)
                    completed_iterations = iteration
                    has_more_iterations = (
                        iteration < max_iterations and self._now() < ends_at
                    )
                    scheduled_next_run_at = (
                        min(ends_at, self._now() + float(interval_seconds))
                        if has_more_iterations
                        else None
                    )
                    runtime_report = self._record_runtime_iteration(
                        runtime_state,
                        objective=objective,
                        iteration=iteration,
                        max_iterations=max_iterations,
                        result=result,
                        child_task_id=child_task_id,
                        next_run_at=scheduled_next_run_at,
                        has_more_iterations=has_more_iterations,
                    )
                    result_summary = {
                        "success": result.success,
                        "final_text": result.final_text,
                        "plan_id": result.plan_id,
                        "verification_report_id": result.verification_report_id,
                        "needs_human": bool(result.metadata.get("needs_human")),
                        "child_task_id": child_task_id,
                        "failure_type": runtime_report["failure_type"],
                        "scheduler_queue": runtime_report["scheduler_queue"],
                        "budget_exhausted": bool(
                            runtime_report["budget_state"].get("budget_exhausted")
                        ),
                    }
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "completed_iterations": completed_iterations,
                                "child_task_ids": child_task_ids,
                                "last_child_task_id": child_task_id,
                                "last_result": result_summary,
                            },
                            "durable_execution": runtime_report["durable_execution"],
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_completed",
                        status="completed" if result.success else "failed",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": result.final_text,
                            "iteration": iteration,
                            "child_task_id": child_task_id,
                            "result": result_summary,
                            "runtime": {
                                "recovery_decision": runtime_report["recovery_decision"],
                                "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                                "budget_state": runtime_report["budget_state"],
                                "escalation_record": runtime_report["escalation_record"],
                            },
                        },
                    )

                    if runtime_report["scheduler_queue"] == SchedulerQueueKind.WAITING_FOR_APPROVAL.value:
                        await self._finish_waiting_for_approval(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                            runtime_report=runtime_report,
                        )
                        return
                    if runtime_report["scheduler_queue"] == SchedulerQueueKind.BLOCKED.value:
                        await self._finish_runtime_blocked(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                            runtime_report=runtime_report,
                        )
                        return

                    if runtime_report["scheduler_queue"] == SchedulerQueueKind.COMPLETED.value:
                        break

                    if iteration >= max_iterations or self._now() >= ends_at:
                        break

                    next_run_at = scheduled_next_run_at if scheduled_next_run_at is not None else self._now()
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "next_run_at": next_run_at,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_waiting",
                        status="running",
                        title="Waiting for next iteration",
                        payload={
                            "summary": (
                                "Waiting for the next runtime queue release "
                                f"before iteration {iteration + 1}."
                            ),
                            "iteration": iteration,
                            "next_run_at": next_run_at,
                            "scheduler_queue": runtime_report["scheduler_queue"],
                        },
                    )
                    if await self._wait_for_stop_or_timeout(
                        stop_requested=stop_requested,
                        timeout_seconds=max(0.0, next_run_at - self._now()),
                    ):
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                self._update_task_record(
                    task_id,
                    status="completed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        },
                        "durable_execution": self._serialize_runtime_state(
                            runtime_state,
                            objective=objective,
                        ),
                    },
                    metadata={"completed_iterations": completed_iterations},
                    error=None,
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_completed",
                    status="completed",
                    title="Supervisor completed",
                    payload={
                        "summary": (
                            f"Completed long-running supervision after {completed_iterations} successful iteration(s)."
                        ),
                        "completed_iterations": completed_iterations,
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="completed",
                    message=(
                        f"Long-running control supervisor completed after {completed_iterations} successful iteration(s)."
                    ),
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
            except Exception as exc:
                self._update_task_record(
                    task_id,
                    status="failed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        },
                        "result": {
                            "success": False,
                            "final_text": f"Supervisor crashed: {exc}",
                        },
                    },
                    error=f"supervisor_error:{exc}",
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_error",
                    status="failed",
                    title="Supervisor crashed",
                    payload={
                        "summary": f"Supervisor crashed: {exc}",
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="failed",
                    message=f"Long-running control supervisor crashed: {exc}",
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
                raise
        finally:
            self._handles.pop(task_id, None)

    async def _finish_waiting_for_approval(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
        runtime_report: dict[str, Any],
    ) -> None:
        approval_request = result.metadata.get("approval_request")
        self._update_task_record(
            task_id,
            status="pending",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "approval_request": approval_request,
                    "failure_type": runtime_report.get("failure_type"),
                    "scheduler_queue": runtime_report.get("scheduler_queue"),
                },
                "durable_execution": runtime_report["durable_execution"],
            },
            metadata={
                "needs_human": True,
                "blocking_child_task_id": child_task_id,
                "normalized_failure_type": runtime_report.get("failure_type"),
            },
            error="supervisor_needs_human",
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_blocked",
            status="pending",
            title="Supervisor blocked",
            payload={
                "summary": "Supervisor stopped because a child control-loop task requires human approval.",
                "child_task_id": child_task_id,
                "approval_request": approval_request,
                "runtime": {
                    "recovery_decision": runtime_report["recovery_decision"],
                    "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                    "escalation_record": runtime_report["escalation_record"],
                },
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="blocked",
            message=(
                "Supervisor stopped because a child control-loop task requires human approval."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_runtime_blocked(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
        runtime_report: dict[str, Any],
    ) -> None:
        self._update_task_record(
            task_id,
            status="blocked",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "failure_type": runtime_report.get("failure_type"),
                    "scheduler_queue": runtime_report.get("scheduler_queue"),
                },
                "durable_execution": runtime_report["durable_execution"],
            },
            metadata={
                "blocking_child_task_id": child_task_id,
                "normalized_failure_type": runtime_report.get("failure_type"),
            },
            error=(
                result.final_text
                or ",".join(runtime_report["budget_state"].get("budget_exhausted_reasons") or [])
                or "control supervisor blocked"
            ),
            ended_at=self._now(),
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_blocked_by_runtime",
            status="blocked",
            title="Supervisor blocked",
            payload={
                "summary": result.final_text,
                "child_task_id": child_task_id,
                "runtime": {
                    "recovery_decision": runtime_report["recovery_decision"],
                    "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                    "budget_state": runtime_report["budget_state"],
                },
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="blocked",
            message=result.final_text or "Long-running control supervisor blocked.",
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_cancelled(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
    ) -> None:
        self._update_task_record(
            task_id,
            status="cancelled",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "next_run_at": None,
                    "stop_requested": True,
                },
            },
            metadata={"stop_requested": True},
            error=None,
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_cancelled",
            status="cancelled",
            title="Supervisor stopped",
            payload={
                "summary": (
                    f"Supervisor stopped after {completed_iterations} completed iteration(s)."
                ),
                "completed_iterations": completed_iterations,
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="cancelled",
            message=(
                f"Long-running control supervisor stopped after {completed_iterations} completed iteration(s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _wait_for_stop_or_timeout(
        self,
        *,
        stop_requested: asyncio.Event,
        timeout_seconds: float,
    ) -> bool:
        if stop_requested.is_set():
            return True
        if timeout_seconds <= 0:
            return stop_requested.is_set()
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            return False
