"""Tests for hil_telemetry_contract.v1 (#172 first slice).

Pin both the contract / envelope shape and the fail-closed semantics of the
ingestion path. No runtime is routed through this contract here; this slice
is the read-only telemetry boundary only.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryEnvelope,
    HilTelemetryMode,
    HilTelemetryRejected,
    ingest_hil_telemetry_envelope,
)


CAPTURED_AT = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def _valid_envelope_payload(**overrides):
    base = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": "hil-test.v1",
        "subject_kind": "test_subject",
        "subject_id": "subject-001",
        "captured_at": CAPTURED_AT.isoformat(),
        "measurements": {"battery": 78.2, "comms_ok": True, "mode": "idle"},
        "metadata": {"source": "test", "calibration": "v1"},
    }
    base.update(overrides)
    return base


def _valid_contract_payload(**overrides):
    base = {
        "schema_version": HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
        "contract_id": "hil-test.v1",
        "subject_kind": "test_subject",
        "telemetry_envelope_schema": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "supports_action_dispatch": False,
        "supports_command_payload": False,
        "supports_live_execution": False,
        "supports_physical_execution": False,
        "supports_ros_dispatch": False,
        "operator_approval_required": True,
        "mode": HilTelemetryMode.TELEMETRY_ONLY.value,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# schema versions
# ---------------------------------------------------------------------------


def test_contract_schema_version_is_v1():
    assert HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION == "hil_telemetry_contract.v1"


def test_envelope_schema_version_is_v1():
    assert HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION == "hil_telemetry_envelope.v1"


# ---------------------------------------------------------------------------
# contract invariants
# ---------------------------------------------------------------------------


def test_contract_with_default_invariants_constructs_cleanly():
    contract = HilTelemetryContract.model_validate(_valid_contract_payload())

    assert contract.schema_version == HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    assert contract.supports_action_dispatch is False
    assert contract.supports_command_payload is False
    assert contract.supports_live_execution is False
    assert contract.supports_physical_execution is False
    assert contract.supports_ros_dispatch is False
    assert contract.operator_approval_required is True
    assert contract.mode is HilTelemetryMode.TELEMETRY_ONLY


@pytest.mark.parametrize(
    "field",
    [
        "supports_action_dispatch",
        "supports_command_payload",
        "supports_live_execution",
        "supports_physical_execution",
        "supports_ros_dispatch",
    ],
)
def test_contract_rejects_capability_flag_set_true(field: str):
    payload = _valid_contract_payload(**{field: True})

    with pytest.raises(Exception):
        HilTelemetryContract.model_validate(payload)


def test_contract_rejects_operator_approval_required_false():
    payload = _valid_contract_payload(operator_approval_required=False)

    with pytest.raises(Exception):
        HilTelemetryContract.model_validate(payload)


def test_contract_rejects_unknown_mode():
    payload = _valid_contract_payload(mode="bounded_write")

    with pytest.raises(Exception):
        HilTelemetryContract.model_validate(payload)


def test_contract_rejects_extra_field():
    payload = _valid_contract_payload(action_schema="something.v1")

    with pytest.raises(Exception):
        HilTelemetryContract.model_validate(payload)


def test_contract_rejects_unknown_schema_version():
    payload = _valid_contract_payload(schema_version="hil_telemetry_contract.v2")

    with pytest.raises(Exception):
        HilTelemetryContract.model_validate(payload)


# ---------------------------------------------------------------------------
# envelope shape
# ---------------------------------------------------------------------------


def test_envelope_accepts_valid_telemetry_only_payload():
    envelope = HilTelemetryEnvelope.model_validate(_valid_envelope_payload())

    assert envelope.schema_version == HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    assert envelope.subject_kind == "test_subject"
    assert envelope.measurements == {"battery": 78.2, "comms_ok": True, "mode": "idle"}


def test_envelope_rejects_extra_top_level_field():
    payload = _valid_envelope_payload(action="land")

    with pytest.raises(Exception):
        HilTelemetryEnvelope.model_validate(payload)


def test_envelope_rejects_dict_value_in_measurements():
    payload = _valid_envelope_payload(
        measurements={"command": {"type": "land"}}
    )

    with pytest.raises(Exception):
        HilTelemetryEnvelope.model_validate(payload)


def test_envelope_rejects_list_value_in_measurements():
    payload = _valid_envelope_payload(measurements={"path": [1, 2, 3]})

    with pytest.raises(Exception):
        HilTelemetryEnvelope.model_validate(payload)


# ---------------------------------------------------------------------------
# ingestion fail-closed semantics
# ---------------------------------------------------------------------------


def test_ingest_returns_envelope_unchanged_when_already_typed():
    envelope = HilTelemetryEnvelope.model_validate(_valid_envelope_payload())

    assert ingest_hil_telemetry_envelope(envelope) is envelope


def test_ingest_accepts_valid_dict_payload():
    payload = _valid_envelope_payload()

    envelope = ingest_hil_telemetry_envelope(payload)

    assert isinstance(envelope, HilTelemetryEnvelope)
    assert envelope.contract_id == "hil-test.v1"


def test_ingest_rejects_non_dict_non_envelope_payload():
    with pytest.raises(HilTelemetryRejected):
        ingest_hil_telemetry_envelope("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "action",
        "actions",
        "command",
        "commands",
        "actuator",
        "actuators",
        "dispatch",
        "ros_topic",
        "ros2_topic",
        "execute",
        "execute_now",
        "live_execution_allowed",
        "physical_execution_invoked",
    ],
)
def test_ingest_rejects_command_like_top_level_key(forbidden_key: str):
    payload = _valid_envelope_payload()
    payload[forbidden_key] = "anything"

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert forbidden_key in str(exc_info.value)


@pytest.mark.parametrize(
    "casing",
    [
        "Action",
        "COMMAND",
        "RosTopic",
        "Physical_Execution_Invoked",
        "PhysicalExecutionInvoked",
        "liveExecutionAllowed",
        "DISPATCH",
    ],
)
def test_ingest_rejects_command_like_key_case_insensitively(casing: str):
    payload = _valid_envelope_payload()
    payload[casing] = "anything"

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert casing in str(exc_info.value)


def test_ingest_rejects_command_like_key_in_metadata():
    payload = _valid_envelope_payload()
    payload["metadata"] = {"source": "test", "command": "land"}

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert "metadata.command" in str(exc_info.value)


@pytest.mark.parametrize(
    "key",
    [
        "RosTopic",
        "rosTopic",
        "ros-topic",
        "PhysicalExecutionInvoked",
        "liveExecutionAllowed",
    ],
)
def test_ingest_rejects_nested_command_like_key_variants_in_metadata(key: str):
    payload = _valid_envelope_payload()
    payload["metadata"] = {"source": "test", key: "cmd/land"}

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert f"metadata.{key}" in str(exc_info.value)


def test_ingest_rejects_command_like_key_in_measurements():
    payload = _valid_envelope_payload()
    payload["measurements"] = {"battery": 78.2, "dispatch": "now"}

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert "measurements.dispatch" in str(exc_info.value)


@pytest.mark.parametrize(
    "key",
    [
        "RosTopic",
        "rosTopic",
        "ros-topic",
        "PhysicalExecutionInvoked",
        "liveExecutionAllowed",
    ],
)
def test_ingest_rejects_nested_command_like_key_variants_in_measurements(key: str):
    payload = _valid_envelope_payload()
    payload["measurements"] = {"battery": 78.2, key: "cmd/land"}

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert f"measurements.{key}" in str(exc_info.value)


def test_ingest_rejects_command_like_key_inside_nested_list():
    # Defensive: even if a future schema lets a nested list slip through, a
    # command-like key inside it is still rejected by the recursive walk.
    payload = _valid_envelope_payload()
    payload["metadata"] = {
        "source": "test",
        "history": [{"action": "land", "ts": 1}],
    }

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert "action" in str(exc_info.value)


def test_ingest_rejects_command_like_key_variant_inside_nested_list():
    payload = _valid_envelope_payload()
    payload["metadata"] = {
        "source": "test",
        "history": [{"RosTopic": "/cmd_vel", "ts": 1}],
    }

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    assert "metadata.history.0.RosTopic" in str(exc_info.value)


def test_ingest_rejects_unknown_top_level_field_via_pydantic():
    payload = _valid_envelope_payload()
    payload["unknown_extra_field"] = 42

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    # Pydantic ValidationError is wrapped in HilTelemetryRejected
    assert "unknown_extra_field" in str(exc_info.value) or "extra" in str(exc_info.value).lower()


def test_ingest_lists_all_offending_command_like_keys():
    payload = _valid_envelope_payload()
    payload["action"] = "land"
    payload["metadata"] = {"command": "x"}
    payload["measurements"] = {"battery": 1.0, "dispatch": "y"}

    with pytest.raises(HilTelemetryRejected) as exc_info:
        ingest_hil_telemetry_envelope(payload)
    msg = str(exc_info.value)
    assert "action" in msg
    assert "metadata.command" in msg
    assert "measurements.dispatch" in msg


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_envelope_is_deterministic_for_same_input():
    payload = _valid_envelope_payload()

    first = ingest_hil_telemetry_envelope(payload).model_dump(mode="json")
    second = ingest_hil_telemetry_envelope(dict(payload)).model_dump(mode="json")

    assert first == second
