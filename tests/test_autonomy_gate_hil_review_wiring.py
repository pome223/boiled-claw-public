"""Tests for hil_telemetry_review.v1 → autonomy_gate_result.v1 wiring.

Pin the gate-level enforcement of HIL telemetry reviews:

- ``required_hil_telemetry_review_missing`` when required + zero reviews
- ``hil_telemetry_review_failed:<id>`` (generic) per blocked review
- specific buckets lifted: ``hil_telemetry_stale`` / ``hil_telemetry_missing`` /
  ``hil_telemetry_malformed`` / ``command_payload_rejected``
- gate result records ``hil_telemetry_review_refs`` and
  ``hil_telemetry_review_snapshots`` for audit / UI
- safety invariants stay pinned at the type level
- the high-level ``build_toy_grid_world_autonomy_safety_regression_gate``
  entry-point passes the HIL params through

Out of scope: scorecard wiring, UI, telemetry source adapters.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryMode,
)
from src.runtime.hil_telemetry_evidence import build_hil_telemetry_evidence
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
    HIL_REVIEW_BUCKET_MALFORMED,
    HIL_REVIEW_BUCKET_MISSING,
    HIL_REVIEW_BUCKET_STALE,
    build_hil_telemetry_review,
)
from tests.fixtures.toy_grid_autonomy_corpus import (
    CORPUS_NOW,
    build_golden_toy_grid_autonomy_cases,
)
from src.runtime.toy_grid_world import (
    ToyGridWorldAction,
    build_toy_grid_world_autonomy_gate_result,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_safety_regression_gate,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
)
from src.runtime.mission_contract import build_mission_contract


# ---------------------------------------------------------------------------
# clean baseline gate inputs (taken from the existing corpus)
# ---------------------------------------------------------------------------


def _clean_gate_inputs():
    cases = {case.case_id: case for case in build_golden_toy_grid_autonomy_cases()}
    case = cases["clean_goal_reached"]
    return case.artifacts


def _build_gate(**overrides):
    artifacts = _clean_gate_inputs()
    return build_toy_grid_world_autonomy_gate_result(
        artifacts["scorecard"],
        autonomy_episode_review=artifacts["review"],
        safety_eval_results=artifacts.get("safety_eval_results"),
        now=CORPUS_NOW,
        **overrides,
    )


# ---------------------------------------------------------------------------
# HIL review fixtures
# ---------------------------------------------------------------------------


HIL_CAPTURED_AT = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
HIL_FRESH_NOW = HIL_CAPTURED_AT + timedelta(seconds=10)
HIL_LATE_NOW = HIL_CAPTURED_AT + timedelta(seconds=120)


def _hil_contract():
    return HilTelemetryContract(
        contract_id="hil-test.v1",
        subject_kind="test_subject",
        telemetry_envelope_schema=HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        mode=HilTelemetryMode.TELEMETRY_ONLY,
    )


def _hil_envelope_payload(**overrides):
    base = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": "hil-test.v1",
        "subject_kind": "test_subject",
        "subject_id": "subject-001",
        "captured_at": HIL_CAPTURED_AT.isoformat(),
        "measurements": {"battery": 78.2, "comms_ok": True},
        "metadata": {},
    }
    base.update(overrides)
    return base


def _fresh_evidence():
    return build_hil_telemetry_evidence(
        _hil_envelope_payload(),
        hil_telemetry_contract=_hil_contract(),
        freshness_threshold_seconds=60.0,
        now=HIL_FRESH_NOW,
    )


def _stale_evidence():
    return build_hil_telemetry_evidence(
        _hil_envelope_payload(),
        hil_telemetry_contract=_hil_contract(),
        freshness_threshold_seconds=60.0,
        now=HIL_LATE_NOW,
    )


def _empty_measurements_evidence():
    return build_hil_telemetry_evidence(
        _hil_envelope_payload(measurements={}),
        hil_telemetry_contract=_hil_contract(),
        freshness_threshold_seconds=60.0,
        now=HIL_FRESH_NOW,
    )


def _passing_review():
    return build_hil_telemetry_review(
        telemetry_evidences=[_fresh_evidence()],
        now=HIL_FRESH_NOW,
    )


def _stale_review():
    return build_hil_telemetry_review(
        telemetry_evidences=[_stale_evidence()],
        now=HIL_LATE_NOW,
    )


def _malformed_review():
    return build_hil_telemetry_review(
        telemetry_evidences=[_empty_measurements_evidence()],
        now=HIL_FRESH_NOW,
    )


def _command_rejected_review():
    return build_hil_telemetry_review(
        telemetry_evidences=[_fresh_evidence()],
        rejected_command_like_payload_count=2,
        now=HIL_FRESH_NOW,
    )


def _missing_review():
    return build_hil_telemetry_review(required=True, now=HIL_FRESH_NOW)


# ---------------------------------------------------------------------------
# default behavior: no HIL params -> existing gate behavior unchanged
# ---------------------------------------------------------------------------


def test_clean_gate_without_hil_params_passes_unchanged():
    gate = _build_gate()

    assert gate.passed is True
    assert gate.blocked_reasons == []
    assert gate.hil_telemetry_review_refs == []
    assert gate.hil_telemetry_review_snapshots == []


def test_clean_gate_with_required_hil_review_but_no_reviews_blocks():
    gate = _build_gate(required_hil_telemetry_review=True)

    assert gate.passed is False
    assert "required_hil_telemetry_review_missing" in gate.blocked_reasons


def test_required_hil_review_with_passing_review_does_not_emit_missing():
    gate = _build_gate(
        hil_telemetry_reviews=[_passing_review()],
        required_hil_telemetry_review=True,
    )

    assert gate.passed is True
    assert (
        "required_hil_telemetry_review_missing" not in gate.blocked_reasons
    )


# ---------------------------------------------------------------------------
# specific bucket lift
# ---------------------------------------------------------------------------


def test_passing_hil_review_records_refs_and_snapshots_without_blocking():
    review = _passing_review()
    gate = _build_gate(hil_telemetry_reviews=[review])

    assert gate.passed is True
    assert gate.hil_telemetry_review_refs == [
        f"hil_telemetry_review:{review.review_id}"
    ]
    assert len(gate.hil_telemetry_review_snapshots) == 1
    assert (
        gate.hil_telemetry_review_snapshots[0]["review_id"] == review.review_id
    )


def test_stale_hil_review_blocks_with_specific_and_generic_reasons():
    review = _stale_review()
    gate = _build_gate(hil_telemetry_reviews=[review])

    assert gate.passed is False
    assert HIL_REVIEW_BUCKET_STALE in gate.blocked_reasons
    assert (
        f"hil_telemetry_review_failed:{review.review_id}"
        in gate.blocked_reasons
    )


def test_malformed_hil_review_blocks_with_specific_and_generic_reasons():
    review = _malformed_review()
    gate = _build_gate(hil_telemetry_reviews=[review])

    assert gate.passed is False
    assert HIL_REVIEW_BUCKET_MALFORMED in gate.blocked_reasons
    assert (
        f"hil_telemetry_review_failed:{review.review_id}"
        in gate.blocked_reasons
    )


def test_command_payload_rejected_hil_review_blocks_with_specific_reason():
    review = _command_rejected_review()
    gate = _build_gate(hil_telemetry_reviews=[review])

    assert gate.passed is False
    assert (
        HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED in gate.blocked_reasons
    )


def test_missing_evidence_review_blocks_with_specific_missing_reason():
    review = _missing_review()
    gate = _build_gate(hil_telemetry_reviews=[review])

    assert gate.passed is False
    assert HIL_REVIEW_BUCKET_MISSING in gate.blocked_reasons


# ---------------------------------------------------------------------------
# multi-review aggregation + dedupe
# ---------------------------------------------------------------------------


def test_multi_review_blocked_reasons_are_sorted_and_deduped():
    stale = _stale_review()
    malformed = _malformed_review()
    rejected = _command_rejected_review()

    gate = _build_gate(
        hil_telemetry_reviews=[stale, malformed, rejected],
    )

    # Specific buckets dedupe to single occurrences; generic per-review
    # failures are unique by review_id.
    assert HIL_REVIEW_BUCKET_STALE in gate.blocked_reasons
    assert HIL_REVIEW_BUCKET_MALFORMED in gate.blocked_reasons
    assert HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED in gate.blocked_reasons
    generic = [
        reason
        for reason in gate.blocked_reasons
        if reason.startswith("hil_telemetry_review_failed:")
    ]
    assert len(generic) == 3
    # Stable / sorted / deduped
    assert gate.blocked_reasons == sorted(gate.blocked_reasons)
    assert len(gate.blocked_reasons) == len(set(gate.blocked_reasons))


def test_multi_review_refs_and_snapshots_are_aggregated():
    stale = _stale_review()
    passing = _passing_review()

    gate = _build_gate(hil_telemetry_reviews=[stale, passing])

    assert {ref for ref in gate.hil_telemetry_review_refs} == {
        f"hil_telemetry_review:{stale.review_id}",
        f"hil_telemetry_review:{passing.review_id}",
    }
    assert len(gate.hil_telemetry_review_snapshots) == 2


# ---------------------------------------------------------------------------
# defense in depth: dict input cannot smuggle stronger HIL flags
# ---------------------------------------------------------------------------


def test_gate_rejects_dict_review_with_smuggled_live_execution_flag():
    payload = _passing_review().model_dump(mode="json")
    payload["live_execution_allowed"] = True

    with pytest.raises(ValidationError):
        _build_gate(hil_telemetry_reviews=[payload])


def test_gate_accepts_dict_passing_review():
    payload = _passing_review().model_dump(mode="json")
    gate = _build_gate(hil_telemetry_reviews=[payload])

    assert gate.passed is True
    assert len(gate.hil_telemetry_review_refs) == 1


# ---------------------------------------------------------------------------
# safety invariants on the gate are still pinned at the type level
# ---------------------------------------------------------------------------


def test_gate_safety_invariants_unchanged_when_hil_blocks():
    gate = _build_gate(hil_telemetry_reviews=[_stale_review()])

    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.stronger_execution_allowed is False
    assert gate.operator_approval_required is True
    assert gate.metadata["rule_based"] is True
    assert gate.metadata["llm_judge_used"] is False


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_gate_with_hil_reviews_is_deterministic():
    first = _build_gate(hil_telemetry_reviews=[_stale_review()])
    second = _build_gate(hil_telemetry_reviews=[_stale_review()])

    assert first.gate_id == second.gate_id
    assert first.blocked_reasons == second.blocked_reasons
    assert first.hil_telemetry_review_refs == second.hil_telemetry_review_refs


def test_gate_id_unchanged_when_no_hil_reviews_provided():
    # Adding the new HIL parameters with empty defaults must not perturb
    # gate IDs of pre-existing call sites that never pass HIL inputs.
    legacy_style = _build_gate()
    explicit_no_hil = _build_gate(hil_telemetry_reviews=[])

    assert legacy_style.gate_id == explicit_no_hil.gate_id


# ---------------------------------------------------------------------------
# safety regression entry-point passes through HIL params
# ---------------------------------------------------------------------------


def _safety_regression_episode():
    state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="hil-wiring-world",
    )
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=CORPUS_NOW)
    contract = build_mission_contract(
        contract_id="toy-grid-hil-wiring",
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
        state, plan, mission_contract=contract, now=CORPUS_NOW
    )


def test_safety_regression_gate_passes_hil_params_through():
    episode = _safety_regression_episode()

    blocked_gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        hil_telemetry_reviews=[_stale_review()],
        now=CORPUS_NOW,
    )
    assert blocked_gate.passed is False
    assert HIL_REVIEW_BUCKET_STALE in blocked_gate.blocked_reasons
    assert blocked_gate.hil_telemetry_review_refs

    required_missing_gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        required_hil_telemetry_review=True,
        now=CORPUS_NOW,
    )
    assert required_missing_gate.passed is False
    assert (
        "required_hil_telemetry_review_missing"
        in required_missing_gate.blocked_reasons
    )
