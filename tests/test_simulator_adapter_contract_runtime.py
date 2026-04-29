"""Tests for the toy-grid runtime consulting simulator_adapter_contract.v1.

Pin both the happy path (default contract flows through episode + gate, with
adapter provenance ending up in artifact metadata) and the fail-closed path
(any mismatched / weakened contract refuses to run).

This is the second slice of #171: runtime path consults the contract. Live /
physical / ROS / HIL paths remain firmly out of scope and are guarded by
type-level Pydantic invariants on ``SimulatorAdapterContract``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.mission_contract import build_mission_contract
from src.runtime.simulator_adapter_contract import (
    SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION,
    SimulatorAdapterContract,
    SimulatorAdapterContractError,
    SimulatorAdapterMode,
)
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_ACTION_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
    TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION,
    TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID,
    TOY_GRID_WORLD_SIMULATOR_KIND,
    TOY_GRID_WORLD_STATE_SCHEMA_VERSION,
    ToyGridWorldAction,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_safety_regression_gate,
    build_toy_grid_world_simulator_adapter_contract,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
    validate_toy_grid_world_simulator_adapter_contract,
)


NOW = datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc)


def _basic_state():
    return build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="adapter-contract-runtime-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-adapter-contract-runtime",
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


def _run_episode(simulator_adapter_contract=None):
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    return run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        simulator_adapter_contract=simulator_adapter_contract,
        now=NOW,
    )


def _expected_metadata_keys() -> set[str]:
    return {
        "adapter_contract_id",
        "adapter_contract_schema_version",
        "adapter_contract_simulator_kind",
        "adapter_contract_mode",
    }


# ---------------------------------------------------------------------------
# validator
# ---------------------------------------------------------------------------


def test_validator_returns_canonical_contract_when_unset():
    contract = validate_toy_grid_world_simulator_adapter_contract(None)
    assert contract == build_toy_grid_world_simulator_adapter_contract()


def test_validator_passes_canonical_contract_through():
    canonical = build_toy_grid_world_simulator_adapter_contract()
    assert validate_toy_grid_world_simulator_adapter_contract(canonical) is canonical


def test_validator_accepts_dict_payload():
    payload = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    contract = validate_toy_grid_world_simulator_adapter_contract(payload)
    assert contract.adapter_id == TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID


def _payload_with(**overrides):
    base = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    base.update(overrides)
    return base


def test_validator_rejects_wrong_simulator_kind():
    payload = _payload_with(simulator_kind="another_simulator")
    with pytest.raises(SimulatorAdapterContractError):
        validate_toy_grid_world_simulator_adapter_contract(payload)


@pytest.mark.parametrize(
    "field, expected_value",
    [
        ("state_schema", TOY_GRID_WORLD_STATE_SCHEMA_VERSION),
        ("action_schema", TOY_GRID_WORLD_ACTION_SCHEMA_VERSION),
        ("episode_schema", TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION),
        ("replay_trace_schema", TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION),
    ],
)
def test_validator_rejects_wrong_schema_ref(field: str, expected_value: str):
    payload = _payload_with(**{field: f"definitely_not_{expected_value}"})
    with pytest.raises(SimulatorAdapterContractError):
        validate_toy_grid_world_simulator_adapter_contract(payload)


@pytest.mark.parametrize(
    "field",
    [
        "supports_live_execution",
        "supports_physical_execution",
        "supports_ros_dispatch",
    ],
)
def test_validator_rejects_capability_flag_set_true(field: str):
    payload = _payload_with(**{field: True})
    with pytest.raises(SimulatorAdapterContractError):
        validate_toy_grid_world_simulator_adapter_contract(payload)


def test_validator_rejects_operator_approval_required_false():
    payload = _payload_with(operator_approval_required=False)
    with pytest.raises(SimulatorAdapterContractError):
        validate_toy_grid_world_simulator_adapter_contract(payload)


def test_validator_rejects_unknown_adapter_mode():
    payload = _payload_with(adapter_mode="limited_live_execution")
    with pytest.raises(SimulatorAdapterContractError):
        validate_toy_grid_world_simulator_adapter_contract(payload)


# ---------------------------------------------------------------------------
# runner consults the contract
# ---------------------------------------------------------------------------


def test_episode_runner_passes_with_default_contract():
    episode = _run_episode()
    metadata = episode.metadata
    assert _expected_metadata_keys().issubset(metadata.keys())
    assert metadata["adapter_contract_id"] == TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID
    assert (
        metadata["adapter_contract_schema_version"]
        == SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION
    )
    assert metadata["adapter_contract_simulator_kind"] == TOY_GRID_WORLD_SIMULATOR_KIND
    assert metadata["adapter_contract_mode"] == SimulatorAdapterMode.DRY_RUN_ONLY.value
    # Defense in depth: even with a contract present the static safety
    # invariants on episode metadata stay intact.
    assert metadata["live_execution_allowed"] is False
    assert metadata["physical_execution_invoked"] is False


def test_episode_runner_passes_with_explicit_canonical_contract():
    canonical = build_toy_grid_world_simulator_adapter_contract()
    episode = _run_episode(simulator_adapter_contract=canonical)
    assert episode.metadata["adapter_contract_id"] == canonical.adapter_id


def test_episode_runner_fails_closed_on_wrong_simulator_kind():
    bad = _payload_with(simulator_kind="another_simulator")
    with pytest.raises(SimulatorAdapterContractError):
        _run_episode(simulator_adapter_contract=bad)


@pytest.mark.parametrize(
    "field, illegal_value",
    [
        ("supports_live_execution", True),
        ("supports_physical_execution", True),
        ("supports_ros_dispatch", True),
        ("operator_approval_required", False),
    ],
)
def test_episode_runner_fails_closed_on_weakened_contract(
    field: str, illegal_value: bool
):
    bad = _payload_with(**{field: illegal_value})
    with pytest.raises(SimulatorAdapterContractError):
        _run_episode(simulator_adapter_contract=bad)


def test_episode_runner_fails_closed_on_wrong_replay_schema_ref():
    bad = _payload_with(replay_trace_schema="some_other_replay_schema.v1")
    with pytest.raises(SimulatorAdapterContractError):
        _run_episode(simulator_adapter_contract=bad)


def test_episode_runner_fails_closed_on_wrong_adapter_mode():
    bad = _payload_with(adapter_mode="limited_live_execution")
    with pytest.raises(SimulatorAdapterContractError):
        _run_episode(simulator_adapter_contract=bad)


# ---------------------------------------------------------------------------
# safety regression gate consults the contract
# ---------------------------------------------------------------------------


def test_safety_regression_gate_passes_with_default_contract():
    episode = _run_episode()
    gate = build_toy_grid_world_autonomy_safety_regression_gate(episode, now=NOW)
    metadata = gate.metadata
    assert _expected_metadata_keys().issubset(metadata.keys())
    assert metadata["adapter_contract_id"] == TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID
    # Static safety invariants on the gate itself are unchanged by the new
    # adapter metadata path.
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.stronger_execution_allowed is False


def test_safety_regression_gate_fails_closed_on_weakened_contract():
    episode = _run_episode()
    bad = _payload_with(supports_live_execution=True)
    with pytest.raises(SimulatorAdapterContractError):
        build_toy_grid_world_autonomy_safety_regression_gate(
            episode,
            simulator_adapter_contract=bad,
            now=NOW,
        )


def test_safety_regression_gate_fails_closed_on_wrong_simulator_kind():
    episode = _run_episode()
    bad = _payload_with(simulator_kind="another_simulator")
    with pytest.raises(SimulatorAdapterContractError):
        build_toy_grid_world_autonomy_safety_regression_gate(
            episode,
            simulator_adapter_contract=bad,
            now=NOW,
        )


def test_safety_regression_gate_accepts_explicit_canonical_contract_dict():
    episode = _run_episode()
    payload = build_toy_grid_world_simulator_adapter_contract().model_dump(mode="json")
    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        simulator_adapter_contract=payload,
        now=NOW,
    )
    assert gate.metadata["adapter_contract_id"] == TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID
