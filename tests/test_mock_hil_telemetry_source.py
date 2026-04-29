"""Tests for the mock HIL telemetry source.

Closes the HIL telemetry chain end-to-end without touching real hardware.
Pin both the happy path (mock source -> envelope -> evidence -> review ->
gate -> task.artifacts) and the fail-closed path (rejected payloads do
NOT pollute the task store).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryRejected,
)
from src.runtime.hil_telemetry_evidence import (
    HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION,
    HilTelemetryEvidenceStatus,
)
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
    HIL_REVIEW_BUCKET_STALE,
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReviewStatus,
)
from src.runtime.mock_hil_telemetry_source import (
    DEFAULT_MOCK_HIL_CONTRACT_ID,
    DEFAULT_MOCK_HIL_SUBJECT_ID,
    DEFAULT_MOCK_HIL_SUBJECT_KIND,
    MockHilTelemetryAttachError,
    attach_mock_hil_telemetry_chain,
    build_mock_hil_telemetry_chain,
    build_mock_hil_telemetry_contract,
    build_mock_hil_telemetry_envelope,
)
from src.runtime.task_store import get_task_store
from src.runtime.toy_grid_world import (
    ToyGridWorldAction,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_safety_regression_gate,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
)
from src.runtime.mission_contract import build_mission_contract


CAPTURED_AT = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
FRESH_NOW = CAPTURED_AT + timedelta(seconds=10)
LATE_NOW = CAPTURED_AT + timedelta(seconds=120)


# ---------------------------------------------------------------------------
# contract / envelope helpers
# ---------------------------------------------------------------------------


def test_mock_contract_pins_telemetry_only_invariants():
    contract = build_mock_hil_telemetry_contract()

    assert contract.schema_version == HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    assert contract.contract_id == DEFAULT_MOCK_HIL_CONTRACT_ID
    assert contract.subject_kind == DEFAULT_MOCK_HIL_SUBJECT_KIND
    assert contract.supports_action_dispatch is False
    assert contract.supports_command_payload is False
    assert contract.supports_live_execution is False
    assert contract.supports_physical_execution is False
    assert contract.supports_ros_dispatch is False
    assert contract.operator_approval_required is True
    assert contract.mode.value == "telemetry_only"


def test_mock_envelope_uses_canonical_defaults():
    envelope = build_mock_hil_telemetry_envelope(captured_at=CAPTURED_AT)

    assert envelope.schema_version == HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    assert envelope.contract_id == DEFAULT_MOCK_HIL_CONTRACT_ID
    assert envelope.subject_kind == DEFAULT_MOCK_HIL_SUBJECT_KIND
    assert envelope.subject_id == DEFAULT_MOCK_HIL_SUBJECT_ID
    assert envelope.measurements == {
        "battery": 100.0,
        "comms_ok": True,
        "mode": "idle",
    }


def test_mock_envelope_passes_through_ingest_path():
    # Build twice; outputs equal (deterministic and the ingest path was
    # exercised both times rather than bypassed).
    first = build_mock_hil_telemetry_envelope(captured_at=CAPTURED_AT)
    second = build_mock_hil_telemetry_envelope(captured_at=CAPTURED_AT)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_mock_envelope_rejects_command_like_metadata():
    with pytest.raises(HilTelemetryRejected) as exc_info:
        build_mock_hil_telemetry_envelope(
            captured_at=CAPTURED_AT,
            metadata={"source": "mock", "command": "land"},
        )
    assert "metadata.command" in str(exc_info.value)


def test_mock_envelope_rejects_dict_value_inside_measurements():
    # measurements is typed dict[str, float|int|bool|str]; Pydantic refuses
    # dict-valued entries via extra=forbid + scalar-only typing. The mock
    # surfaces this through the same ingest path.
    with pytest.raises(HilTelemetryRejected):
        build_mock_hil_telemetry_envelope(
            captured_at=CAPTURED_AT,
            measurements={"command": {"type": "land"}},  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# chain builder
# ---------------------------------------------------------------------------


def test_mock_chain_builds_envelope_evidence_and_review():
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
    )

    assert set(chain.keys()) == {
        "hil_telemetry_contract",
        "hil_telemetry_envelope",
        "hil_telemetry_evidence",
        "hil_telemetry_review",
    }
    assert (
        chain["hil_telemetry_evidence"].schema_version
        == HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION
    )
    assert (
        chain["hil_telemetry_evidence"].status is HilTelemetryEvidenceStatus.FRESH
    )
    assert (
        chain["hil_telemetry_review"].schema_version
        == HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    )
    assert chain["hil_telemetry_review"].passed is True


def test_mock_chain_stale_telemetry_blocks_review():
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT,
        now=LATE_NOW,
        freshness_threshold_seconds=60.0,
    )

    assert (
        chain["hil_telemetry_evidence"].status is HilTelemetryEvidenceStatus.STALE
    )
    review = chain["hil_telemetry_review"]
    assert review.passed is False
    assert review.status is HilTelemetryReviewStatus.BLOCKED
    assert HIL_REVIEW_BUCKET_STALE in review.blocked_reasons


def test_mock_chain_propagates_command_payload_rejection_count():
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
        rejected_command_like_payload_count=2,
    )

    review = chain["hil_telemetry_review"]
    assert review.passed is False
    assert HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED in review.blocked_reasons
    assert review.rejected_command_like_payload_count == 2


def test_mock_chain_required_review_with_evidence_does_not_emit_missing():
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
        required_review=True,
    )

    review = chain["hil_telemetry_review"]
    assert review.required is True
    assert review.passed is True


def test_mock_chain_is_deterministic_for_same_inputs():
    first = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT, now=FRESH_NOW
    )
    second = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT, now=FRESH_NOW
    )

    assert (
        first["hil_telemetry_envelope"].model_dump(mode="json")
        == second["hil_telemetry_envelope"].model_dump(mode="json")
    )
    assert (
        first["hil_telemetry_evidence"].evidence_id
        == second["hil_telemetry_evidence"].evidence_id
    )
    assert (
        first["hil_telemetry_review"].review_id
        == second["hil_telemetry_review"].review_id
    )


def test_mock_chain_rejects_command_like_metadata_before_returning():
    with pytest.raises(HilTelemetryRejected):
        build_mock_hil_telemetry_chain(
            captured_at=CAPTURED_AT,
            now=FRESH_NOW,
            metadata={"command": "land"},
        )


# ---------------------------------------------------------------------------
# task.artifacts attach
# ---------------------------------------------------------------------------


def _create_task(kind: str = "mock-hil-telemetry", title: str = "mock hil chain"):
    return get_task_store().create(kind=kind, title=title, status="running")


def test_attach_mock_chain_writes_four_keys_to_task_artifacts():
    task = _create_task()

    chain = attach_mock_hil_telemetry_chain(
        task["task_id"],
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
    )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    assert "hil_telemetry_contract" in artifacts
    assert "hil_telemetry_envelope" in artifacts
    assert "hil_telemetry_evidence" in artifacts
    assert "hil_telemetry_review" in artifacts
    assert (
        artifacts["hil_telemetry_review"]["review_id"]
        == chain["hil_telemetry_review"].review_id
    )


def test_attach_mock_chain_preserves_existing_artifacts():
    store = get_task_store()
    task = store.create(
        kind="mock-hil-telemetry",
        title="merge",
        status="running",
        artifacts={"existing_key": {"value": 42}},
    )

    attach_mock_hil_telemetry_chain(
        task["task_id"],
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    assert stored["artifacts"]["existing_key"] == {"value": 42}
    assert "hil_telemetry_envelope" in stored["artifacts"]


def test_attach_mock_chain_does_not_change_task_status():
    task = _create_task()
    initial_status = task["status"]

    attach_mock_hil_telemetry_chain(
        task["task_id"],
        captured_at=CAPTURED_AT,
        now=FRESH_NOW,
    )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == initial_status


def test_attach_mock_chain_raises_when_task_does_not_exist():
    with pytest.raises(MockHilTelemetryAttachError):
        attach_mock_hil_telemetry_chain(
            "task_does_not_exist",
            captured_at=CAPTURED_AT,
            now=FRESH_NOW,
        )


def test_attach_mock_chain_does_not_pollute_task_artifacts_on_rejection():
    task = _create_task()
    initial_artifacts_keys = set((task["artifacts"] or {}).keys())

    with pytest.raises(HilTelemetryRejected):
        attach_mock_hil_telemetry_chain(
            task["task_id"],
            captured_at=CAPTURED_AT,
            now=FRESH_NOW,
            metadata={"command": "land"},
        )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    # No HIL artifacts ended up on the task: the chain raised BEFORE the
    # store was touched.
    assert "hil_telemetry_contract" not in stored["artifacts"]
    assert "hil_telemetry_envelope" not in stored["artifacts"]
    assert "hil_telemetry_evidence" not in stored["artifacts"]
    assert "hil_telemetry_review" not in stored["artifacts"]
    assert set(stored["artifacts"].keys()) == initial_artifacts_keys


# ---------------------------------------------------------------------------
# end-to-end with autonomy gate
# ---------------------------------------------------------------------------


def _toy_grid_episode():
    state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="mock-hil-e2e",
    )
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=FRESH_NOW)
    contract = build_mission_contract(
        contract_id="mock-hil-e2e",
        objective="Reach the toy-grid goal using dry-run simulation only.",
        allowed_actions=[item.value for item in ToyGridWorldAction],
        forbidden_actions=[
            "live_actuator_execution",
            "direct_motor_control",
            "ros_dispatch",
            "enter_obstacle",
            "enter_hazard",
        ],
        completion_criteria=["agent_position_equals_goal"],
        evidence_requirements=[
            "telemetry_health_snapshot",
            "safety_governor_decision",
            "dry_run_action_envelope",
            "offline_replay_plan",
            "autonomy_scorecard",
            "autonomy_gate_result",
        ],
    )
    return run_toy_grid_world_autonomous_episode(
        state, plan, mission_contract=contract, now=FRESH_NOW
    )


def test_e2e_mock_hil_chain_passes_autonomy_gate_when_fresh():
    episode = _toy_grid_episode()
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT, now=FRESH_NOW
    )

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        hil_telemetry_reviews=[chain["hil_telemetry_review"]],
        required_hil_telemetry_review=True,
        now=FRESH_NOW,
    )

    assert gate.passed is True
    assert gate.hil_telemetry_review_refs == [
        f"hil_telemetry_review:{chain['hil_telemetry_review'].review_id}"
    ]
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.stronger_execution_allowed is False


def test_e2e_mock_hil_chain_blocks_autonomy_gate_when_stale():
    episode = _toy_grid_episode()
    chain = build_mock_hil_telemetry_chain(
        captured_at=CAPTURED_AT, now=LATE_NOW, freshness_threshold_seconds=60.0
    )

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        hil_telemetry_reviews=[chain["hil_telemetry_review"]],
        required_hil_telemetry_review=True,
        now=LATE_NOW,
    )

    assert gate.passed is False
    assert HIL_REVIEW_BUCKET_STALE in gate.blocked_reasons


def test_e2e_required_hil_review_missing_blocks_autonomy_gate():
    episode = _toy_grid_episode()

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        required_hil_telemetry_review=True,
        now=FRESH_NOW,
    )

    assert gate.passed is False
    assert "required_hil_telemetry_review_missing" in gate.blocked_reasons
