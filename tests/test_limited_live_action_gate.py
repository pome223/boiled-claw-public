from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.runtime.limited_live_action_gate import (
    LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION,
    LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION,
    REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS,
    LimitedLiveActionApprovalPackage,
    LimitedLiveActionGateError,
    LimitedLiveActionGateResult,
    LimitedLiveActionGateStatus,
    build_limited_live_action_gate_result,
)


NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def _complete_gate() -> LimitedLiveActionGateResult:
    return build_limited_live_action_gate_result(
        subject_id="episode:clean-goal",
        proposed_action_ref="dry-run-envelope:review-only",
        autonomy_gate_result_refs=["autonomy-gate:passed"],
        hil_telemetry_review_refs=["hil-review:nominal"],
        emergency_stop_evidence_refs=["estop:verified"],
        rollback_plan_refs=["rollback:stop-and-hold"],
        action_allowlist_refs=["allowlist:bounded-forward"],
        responsibility_ack_refs=["operator-ack:alice"],
        audit_refs=["audit:chain-001"],
        now=NOW,
    )


def test_schema_versions_are_explicit() -> None:
    gate = _complete_gate()

    assert gate.schema_version == LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION
    assert (
        gate.approval_package.schema_version
        == LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION
    )


def test_missing_preconditions_block_gate() -> None:
    gate = build_limited_live_action_gate_result(
        subject_id="episode:missing-preconditions",
        proposed_action_ref="dry-run-envelope:review-only",
        now=NOW,
    )

    assert gate.passed is False
    assert gate.status == LimitedLiveActionGateStatus.BLOCKED
    assert gate.missing_preconditions == tuple(
        sorted(REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS)
    )
    assert set(gate.blocked_reasons) == {
        f"missing_precondition:{item}"
        for item in REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS
    }
    assert gate.operator_approval_required is True
    assert gate.operator_approval_performed is False
    assert gate.stronger_execution_allowed is False
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.command_payload_allowed is False
    assert gate.dispatch_implementation_present is False
    assert gate.ros_dispatch_allowed is False
    assert gate.mavlink_dispatch_allowed is False
    assert gate.actuator_execution_allowed is False


def test_complete_preconditions_are_operator_review_ready_not_live_allowed() -> None:
    gate = _complete_gate()

    assert gate.passed is True
    assert gate.status == LimitedLiveActionGateStatus.OPERATOR_REVIEW_READY
    assert gate.blocked_reasons == ()
    assert gate.missing_preconditions == ()
    assert gate.warning_reasons == (
        "operator_approval_required_before_any_stronger_execution",
        "design_only_no_dispatch_implementation",
    )
    assert gate.operator_approval_required is True
    assert gate.operator_approval_performed is False
    assert gate.stronger_execution_allowed is False
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.command_payload_allowed is False
    assert gate.dispatch_implementation_present is False
    assert gate.ros_dispatch_allowed is False
    assert gate.mavlink_dispatch_allowed is False
    assert gate.actuator_execution_allowed is False
    assert gate.approval_package.operator_approval_required is True
    assert gate.approval_package.operator_approval_performed is False
    assert gate.approval_package.approval_ref is None
    assert gate.approval_package.emergency_stop_required is True
    assert gate.approval_package.rollback_plan_required is True
    assert gate.approval_package.action_allowlist_required is True
    assert gate.metadata["action_allowlist_scope"] == "proposal_categories_only"
    assert gate.approval_package.metadata["action_allowlist_scope"] == (
        "proposal_categories_only"
    )
    assert gate.metadata["dispatch_surface_added"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule_based", False),
        ("llm_judge_used", True),
        ("operator_approval_performed", True),
        ("stronger_execution_allowed", True),
        ("live_execution_allowed", True),
        ("physical_execution_invoked", True),
        ("command_payload_allowed", True),
        ("dispatch_implementation_present", True),
        ("ros_dispatch_allowed", True),
        ("mavlink_dispatch_allowed", True),
        ("actuator_execution_allowed", True),
    ],
)
def test_gate_rejects_weakened_safety_invariants(field: str, value: bool) -> None:
    payload = _complete_gate().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        LimitedLiveActionGateResult.model_validate(payload)


def test_gate_rejects_command_like_metadata_recursively() -> None:
    with pytest.raises(LimitedLiveActionGateError, match="RosTopic"):
        build_limited_live_action_gate_result(
            subject_id="episode:metadata-command",
            proposed_action_ref="dry-run-envelope:review-only",
            metadata={"nested": [{"RosTopic": "/cmd_vel"}]},
            now=NOW,
        )


def test_approval_package_rejects_command_like_metadata_recursively() -> None:
    with pytest.raises(ValidationError, match="velocityCommand"):
        LimitedLiveActionApprovalPackage.model_validate(
            {
                "approval_package_id": "limited-live-approval:test",
                "subject_id": "episode:metadata-command",
                "proposed_action_ref": "dry-run-envelope:review-only",
                "responsibility_summary": "Operator must review evidence before any stronger execution.",
                "created_at": NOW,
                "metadata": {"history": [{"velocityCommand": "1.0"}]},
            }
        )


def test_extra_command_payload_fields_are_rejected() -> None:
    payload = _complete_gate().model_dump(mode="python")
    payload["command_payload"] = {"motor": "start"}

    with pytest.raises(ValidationError):
        LimitedLiveActionGateResult.model_validate(payload)


def test_gate_id_is_deterministic_and_ref_order_independent() -> None:
    first = build_limited_live_action_gate_result(
        subject_id="episode:stable",
        proposed_action_ref="dry-run-envelope:review-only",
        autonomy_gate_result_refs=["b", "a", "a"],
        hil_telemetry_review_refs=["hil-review:nominal"],
        emergency_stop_evidence_refs=["estop:verified"],
        rollback_plan_refs=["rollback:stop-and-hold"],
        action_allowlist_refs=["allowlist:bounded-forward"],
        responsibility_ack_refs=["operator-ack:alice"],
        audit_refs=["audit:chain-001"],
        now=NOW,
    )
    second = build_limited_live_action_gate_result(
        subject_id="episode:stable",
        proposed_action_ref="dry-run-envelope:review-only",
        autonomy_gate_result_refs=["a", "b"],
        hil_telemetry_review_refs=["hil-review:nominal"],
        emergency_stop_evidence_refs=["estop:verified"],
        rollback_plan_refs=["rollback:stop-and-hold"],
        action_allowlist_refs=["allowlist:bounded-forward"],
        responsibility_ack_refs=["operator-ack:alice"],
        audit_refs=["audit:chain-001"],
        now=NOW,
    )

    assert first.autonomy_gate_result_refs == ("a", "b")
    assert first.gate_id == second.gate_id
    assert first.approval_package.approval_package_id == (
        second.approval_package.approval_package_id
    )


def test_builder_requires_subject_and_proposed_action_ref() -> None:
    with pytest.raises(LimitedLiveActionGateError, match="subject_id"):
        build_limited_live_action_gate_result(
            subject_id=" ",
            proposed_action_ref="dry-run-envelope:review-only",
            now=NOW,
        )

    with pytest.raises(LimitedLiveActionGateError, match="proposed_action_ref"):
        build_limited_live_action_gate_result(
            subject_id="episode:missing-action",
            proposed_action_ref=" ",
            now=NOW,
        )
