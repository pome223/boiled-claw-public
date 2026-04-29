from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.runtime.limited_live_action_gate import (
    build_limited_live_action_gate_result,
)
from src.runtime.limited_live_action_rehearsal import (
    LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION,
    LimitedLiveActionRehearsal,
    LimitedLiveActionRehearsalError,
    LimitedLiveActionRehearsalStatus,
    attach_limited_live_action_rehearsal,
    build_limited_live_action_rehearsal,
)
from src.runtime.task_store import get_task_store


NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ready_gate():
    return build_limited_live_action_gate_result(
        subject_id="episode:limited-live-rehearsal",
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


def _ready_rehearsal(**overrides):
    kwargs = {
        "limited_live_action_gate": _ready_gate(),
        "mission_contract_ref": "mission-contract:limited-live",
        "now": NOW,
    }
    kwargs.update(overrides)
    return build_limited_live_action_rehearsal(**kwargs)


def _task(status: str = "running"):
    return get_task_store().create(
        kind="control_supervisor",
        title="Limited live action rehearsal attach test",
        status=status,
        artifacts={"existing": {"kept": True}},
        approval_dependencies=["approval:existing"],
    )


def test_complete_evidence_package_is_ready_for_operator_review():
    rehearsal = _ready_rehearsal()

    assert rehearsal.schema_version == LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION
    assert rehearsal.readiness_status == (
        LimitedLiveActionRehearsalStatus.READY_FOR_OPERATOR_REVIEW
    )
    assert rehearsal.missing_preconditions == ()
    assert rehearsal.blocked_reasons == ()
    assert rehearsal.mission_contract_ref == "mission-contract:limited-live"
    assert rehearsal.autonomy_gate_result_ref == "autonomy-gate:passed"
    assert rehearsal.hil_telemetry_review_ref == "hil-review:nominal"
    assert rehearsal.limited_live_action_gate_ref == _ready_gate().gate_id
    assert (
        rehearsal.limited_live_action_approval_package_ref
        == _ready_gate().approval_package.approval_package_id
    )
    assert rehearsal.emergency_stop_evidence_ref == "estop:verified"
    assert rehearsal.rollback_plan_ref == "rollback:stop-and-hold"
    assert rehearsal.operator_responsibility_ack_ref == "operator-ack:alice"
    assert rehearsal.audit_refs == ("audit:chain-001",)
    assert rehearsal.metadata["rehearsal_only"] is True
    assert rehearsal.metadata["dispatch_surface_added"] is False


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"emergency_stop_evidence_ref": " "}, "emergency_stop_evidence_ref"),
        ({"rollback_plan_ref": " "}, "rollback_plan_ref"),
        (
            {"operator_responsibility_ack_ref": " "},
            "operator_responsibility_ack_ref",
        ),
    ],
)
def test_missing_required_evidence_blocks_rehearsal(override, missing):
    gate = _ready_gate()
    # Remove the relevant fallback from the upstream gate so the explicit empty
    # override proves the precondition is truly missing.
    payload = gate.model_copy(
        update={
            "emergency_stop_evidence_refs": ()
            if missing == "emergency_stop_evidence_ref"
            else gate.emergency_stop_evidence_refs,
            "rollback_plan_refs": ()
            if missing == "rollback_plan_ref"
            else gate.rollback_plan_refs,
            "responsibility_ack_refs": ()
            if missing == "operator_responsibility_ack_ref"
            else gate.responsibility_ack_refs,
        }
    )

    rehearsal = build_limited_live_action_rehearsal(
        limited_live_action_gate=payload,
        mission_contract_ref="mission-contract:limited-live",
        now=NOW,
        **override,
    )

    assert rehearsal.readiness_status == LimitedLiveActionRehearsalStatus.BLOCKED
    assert missing in rehearsal.missing_preconditions
    assert f"missing_precondition:{missing}" in rehearsal.blocked_reasons


def test_gate_not_ready_blocks_rehearsal():
    blocked_gate = build_limited_live_action_gate_result(
        subject_id="episode:blocked-gate",
        proposed_action_ref="dry-run-envelope:review-only",
        autonomy_gate_result_refs=["autonomy-gate:passed"],
        now=NOW,
    )

    rehearsal = build_limited_live_action_rehearsal(
        limited_live_action_gate=blocked_gate,
        mission_contract_ref="mission-contract:limited-live",
        now=NOW,
    )

    assert rehearsal.readiness_status == LimitedLiveActionRehearsalStatus.BLOCKED
    assert "limited_live_action_gate_operator_review_ready" in (
        rehearsal.missing_preconditions
    )
    assert "missing_precondition:limited_live_action_gate_operator_review_ready" in (
        rehearsal.blocked_reasons
    )


def test_approval_package_not_ready_blocks_rehearsal():
    gate = _ready_gate()
    approval_package = gate.approval_package.model_copy(
        update={"required_evidence_refs": ()}
    )

    rehearsal = build_limited_live_action_rehearsal(
        limited_live_action_gate=gate,
        limited_live_action_approval_package=approval_package,
        mission_contract_ref="mission-contract:limited-live",
        now=NOW,
    )

    assert rehearsal.readiness_status == LimitedLiveActionRehearsalStatus.BLOCKED
    assert "limited_live_action_approval_package_ready" in (
        rehearsal.missing_preconditions
    )
    assert "missing_precondition:limited_live_action_approval_package_ready" in (
        rehearsal.blocked_reasons
    )


def test_all_safety_booleans_remain_pinned():
    rehearsal = _ready_rehearsal()

    assert rehearsal.live_execution_allowed is False
    assert rehearsal.physical_execution_invoked is False
    assert rehearsal.command_payload_allowed is False
    assert rehearsal.ros_dispatch_allowed is False
    assert rehearsal.mavlink_dispatch_allowed is False
    assert rehearsal.actuator_execution_allowed is False
    assert rehearsal.dispatch_implementation_present is False
    assert rehearsal.operator_approval_required is True
    assert rehearsal.operator_approval_performed is False
    assert rehearsal.stronger_execution_allowed is False
    assert rehearsal.rule_based is True
    assert rehearsal.llm_judge_used is False


def test_attach_helper_preserves_task_status_and_existing_artifacts():
    task = _task(status="running")
    rehearsal = _ready_rehearsal()

    attached = attach_limited_live_action_rehearsal(task["task_id"], rehearsal)

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert (
        stored["artifacts"]["limited_live_action_rehearsal"]
        == attached["limited_live_action_rehearsal"]
    )


def test_attach_helper_does_not_create_approval_promotion_or_runtime_reuse():
    task = _task(status="accepted")

    attach_limited_live_action_rehearsal(task["task_id"], _ready_rehearsal())

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "accepted"
    assert stored["approval_dependencies"] == ["approval:existing"]
    artifacts = stored["artifacts"]
    assert "approval" not in artifacts
    assert "promotion_package" not in artifacts
    assert "approved_promotion_artifact" not in artifacts
    assert "reuse_plan" not in artifacts
    rehearsal = artifacts["limited_live_action_rehearsal"]
    assert rehearsal["operator_approval_required"] is True
    assert rehearsal["operator_approval_performed"] is False
    assert rehearsal["metadata"]["approval_created"] is False
    assert rehearsal["metadata"]["promotion_created"] is False
    assert rehearsal["metadata"]["runtime_reuse_created"] is False


def test_rehearsal_id_is_deterministic_and_ref_order_independent():
    gate = _ready_gate()
    first = build_limited_live_action_rehearsal(
        limited_live_action_gate=gate,
        mission_contract_ref="mission-contract:limited-live",
        audit_refs=["audit:b", "audit:a", "audit:a"],
        now=NOW,
    )
    second = build_limited_live_action_rehearsal(
        limited_live_action_gate=gate,
        mission_contract_ref="mission-contract:limited-live",
        audit_refs=["audit:a", "audit:b"],
        now=NOW,
    )

    assert first.audit_refs == ("audit:a", "audit:b")
    assert first.evidence_refs == second.evidence_refs
    assert first.rehearsal_id == second.rehearsal_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_execution_allowed", True),
        ("physical_execution_invoked", True),
        ("stronger_execution_allowed", True),
        ("operator_approval_performed", True),
        ("command_payload_allowed", True),
        ("ros_dispatch_allowed", True),
        ("mavlink_dispatch_allowed", True),
        ("actuator_execution_allowed", True),
        ("dispatch_implementation_present", True),
        ("rule_based", False),
        ("llm_judge_used", True),
    ],
)
def test_pydantic_rejects_attempts_to_weaken_safety_invariants(field, value):
    payload = _ready_rehearsal().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        LimitedLiveActionRehearsal.model_validate(payload)


def test_attach_raises_when_task_does_not_exist():
    with pytest.raises(LimitedLiveActionRehearsalError):
        attach_limited_live_action_rehearsal(
            "task_does_not_exist",
            _ready_rehearsal(),
        )
