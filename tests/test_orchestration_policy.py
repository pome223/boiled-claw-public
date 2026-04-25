import pytest

from src.runtime.durable_execution_schema import (
    RecoveryActionType,
    RecoveryLadderStep,
    RecoveryOutcome,
    SchedulerQueueKind,
)
from src.runtime.orchestration_policy import recovery_outcome_for_queue
from src.runtime.orchestration_policy import recovery_ladder_step_for_decision
from src.runtime.orchestration_policy import recovery_action_for_ladder_step
from src.runtime.orchestration_policy import scheduler_queue_for_recovery_ladder_step
from src.runtime.orchestration_policy import select_recovery_ladder_step


RECOVERY_ACTION_LADDER_CASES = [
    (RecoveryActionType.GATHER_DESTINATION_EVIDENCE, RecoveryLadderStep.VERIFY_STATE),
    (RecoveryActionType.RESELECT_SURFACE, RecoveryLadderStep.OBSERVE_AGAIN),
    (RecoveryActionType.SWITCH_SURFACE, RecoveryLadderStep.ALTERNATE_CAPABILITY),
    (RecoveryActionType.RETRY_WITH_BACKOFF, RecoveryLadderStep.RETRY_SMALLER_STEP),
    (RecoveryActionType.INSPECT_REPLAY, RecoveryLadderStep.DIAGNOSTIC_TASK),
    (RecoveryActionType.REQUEST_HUMAN_APPROVAL, RecoveryLadderStep.REQUEST_APPROVAL),
    (RecoveryActionType.MARK_FAILED, RecoveryLadderStep.PAUSE_OR_BLOCK),
]


@pytest.mark.parametrize(
    ("action", "expected"),
    RECOVERY_ACTION_LADDER_CASES,
)
def test_recovery_ladder_step_for_decision_maps_actions(action, expected):
    assert {case[0] for case in RECOVERY_ACTION_LADDER_CASES} == set(RecoveryActionType)
    assert (
        recovery_ladder_step_for_decision(
            chosen_action=action,
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        )
        == expected
    )


def test_recovery_ladder_step_for_decision_handles_terminal_and_budget_cases():
    assert (
        recovery_ladder_step_for_decision(
            chosen_action=None,
            next_scheduler_queue=SchedulerQueueKind.COMPLETED,
        )
        is None
    )
    assert recovery_ladder_step_for_decision(
        chosen_action=None,
        next_scheduler_queue=SchedulerQueueKind.READY,
        budget_exhausted=True,
    ) == RecoveryLadderStep.PAUSE_OR_BLOCK
    assert recovery_ladder_step_for_decision(
        chosen_action=None,
        next_scheduler_queue=SchedulerQueueKind.READY,
    ) == RecoveryLadderStep.RETRY_SAME_STEP


def test_select_recovery_ladder_step_escalates_when_step_retry_budget_is_exhausted():
    step, reason = select_recovery_ladder_step(
        preferred_step=RecoveryLadderStep.RETRY_SMALLER_STEP,
        ladder=[
            "verify_state",
            "retry_smaller_step",
            "request_approval",
            "pause_or_block",
        ],
        retry_count=1,
        max_retries_per_step=1,
    )

    assert step == RecoveryLadderStep.REQUEST_APPROVAL
    assert reason == "mission_recovery_step_retry_limit_exhausted"
    assert recovery_action_for_ladder_step(step) == RecoveryActionType.REQUEST_HUMAN_APPROVAL
    assert scheduler_queue_for_recovery_ladder_step(
        step,
        fallback=SchedulerQueueKind.RETRY_LATER,
    ) == SchedulerQueueKind.WAITING_FOR_APPROVAL


def test_scheduler_queue_for_nonterminal_recovery_step_preserves_existing_policy_queue():
    assert scheduler_queue_for_recovery_ladder_step(
        RecoveryLadderStep.VERIFY_STATE,
        fallback=SchedulerQueueKind.WAITING_FOR_APPROVAL,
    ) == SchedulerQueueKind.WAITING_FOR_APPROVAL


def test_recovery_outcome_prefers_operator_visible_queue_state():
    assert recovery_outcome_for_queue(
        SchedulerQueueKind.WAITING_FOR_APPROVAL,
        result_success=True,
    ) == RecoveryOutcome.PAUSED
    assert recovery_outcome_for_queue(
        SchedulerQueueKind.BLOCKED,
        result_success=True,
    ) == RecoveryOutcome.BLOCKED
    assert recovery_outcome_for_queue(
        SchedulerQueueKind.RETRY_LATER,
        result_success=False,
        cancelled=True,
    ) == RecoveryOutcome.CANCELLED
