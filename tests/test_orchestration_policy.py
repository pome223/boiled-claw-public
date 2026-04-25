import pytest

from src.runtime.durable_execution_schema import (
    RecoveryActionType,
    RecoveryLadderStep,
    SchedulerQueueKind,
)
from src.runtime.orchestration_policy import recovery_ladder_step_for_decision


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
