from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.runtime.limited_live_action_gate import build_limited_live_action_gate_result
from src.runtime.limited_live_action_rehearsal import build_limited_live_action_rehearsal
from src.runtime.task_store import get_task_store
from src.runtime.tenth_stage_readiness import (
    TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION,
    TenthStageLiveActionStatus,
    TenthStageReadinessCheck,
    TenthStageReadinessError,
    TenthStageReadinessStatus,
    attach_tenth_stage_readiness_check,
    build_tenth_stage_readiness_check,
)


NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ready_rehearsal():
    gate = build_limited_live_action_gate_result(
        subject_id="episode:tenth-stage",
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
    return build_limited_live_action_rehearsal(
        limited_live_action_gate=gate,
        mission_contract_ref="mission-contract:tenth-stage",
        now=NOW,
    )


def _complete_check(**overrides):
    kwargs = {
        "limited_live_action_rehearsal": _ready_rehearsal(),
        "adopting_organization_ref": "org:adopting-lab",
        "hardware_owner_ref": "hardware-owner:responsible-owner",
        "certified_or_autopilot_controller_ref": "controller:certified-autopilot",
        "emergency_stop_process_ref": "estop-process:documented",
        "now": NOW,
    }
    kwargs.update(overrides)
    return build_tenth_stage_readiness_check(**kwargs)


def _task(status: str = "running"):
    return get_task_store().create(
        kind="control_supervisor",
        title="10th-stage readiness attach test",
        status=status,
        artifacts={"existing": {"kept": True}},
        approval_dependencies=["approval:existing"],
    )


def test_complete_rehearsal_is_ready_for_organization_review_but_live_blocked():
    check = _complete_check()

    assert check.schema_version == TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION
    assert check.readiness_status == (
        TenthStageReadinessStatus.READY_FOR_ORGANIZATION_REVIEW
    )
    assert check.live_action_status == TenthStageLiveActionStatus.BLOCKED_FOR_LIVE_ACTION
    assert check.missing_preconditions == ()
    assert check.blocked_reasons == ()
    assert check.limited_live_action_rehearsal_ref == _ready_rehearsal().rehearsal_id
    assert check.mission_contract_ref == "mission-contract:tenth-stage"
    assert check.emergency_stop_evidence_ref == "estop:verified"
    assert check.rollback_plan_ref == "rollback:stop-and-hold"
    assert check.operator_responsibility_ack_ref == "operator-ack:alice"
    assert check.adopting_organization_ref == "org:adopting-lab"
    assert check.hardware_owner_ref == "hardware-owner:responsible-owner"
    assert check.certified_or_autopilot_controller_ref == (
        "controller:certified-autopilot"
    )
    assert check.emergency_stop_process_ref == "estop-process:documented"
    assert "operator_approval_not_performed" in check.live_action_blocked_reasons
    assert "live_dispatch_not_implemented" in check.live_action_blocked_reasons
    assert check.operator_approval_required is True
    assert check.operator_approval_performed is False
    assert check.stronger_execution_allowed is False


@pytest.mark.parametrize(
    "field",
    [
        "adopting_organization_ref",
        "hardware_owner_ref",
        "certified_or_autopilot_controller_ref",
        "emergency_stop_process_ref",
    ],
)
def test_missing_organization_or_hardware_precondition_blocks(field):
    check = _complete_check(**{field: " "})

    assert check.readiness_status == TenthStageReadinessStatus.BLOCKED
    assert field in check.missing_preconditions
    assert f"missing_precondition:{field}" in check.blocked_reasons
    assert check.live_action_status == TenthStageLiveActionStatus.BLOCKED_FOR_LIVE_ACTION


def test_blocked_rehearsal_blocks_tenth_stage_check():
    gate = build_limited_live_action_gate_result(
        subject_id="episode:tenth-stage-blocked",
        proposed_action_ref="dry-run-envelope:review-only",
        autonomy_gate_result_refs=["autonomy-gate:passed"],
        now=NOW,
    )
    rehearsal = build_limited_live_action_rehearsal(
        limited_live_action_gate=gate,
        mission_contract_ref="mission-contract:tenth-stage",
        now=NOW,
    )

    check = _complete_check(limited_live_action_rehearsal=rehearsal)

    assert check.readiness_status == TenthStageReadinessStatus.BLOCKED
    assert "limited_live_action_rehearsal_ready_for_operator_review" in (
        check.missing_preconditions
    )
    assert (
        "missing_precondition:limited_live_action_rehearsal_ready_for_operator_review"
        in check.blocked_reasons
    )


def test_all_safety_booleans_remain_pinned():
    check = _complete_check()

    assert check.organization_review_required is True
    assert check.operator_approval_required is True
    assert check.operator_approval_performed is False
    assert check.stronger_execution_allowed is False
    assert check.live_execution_allowed is False
    assert check.physical_execution_invoked is False
    assert check.command_payload_allowed is False
    assert check.dispatch_implementation_present is False
    assert check.ros_dispatch_allowed is False
    assert check.mavlink_dispatch_allowed is False
    assert check.actuator_execution_allowed is False
    assert check.rule_based is True
    assert check.llm_judge_used is False
    assert check.metadata["approval_created"] is False
    assert check.metadata["promotion_created"] is False
    assert check.metadata["runtime_reuse_created"] is False
    assert check.metadata["dispatch_surface_added"] is False


def test_attach_helper_preserves_task_status_and_existing_artifacts():
    task = _task(status="running")
    check = _complete_check()

    attached = attach_tenth_stage_readiness_check(task["task_id"], check)

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert (
        stored["artifacts"]["tenth_stage_readiness_check"]
        == attached["tenth_stage_readiness_check"]
    )


def test_attach_helper_does_not_create_approval_promotion_or_runtime_reuse():
    task = _task(status="accepted")

    attach_tenth_stage_readiness_check(task["task_id"], _complete_check())

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "accepted"
    assert stored["approval_dependencies"] == ["approval:existing"]
    artifacts = stored["artifacts"]
    assert "approval" not in artifacts
    assert "promotion_package" not in artifacts
    assert "approved_promotion_artifact" not in artifacts
    assert "reuse_plan" not in artifacts
    readiness = artifacts["tenth_stage_readiness_check"]
    assert readiness["operator_approval_required"] is True
    assert readiness["operator_approval_performed"] is False
    assert readiness["live_execution_allowed"] is False
    assert readiness["physical_execution_invoked"] is False
    assert readiness["metadata"]["approval_created"] is False
    assert readiness["metadata"]["promotion_created"] is False
    assert readiness["metadata"]["runtime_reuse_created"] is False


def test_check_id_is_deterministic_and_ref_order_independent():
    rehearsal = _ready_rehearsal()
    first = _complete_check(limited_live_action_rehearsal=rehearsal)
    second = build_tenth_stage_readiness_check(
        limited_live_action_rehearsal=rehearsal.model_copy(
            update={"audit_refs": ("audit:chain-001",)}
        ),
        adopting_organization_ref="org:adopting-lab",
        hardware_owner_ref="hardware-owner:responsible-owner",
        certified_or_autopilot_controller_ref="controller:certified-autopilot",
        emergency_stop_process_ref="estop-process:documented",
        now=NOW,
    )

    assert first.evidence_refs == second.evidence_refs
    assert first.check_id == second.check_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator_approval_performed", True),
        ("stronger_execution_allowed", True),
        ("live_execution_allowed", True),
        ("physical_execution_invoked", True),
        ("command_payload_allowed", True),
        ("dispatch_implementation_present", True),
        ("ros_dispatch_allowed", True),
        ("mavlink_dispatch_allowed", True),
        ("actuator_execution_allowed", True),
        ("rule_based", False),
        ("llm_judge_used", True),
    ],
)
def test_pydantic_rejects_attempts_to_weaken_safety_invariants(field, value):
    payload = _complete_check().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        TenthStageReadinessCheck.model_validate(payload)


def test_attach_raises_when_task_does_not_exist():
    with pytest.raises(TenthStageReadinessError):
        attach_tenth_stage_readiness_check(
            "task_does_not_exist",
            _complete_check(),
        )
