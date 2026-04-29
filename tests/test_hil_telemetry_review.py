"""Tests for hil_telemetry_review.v1.

This is the gate / scorecard input layer between
``hil_telemetry_evidence.v1`` and any future safety gate that consumes HIL
telemetry. Pin the aggregation rules, the bucket vocabulary, the
fail-closed behavior, and the type-level safety invariants.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryMode,
)
from src.runtime.hil_telemetry_evidence import (
    HilTelemetryEvidence,
    HilTelemetryEvidenceStatus,
    build_hil_telemetry_evidence,
)
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
    HIL_REVIEW_BUCKET_MALFORMED,
    HIL_REVIEW_BUCKET_MISSING,
    HIL_REVIEW_BUCKET_STALE,
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
    HilTelemetryReviewSeverity,
    HilTelemetryReviewStatus,
    build_hil_telemetry_review,
)


CAPTURED_AT = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
NOW = CAPTURED_AT + timedelta(seconds=10)
LATE_NOW = CAPTURED_AT + timedelta(seconds=120)


def _envelope_payload(**overrides):
    base = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": "hil-test.v1",
        "subject_kind": "test_subject",
        "subject_id": "subject-001",
        "captured_at": CAPTURED_AT.isoformat(),
        "measurements": {"battery": 78.2, "comms_ok": True},
        "metadata": {},
    }
    base.update(overrides)
    return base


def _contract():
    return HilTelemetryContract(
        contract_id="hil-test.v1",
        subject_kind="test_subject",
        telemetry_envelope_schema=HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        mode=HilTelemetryMode.TELEMETRY_ONLY,
    )


def _fresh_evidence(*, subject_id: str = "subject-001") -> HilTelemetryEvidence:
    return build_hil_telemetry_evidence(
        _envelope_payload(subject_id=subject_id),
        hil_telemetry_contract=_contract(),
        freshness_threshold_seconds=60.0,
        now=NOW,
    )


def _stale_evidence(*, subject_id: str = "subject-001") -> HilTelemetryEvidence:
    return build_hil_telemetry_evidence(
        _envelope_payload(subject_id=subject_id),
        hil_telemetry_contract=_contract(),
        freshness_threshold_seconds=60.0,
        now=LATE_NOW,
    )


def _empty_measurements_evidence() -> HilTelemetryEvidence:
    return build_hil_telemetry_evidence(
        _envelope_payload(measurements={}),
        hil_telemetry_contract=_contract(),
        freshness_threshold_seconds=60.0,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# schema invariants
# ---------------------------------------------------------------------------


def test_review_schema_version_is_v1():
    assert HIL_TELEMETRY_REVIEW_SCHEMA_VERSION == "hil_telemetry_review.v1"


def test_empty_input_passes_when_not_required():
    review = build_hil_telemetry_review(now=NOW)

    assert review.schema_version == HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    assert review.status is HilTelemetryReviewStatus.PASSED
    assert review.passed is True
    assert review.blocked_reasons == ()
    assert review.findings == ()
    assert review.required is False
    assert review.contract_schema_version == HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION


def test_review_pins_safety_invariants_at_type_level():
    review = build_hil_telemetry_review(now=NOW)
    assert review.operator_approval_required is True
    assert review.operator_approval_performed is False
    assert review.live_execution_allowed is False
    assert review.physical_execution_invoked is False
    assert review.command_payload_allowed is False
    assert review.metadata["rule_based"] is True
    assert review.metadata["llm_judge_used"] is False


@pytest.mark.parametrize(
    "field, illegal_value",
    [
        ("operator_approval_required", False),
        ("live_execution_allowed", True),
        ("physical_execution_invoked", True),
        ("command_payload_allowed", True),
        ("operator_approval_performed", True),
    ],
)
def test_review_rejects_weakened_safety_invariants(field, illegal_value):
    base = build_hil_telemetry_review(now=NOW).model_dump(mode="json")
    base[field] = illegal_value

    with pytest.raises(ValidationError):
        HilTelemetryReview.model_validate(base)


def test_review_rejects_extra_fields():
    base = build_hil_telemetry_review(now=NOW).model_dump(mode="json")
    base["enables_actuator_dispatch"] = True

    with pytest.raises(ValidationError):
        HilTelemetryReview.model_validate(base)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_fresh_evidence_passes_review():
    evidence = _fresh_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence], now=NOW
    )

    assert review.passed is True
    assert review.blocked_reasons == ()
    assert review.findings == ()
    assert review.evidence_ids == (evidence.evidence_id,)
    assert review.envelope_ids == (evidence.envelope_id,)
    assert review.contract_ids == (evidence.contract_id,)
    assert review.measurement_keys == ("battery", "comms_ok")
    assert review.freshness_threshold_seconds == 60.0
    assert review.freshness_seconds_max == round(evidence.freshness_seconds, 6)


def test_review_accepts_dict_evidence_payload():
    evidence = _fresh_evidence()
    payload = evidence.model_dump(mode="json")

    review = build_hil_telemetry_review(
        telemetry_evidences=[payload], now=NOW
    )

    assert review.passed is True
    assert review.evidence_ids == (evidence.evidence_id,)


def test_review_aggregates_measurement_keys_and_max_freshness_across_evidences():
    fresh = _fresh_evidence(subject_id="a")
    stale = _stale_evidence(subject_id="b")

    review = build_hil_telemetry_review(
        telemetry_evidences=[fresh, stale], now=LATE_NOW
    )

    assert review.measurement_keys == ("battery", "comms_ok")
    assert review.freshness_seconds_max == round(stale.freshness_seconds, 6)
    # blocked because of stale, not because of measurement aggregation
    assert HIL_REVIEW_BUCKET_STALE in review.blocked_reasons


# ---------------------------------------------------------------------------
# block paths
# ---------------------------------------------------------------------------


def test_required_with_no_evidence_emits_missing_finding():
    review = build_hil_telemetry_review(required=True, now=NOW)

    assert review.passed is False
    assert review.status is HilTelemetryReviewStatus.BLOCKED
    assert review.blocked_reasons == (HIL_REVIEW_BUCKET_MISSING,)
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.bucket == HIL_REVIEW_BUCKET_MISSING
    assert finding.severity is HilTelemetryReviewSeverity.BLOCKING
    assert finding.detail == {"required": True, "evidence_count": 0}


def test_stale_evidence_emits_stale_finding():
    evidence = _stale_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence], now=LATE_NOW
    )

    assert review.passed is False
    assert HIL_REVIEW_BUCKET_STALE in review.blocked_reasons
    stale_findings = [
        finding
        for finding in review.findings
        if finding.bucket == HIL_REVIEW_BUCKET_STALE
    ]
    assert len(stale_findings) == 1
    assert (
        stale_findings[0].detail["freshness_seconds"]
        > stale_findings[0].detail["freshness_threshold_seconds"]
    )


def test_malformed_evidence_emits_malformed_finding():
    evidence = _empty_measurements_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence], now=NOW
    )

    assert review.passed is False
    assert HIL_REVIEW_BUCKET_MALFORMED in review.blocked_reasons


def test_rejected_command_like_payload_emits_rejected_finding():
    evidence = _fresh_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence],
        rejected_command_like_payload_count=3,
        now=NOW,
    )

    assert review.passed is False
    assert (
        HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED in review.blocked_reasons
    )
    rejected_findings = [
        finding
        for finding in review.findings
        if finding.bucket == HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED
    ]
    assert rejected_findings[0].detail["rejected_count"] == 3


def test_blocked_reasons_are_sorted_and_deduped():
    stale_a = _stale_evidence(subject_id="a")
    stale_b = _stale_evidence(subject_id="b")
    malformed = _empty_measurements_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[stale_a, stale_b, malformed],
        rejected_command_like_payload_count=1,
        required=True,
        now=LATE_NOW,
    )

    # Three buckets: stale (twice -> deduped), malformed, command_payload_rejected.
    # Note: required=True with non-empty evidence list does NOT add missing.
    assert review.blocked_reasons == tuple(
        sorted(
            {
                HIL_REVIEW_BUCKET_STALE,
                HIL_REVIEW_BUCKET_MALFORMED,
                HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
            }
        )
    )


def test_required_with_evidence_does_not_emit_missing():
    evidence = _fresh_evidence()

    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence], required=True, now=NOW
    )

    assert review.passed is True
    assert HIL_REVIEW_BUCKET_MISSING not in review.blocked_reasons


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_review_id_is_deterministic_for_equal_inputs():
    fresh_a = _fresh_evidence()
    fresh_b = _fresh_evidence()

    first = build_hil_telemetry_review(
        telemetry_evidences=[fresh_a], now=NOW
    )
    second = build_hil_telemetry_review(
        telemetry_evidences=[fresh_b], now=NOW
    )

    assert first.review_id == second.review_id
    assert first.blocked_reasons == second.blocked_reasons
    assert first.findings == second.findings


def test_review_id_changes_when_blocked_reasons_change():
    fresh = _fresh_evidence()
    stale = _stale_evidence()

    passed_review = build_hil_telemetry_review(
        telemetry_evidences=[fresh], now=NOW
    )
    blocked_review = build_hil_telemetry_review(
        telemetry_evidences=[stale], now=LATE_NOW
    )

    assert passed_review.review_id != blocked_review.review_id


# ---------------------------------------------------------------------------
# defense in depth: dict input cannot smuggle stronger flags
# ---------------------------------------------------------------------------


def test_review_rejects_dict_evidence_with_weakened_invariant():
    payload = _fresh_evidence().model_dump(mode="json")
    payload["live_execution_allowed"] = True

    with pytest.raises(ValidationError):
        # HilTelemetryEvidence pins live_execution_allowed=Literal[False],
        # so re-validation refuses the smuggled flag before it can ever
        # reach the review aggregation.
        build_hil_telemetry_review(
            telemetry_evidences=[payload], now=NOW
        )


def test_review_does_not_lose_command_payload_rejection_on_passing_evidence():
    # Even when every evidence is fresh and well-formed, a non-zero
    # rejection count must still block the review. This is the boundary
    # the user spec requires: action / command / dispatch payloads were
    # observed and refused at ingestion, so the gate cannot be allowed
    # to pass on telemetry alone.
    fresh = _fresh_evidence()
    review = build_hil_telemetry_review(
        telemetry_evidences=[fresh],
        rejected_command_like_payload_count=1,
        now=NOW,
    )

    assert review.passed is False
    assert HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED in review.blocked_reasons


def test_review_metadata_carries_no_external_hardware_connection_flag():
    review = build_hil_telemetry_review(now=NOW)
    assert review.metadata["no_external_hardware_connection"] is True
    assert review.metadata["telemetry_only"] is True
    assert review.metadata["read_only"] is True
