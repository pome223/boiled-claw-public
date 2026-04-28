from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryRejected,
)
from src.runtime.hil_telemetry_evidence import (
    HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION,
    HilTelemetryEvidenceError,
    HilTelemetryEvidenceStatus,
    attach_hil_telemetry_artifacts,
    build_hil_telemetry_evidence,
)
from src.runtime.task_store import get_task_store


NOW = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)


def _contract(**overrides):
    payload = {
        "schema_version": HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
        "contract_id": "hil-contract-test",
        "subject_kind": "drone",
        "telemetry_envelope_schema": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "supports_action_dispatch": False,
        "supports_command_payload": False,
        "supports_live_execution": False,
        "supports_physical_execution": False,
        "supports_ros_dispatch": False,
        "operator_approval_required": True,
        "mode": "telemetry_only",
    }
    payload.update(overrides)
    return payload


def _envelope(**overrides):
    payload = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": "hil-contract-test",
        "subject_kind": "drone",
        "subject_id": "drone-001",
        "captured_at": NOW.isoformat(),
        "measurements": {
            "battery": 88.0,
            "gps_ok": True,
            "mode": "telemetry_only",
        },
        "metadata": {"source": "hil-fixture"},
    }
    payload.update(overrides)
    return payload


def _task(status: str = "running"):
    return get_task_store().create(
        kind="hil_telemetry",
        title="HIL telemetry evidence test",
        status=status,
        artifacts={"existing": {"kept": True}},
    )


def test_build_hil_telemetry_evidence_from_valid_envelope():
    evidence = build_hil_telemetry_evidence(
        _envelope(),
        hil_telemetry_contract=_contract(),
        now=NOW,
    )

    assert evidence.schema_version == HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION
    assert evidence.status == HilTelemetryEvidenceStatus.FRESH
    assert evidence.contract_id == "hil-contract-test"
    assert evidence.subject_kind == "drone"
    assert evidence.subject_id == "drone-001"
    assert evidence.freshness_seconds == 0.0
    assert evidence.rejected_command_like_payload_count == 0
    assert evidence.measurement_keys == ["battery", "gps_ok", "mode"]
    assert evidence.gate_findings == []
    assert evidence.review_findings == []
    assert evidence.read_only is True
    assert evidence.operator_approval_required is True
    assert evidence.operator_approval_performed is False
    assert evidence.supports_action_dispatch is False
    assert evidence.supports_command_payload is False
    assert evidence.supports_live_execution is False
    assert evidence.supports_physical_execution is False
    assert evidence.supports_ros_dispatch is False
    assert evidence.action_envelope_created is False
    assert evidence.command_payload_created is False
    assert evidence.promotion_created is False
    assert evidence.runtime_reuse_created is False
    assert evidence.live_execution_allowed is False
    assert evidence.physical_execution_invoked is False


def test_stale_hil_telemetry_records_gate_and_review_findings():
    evidence = build_hil_telemetry_evidence(
        _envelope(captured_at=(NOW - timedelta(seconds=120)).isoformat()),
        hil_telemetry_contract=_contract(),
        freshness_threshold_seconds=60,
        now=NOW,
    )

    assert evidence.status == HilTelemetryEvidenceStatus.STALE
    assert evidence.freshness_seconds == 120.0
    assert evidence.gate_findings == [
        {
            "bucket": "hil_telemetry_stale",
            "reason": "freshness_threshold_exceeded",
            "freshness_seconds": 120.0,
            "freshness_threshold_seconds": 60.0,
        }
    ]
    assert evidence.review_findings == evidence.gate_findings


def test_contract_mismatch_fails_before_evidence_creation():
    with pytest.raises(HilTelemetryEvidenceError, match="contract_id mismatch"):
        build_hil_telemetry_evidence(
            _envelope(),
            hil_telemetry_contract=_contract(contract_id="other-contract"),
            now=NOW,
        )


def test_attach_hil_telemetry_artifacts_persists_read_only_evidence():
    task = _task(status="running")

    artifacts = attach_hil_telemetry_artifacts(
        task["task_id"],
        _envelope(),
        hil_telemetry_contract=_contract(),
        now=NOW,
    )

    assert set(artifacts) == {
        "hil_telemetry_contract",
        "hil_telemetry_envelope",
        "hil_telemetry_evidence",
    }
    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert stored["artifacts"]["hil_telemetry_contract"]["schema_version"] == (
        HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    )
    assert stored["artifacts"]["hil_telemetry_envelope"]["schema_version"] == (
        HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    )
    evidence = stored["artifacts"]["hil_telemetry_evidence"]
    assert evidence["schema_version"] == HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION
    assert evidence["read_only"] is True
    assert evidence["action_envelope_created"] is False
    assert evidence["command_payload_created"] is False
    assert evidence["promotion_created"] is False
    assert evidence["runtime_reuse_created"] is False
    assert evidence["live_execution_allowed"] is False
    assert evidence["physical_execution_invoked"] is False


def test_hil_telemetry_evidence_artifact_chain_e2e_smoke():
    task = _task(status="running")

    attached = attach_hil_telemetry_artifacts(
        task["task_id"],
        _envelope(captured_at=(NOW - timedelta(seconds=90)).isoformat()),
        hil_telemetry_contract=_contract(),
        freshness_threshold_seconds=60,
        now=NOW,
    )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert attached["hil_telemetry_contract"]["schema_version"] == (
        HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    )
    assert attached["hil_telemetry_envelope"]["schema_version"] == (
        HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    )
    evidence = stored["artifacts"]["hil_telemetry_evidence"]
    assert evidence["schema_version"] == HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION
    assert evidence["status"] == "stale"
    assert evidence["gate_findings"][0]["bucket"] == "hil_telemetry_stale"
    assert evidence["review_findings"] == evidence["gate_findings"]
    assert evidence["supports_command_payload"] is False
    assert evidence["supports_ros_dispatch"] is False
    assert evidence["supports_live_execution"] is False
    assert evidence["supports_physical_execution"] is False
    assert evidence["action_envelope_created"] is False
    assert evidence["command_payload_created"] is False


def test_command_like_payload_is_rejected_and_not_saved():
    task = _task(status="running")

    with pytest.raises(HilTelemetryRejected):
        attach_hil_telemetry_artifacts(
            task["task_id"],
            _envelope(metadata={"RosTopic": "/cmd_vel"}),
            hil_telemetry_contract=_contract(),
            now=NOW,
        )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "hil_telemetry_envelope" not in stored["artifacts"]
    assert "hil_telemetry_evidence" not in stored["artifacts"]


def test_malformed_payload_is_rejected_and_not_saved():
    task = _task(status="running")
    payload = _envelope()
    payload["measurements"] = {"nested": {"battery": 88}}

    with pytest.raises(HilTelemetryRejected):
        attach_hil_telemetry_artifacts(
            task["task_id"],
            payload,
            hil_telemetry_contract=_contract(),
            now=NOW,
        )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "hil_telemetry_envelope" not in stored["artifacts"]
    assert "hil_telemetry_evidence" not in stored["artifacts"]


def test_attach_does_not_create_approval_promotion_or_runtime_reuse():
    task = _task(status="accepted")

    attach_hil_telemetry_artifacts(
        task["task_id"],
        _envelope(),
        hil_telemetry_contract=HilTelemetryContract.model_validate(_contract()),
        now=NOW,
    )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == "accepted"
    assert stored["approval_dependencies"] == []
    artifacts = stored["artifacts"]
    assert "approval" not in artifacts
    assert "promotion_package" not in artifacts
    assert "reuse_plan" not in artifacts
    evidence = artifacts["hil_telemetry_evidence"]
    assert evidence["operator_approval_required"] is True
    assert evidence["operator_approval_performed"] is False
    assert evidence["promotion_created"] is False
    assert evidence["runtime_reuse_created"] is False


def test_attach_raises_when_task_does_not_exist():
    with pytest.raises(HilTelemetryEvidenceError):
        attach_hil_telemetry_artifacts(
            "task_does_not_exist",
            _envelope(),
            hil_telemetry_contract=_contract(),
            now=NOW,
        )
