"""Tests for simulator_adapter_contract.v1 (#171 first slice).

This is the design / schema slice: only the contract artifact and the
toy-grid declaration. No runtime is routed through the contract here.
The tests pin both:

- the **shape** of the contract (schema versions match what toy-grid actually
  emits today)
- the **invariants** the type system must enforce (live / physical / ROS
  dispatch must not be advertisable through this slice)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.runtime.physical_mission_replay import (
    SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION,
    TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION,
)
from src.runtime.simulator_adapter_contract import (
    SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION,
    SimulatorAdapterContract,
    SimulatorAdapterMode,
)
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_ACTION_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
    TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION,
    TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID,
    TOY_GRID_WORLD_SIMULATOR_KIND,
    TOY_GRID_WORLD_STATE_SCHEMA_VERSION,
    build_toy_grid_world_simulator_adapter_contract,
)


def test_contract_schema_version_is_v1():
    assert SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION == "simulator_adapter_contract.v1"


def test_toy_grid_contract_is_valid_and_carries_expected_identity():
    contract = build_toy_grid_world_simulator_adapter_contract()

    assert isinstance(contract, SimulatorAdapterContract)
    assert contract.schema_version == SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION
    assert contract.adapter_id == TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID == "toy_grid_world.v1"
    assert contract.simulator_kind == TOY_GRID_WORLD_SIMULATOR_KIND == "toy_grid_world"


def test_toy_grid_contract_references_expected_schema_versions():
    contract = build_toy_grid_world_simulator_adapter_contract()

    assert contract.state_schema == TOY_GRID_WORLD_STATE_SCHEMA_VERSION
    assert contract.action_schema == TOY_GRID_WORLD_ACTION_SCHEMA_VERSION
    assert contract.telemetry_schema == TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION
    assert contract.governor_schema == SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION
    assert contract.episode_schema == TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION
    assert contract.replay_trace_schema == TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION


def test_toy_grid_contract_disallows_stronger_execution_modes():
    contract = build_toy_grid_world_simulator_adapter_contract()

    assert contract.supports_live_execution is False
    assert contract.supports_physical_execution is False
    assert contract.supports_ros_dispatch is False


def test_toy_grid_contract_requires_operator_approval():
    contract = build_toy_grid_world_simulator_adapter_contract()

    assert contract.operator_approval_required is True


def test_toy_grid_contract_advertises_dry_run_only_mode():
    contract = build_toy_grid_world_simulator_adapter_contract()

    assert contract.adapter_mode is SimulatorAdapterMode.DRY_RUN_ONLY
    assert contract.adapter_mode.value == "dry_run_only"


def test_contract_payload_shape_matches_design():
    payload = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")

    assert payload == {
        "schema_version": "simulator_adapter_contract.v1",
        "adapter_id": "toy_grid_world.v1",
        "simulator_kind": "toy_grid_world",
        "state_schema": "toy_grid_world_state.v1",
        "action_schema": "toy_grid_world_action.v1",
        "telemetry_schema": "telemetry_health_snapshot.v1",
        "governor_schema": "safety_governor_decision.v1",
        "episode_schema": "autonomous_episode.v1",
        "replay_trace_schema": "toy_grid_world_replay_trace.v1",
        "supports_live_execution": False,
        "supports_physical_execution": False,
        "supports_ros_dispatch": False,
        "operator_approval_required": True,
        "adapter_mode": "dry_run_only",
    }


def test_toy_grid_contract_is_deterministic():
    first = build_toy_grid_world_simulator_adapter_contract()
    second = build_toy_grid_world_simulator_adapter_contract()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


@pytest.mark.parametrize(
    "field, illegal_value",
    [
        ("supports_live_execution", True),
        ("supports_physical_execution", True),
        ("supports_ros_dispatch", True),
    ],
)
def test_contract_rejects_stronger_execution_capabilities(
    field: str, illegal_value: bool
):
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base[field] = illegal_value

    with pytest.raises(ValidationError):
        SimulatorAdapterContract.model_validate(base)


def test_contract_rejects_operator_approval_required_false():
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base["operator_approval_required"] = False

    with pytest.raises(ValidationError):
        SimulatorAdapterContract.model_validate(base)


def test_contract_rejects_unknown_adapter_mode_for_v1_slice():
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base["adapter_mode"] = "limited_live_execution"

    with pytest.raises(ValidationError):
        SimulatorAdapterContract.model_validate(base)


def test_contract_rejects_extra_fields():
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base["enables_actuator_dispatch"] = True

    with pytest.raises(ValidationError):
        SimulatorAdapterContract.model_validate(base)


def test_contract_rejects_unknown_schema_version():
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base["schema_version"] = "simulator_adapter_contract.v2"

    with pytest.raises(ValidationError):
        SimulatorAdapterContract.model_validate(base)
