from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.runtime.durable_execution_schema import (
    DurableEscalationRecord,
    DurableJobRunStatus,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    EscalationStatus,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryBudgetImpact,
    RecoveryDecision,
    RecoveryPolicy,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)


def task_node_status_from_verdict(
    verdict: DurableVerifierVerdictValue,
) -> DurableTaskNodeStatus:
    if verdict == DurableVerifierVerdictValue.PASS:
        return DurableTaskNodeStatus.DONE
    if verdict in {
        DurableVerifierVerdictValue.UNCERTAIN,
        DurableVerifierVerdictValue.UNSAFE,
    }:
        return DurableTaskNodeStatus.BLOCKED
    return DurableTaskNodeStatus.FAILED


def job_run_status_from_verdict(
    verdict: DurableVerifierVerdictValue,
) -> DurableJobRunStatus:
    if verdict == DurableVerifierVerdictValue.PASS:
        return DurableJobRunStatus.COMPLETED
    if verdict in {
        DurableVerifierVerdictValue.UNCERTAIN,
        DurableVerifierVerdictValue.UNSAFE,
    }:
        return DurableJobRunStatus.BLOCKED
    return DurableJobRunStatus.FAILED


def default_guardrail_budget_policy(
    budget: dict[str, Any] | None = None,
) -> GuardrailBudgetPolicy:
    payload = budget if isinstance(budget, dict) else {}

    def _int_field(name: str, default: int) -> int:
        raw = payload.get(name, default)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    return GuardrailBudgetPolicy(
        max_runtime_hours=max(1, _int_field("max_runtime_hours", 72)),
        max_total_llm_calls=max(1, _int_field("max_total_llm_calls", 1200)),
        max_total_tool_calls=max(1, _int_field("max_total_tool_calls", 5000)),
        max_same_failure_retries=max(0, _int_field("max_same_failure_retries", 3)),
        max_repair_depth=max(0, _int_field("max_repair_depth", 2)),
        max_pending_approvals=max(0, _int_field("max_pending_approvals", 5)),
    )


def default_recovery_policies() -> dict[str, RecoveryPolicy]:
    return {
        "weak_evidence": RecoveryPolicy(
            failure_type="weak_evidence",
            allowed_actions=[
                RecoveryActionType.GATHER_DESTINATION_EVIDENCE,
                RecoveryActionType.REQUEST_HUMAN_APPROVAL,
            ],
            retry_limit=1,
            escalation_condition="unresolved_uncertain_verdict",
            budget_impact=RecoveryBudgetImpact(tool_calls=1, pending_approvals=1),
            next_scheduler_queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
        ),
        "focus_mismatch": RecoveryPolicy(
            failure_type="focus_mismatch",
            allowed_actions=[
                RecoveryActionType.RESELECT_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "wrong_surface": RecoveryPolicy(
            failure_type="wrong_surface",
            allowed_actions=[
                RecoveryActionType.SWITCH_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "target_context_mismatch": RecoveryPolicy(
            failure_type="target_context_mismatch",
            allowed_actions=[
                RecoveryActionType.SWITCH_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "unknown": RecoveryPolicy(
            failure_type="unknown",
            allowed_actions=[
                RecoveryActionType.INSPECT_REPLAY,
                RecoveryActionType.MARK_FAILED,
            ],
            retry_limit=0,
            escalation_condition="manual_triage_required",
            budget_impact=RecoveryBudgetImpact(),
            next_scheduler_queue=SchedulerQueueKind.BLOCKED,
        ),
        "policy_blocked": RecoveryPolicy(
            failure_type="policy_blocked",
            allowed_actions=[RecoveryActionType.REQUEST_HUMAN_APPROVAL],
            retry_limit=0,
            escalation_condition="approval_required",
            budget_impact=RecoveryBudgetImpact(pending_approvals=1),
            next_scheduler_queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
        ),
        "tool_timeout": RecoveryPolicy(
            failure_type="tool_timeout",
            allowed_actions=[RecoveryActionType.RETRY_WITH_BACKOFF],
            retry_limit=2,
            escalation_condition="backoff_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
    }


def recovery_policy_for_failure_type(
    failure_type: str | None,
) -> RecoveryPolicy | None:
    if not str(failure_type or "").strip():
        return None
    policies = default_recovery_policies()
    return policies.get(str(failure_type or "unknown"), policies["unknown"])


def budget_exhaustion_reasons(
    *,
    budget_policy: GuardrailBudgetPolicy,
    runtime_hours_used: int,
    llm_calls_used: int,
    tool_calls_used: int,
    repair_depth_used: int,
    pending_approvals_count: int,
    next_scheduler_queue: SchedulerQueueKind,
    failure_type: str | None,
    retry_count: int,
) -> list[str]:
    reasons: list[str] = []
    if runtime_hours_used > budget_policy.max_runtime_hours:
        reasons.append("max_runtime_hours_exhausted")
    if llm_calls_used > budget_policy.max_total_llm_calls:
        reasons.append("max_total_llm_calls_exhausted")
    if tool_calls_used > budget_policy.max_total_tool_calls:
        reasons.append("max_total_tool_calls_exhausted")
    if repair_depth_used > budget_policy.max_repair_depth:
        reasons.append("max_repair_depth_exhausted")
    if pending_approvals_count > budget_policy.max_pending_approvals:
        reasons.append("max_pending_approvals_exhausted")
    if (
        next_scheduler_queue == SchedulerQueueKind.RETRY_LATER
        and failure_type
        and retry_count >= budget_policy.max_same_failure_retries
    ):
        reasons.append("max_same_failure_retries_exhausted")
    return reasons


def repair_depth_increment(chosen_action: RecoveryActionType | None) -> int:
    if chosen_action in {
        RecoveryActionType.RESELECT_SURFACE,
        RecoveryActionType.GATHER_DESTINATION_EVIDENCE,
        RecoveryActionType.SWITCH_SURFACE,
        RecoveryActionType.RETRY_WITH_BACKOFF,
        RecoveryActionType.INSPECT_REPLAY,
    }:
        return 1
    return 0


def scheduler_available_at(
    queue: SchedulerQueueKind,
    *,
    created_at: datetime,
) -> datetime | None:
    if queue == SchedulerQueueKind.RETRY_LATER:
        return (created_at + timedelta(minutes=15)).replace(second=0, microsecond=0)
    if queue == SchedulerQueueKind.PERIODIC_CHECK:
        return (created_at + timedelta(hours=1)).replace(second=0, microsecond=0)
    return None


def scheduler_queue_reason(
    decision: RecoveryDecision,
    *,
    verifier_verdict: DurableVerifierVerdict,
) -> str:
    if decision.budget_exhausted:
        return "guardrail_budget_exhausted"
    if decision.next_scheduler_queue == SchedulerQueueKind.COMPLETED:
        return "verifier_passed"
    if decision.next_scheduler_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
        if verifier_verdict.verdict == DurableVerifierVerdictValue.UNCERTAIN:
            return "unresolved_uncertain_verifier_result"
        if verifier_verdict.verdict == DurableVerifierVerdictValue.UNSAFE:
            return "unsafe_verifier_boundary"
        return "human_approval_required"
    if decision.next_scheduler_queue == SchedulerQueueKind.RETRY_LATER:
        return f"{decision.failure_type or 'unknown'}_retry_later"
    if decision.next_scheduler_queue == SchedulerQueueKind.PERIODIC_CHECK:
        return f"{decision.failure_type or 'unknown'}_periodic_check"
    if decision.next_scheduler_queue == SchedulerQueueKind.READY:
        return "ready_for_worker"
    return f"{decision.failure_type or 'unknown'}_blocked"


def append_scheduler_queue_entry(
    queue_state: SchedulerQueueState,
    entry: SchedulerQueueEntry,
) -> None:
    if entry.queue == SchedulerQueueKind.READY:
        queue_state.ready_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
        queue_state.waiting_for_approval_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.RETRY_LATER:
        queue_state.retry_later_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.PERIODIC_CHECK:
        queue_state.periodic_check_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.COMPLETED:
        queue_state.completed_queue.append(entry)
    else:
        queue_state.blocked_queue.append(entry)


def build_escalation_record(
    *,
    run_id: str,
    node_id: str,
    checkpoint_id: str,
    created_at: datetime,
    failure_type: str | None,
    reason: str,
    approval_request_id: str | None = None,
    audit_ref: str | None = None,
) -> DurableEscalationRecord:
    resolved_approval_request_id = approval_request_id or f"approval:{run_id}"
    return DurableEscalationRecord(
        escalation_id=f"{run_id}/escalation",
        node_id=node_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        status=EscalationStatus.WAITING_FOR_APPROVAL,
        reason=reason,
        failure_type=failure_type,
        approval_request_id=resolved_approval_request_id,
        audit_ref=audit_ref or f"audit://{run_id}/approval",
        resume_checkpoint_id=checkpoint_id,
        created_at=created_at,
        updated_at=created_at,
    )
