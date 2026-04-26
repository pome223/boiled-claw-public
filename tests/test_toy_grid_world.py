from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path

import pytest

from src.runtime.physical_mission_replay import SafetyGovernorStatus, TelemetryHealthStatus
import src.runtime.toy_grid_world as toy_grid_world
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION,
    TOY_GRID_WORLD_STATE_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldError,
    ToyGridWorldStatus,
    build_grid_world_telemetry_snapshot,
    build_toy_grid_world_state,
    render_toy_grid_world_svg,
    run_toy_grid_world_replay,
    step_toy_grid_world,
)


NOW = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)


def _basic_state():
    return build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="toy-world-test",
    )


def test_toy_grid_world_state_is_versioned_and_visualizable():
    state = _basic_state()
    svg = render_toy_grid_world_svg(state, tile_size=24)

    assert state.schema_version == TOY_GRID_WORLD_STATE_SCHEMA_VERSION
    assert state.metadata["visual_style"] == "original_retro_top_down_pixel"
    assert state.metadata["live_execution_allowed"] is False
    assert svg.startswith("<svg")
    assert "toy-world-test" in svg
    assert "pokemon" not in svg.lower()


def test_agent_and_goal_cannot_start_inside_obstacle_or_hazard():
    with pytest.raises(ToyGridWorldError, match="agent cannot start inside an obstacle"):
        build_toy_grid_world_state(
            width=3,
            height=3,
            agent_position=(1, 1),
            goal_position=(2, 2),
            obstacles=[(1, 1)],
        )

    with pytest.raises(ToyGridWorldError, match="agent cannot start inside a hazard"):
        build_toy_grid_world_state(
            width=3,
            height=3,
            agent_position=(1, 1),
            goal_position=(2, 2),
            hazards=[(1, 1)],
        )

    with pytest.raises(ToyGridWorldError, match="goal cannot be inside a hazard"):
        build_toy_grid_world_state(
            width=3,
            height=3,
            agent_position=(0, 0),
            goal_position=(2, 2),
            hazards=[(2, 2)],
        )


def test_reaching_goal_uses_dry_run_artifacts():
    state = _basic_state()

    first = step_toy_grid_world(state, ToyGridWorldAction.MOVE_RIGHT, now=NOW)
    second = step_toy_grid_world(first.next_state, ToyGridWorldAction.MOVE_RIGHT, now=NOW)

    assert first.accepted is True
    assert first.dry_run_action_envelope is not None
    assert first.dry_run_action_envelope.dry_run is True
    assert first.dry_run_action_envelope.live_execution_allowed is False
    assert first.offline_replay_plan is not None
    assert first.offline_replay_plan.live_execution_allowed is False
    assert second.next_state.status == ToyGridWorldStatus.GOAL_REACHED
    assert second.next_state.agent_position.x == 2
    assert second.next_state.agent_position.y == 0


def test_blocked_by_obstacle_does_not_move_or_build_replay_plan():
    state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(3, 0),
        obstacles=[(0, 1)],
        world_id="obstacle-world",
    )
    step = step_toy_grid_world(state, ToyGridWorldAction.MOVE_DOWN, now=NOW)

    assert step.accepted is False
    assert step.blocked_reason == "obstacle"
    assert step.next_state.status == ToyGridWorldStatus.BLOCKED
    assert step.next_state.agent_position == state.agent_position
    assert step.dry_run_action_envelope is None
    assert step.offline_replay_plan is None
    assert step.safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED
    assert "blocked_by_obstacle" in step.safety_governor_decision.reasons


def test_blocked_by_hazard():
    state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(3, 0),
        hazards=[(1, 1)],
        world_id="hazard-world",
    )
    first = step_toy_grid_world(state, ToyGridWorldAction.MOVE_RIGHT, now=NOW)
    hazard = step_toy_grid_world(first.next_state, ToyGridWorldAction.MOVE_DOWN, now=NOW)

    assert first.accepted is True
    assert hazard.accepted is False
    assert hazard.blocked_reason == "hazard"
    assert "blocked_by_hazard" in hazard.safety_governor_decision.reasons


def test_blocked_by_low_battery_before_action():
    state = build_toy_grid_world_state(
        width=3,
        height=2,
        agent_position=(0, 0),
        goal_position=(2, 0),
        battery=20,
        low_battery_threshold=20,
        world_id="low-battery-world",
    )
    step = step_toy_grid_world(state, ToyGridWorldAction.MOVE_RIGHT, now=NOW)

    assert step.accepted is False
    assert step.telemetry_health_snapshot.status == TelemetryHealthStatus.UNSAFE
    assert step.safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED
    assert step.dry_run_action_envelope is None


def test_missing_telemetry_blocks_simulator_step():
    state = _basic_state()
    step = step_toy_grid_world(
        state,
        ToyGridWorldAction.MOVE_RIGHT,
        telemetry=None,
        now=NOW,
    )

    assert step.accepted is False
    assert step.telemetry_health_snapshot.status == TelemetryHealthStatus.MISSING
    assert step.safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED


def test_stale_telemetry_blocks_simulator_step():
    state = _basic_state()
    scenario_id = "scenario-stale"
    stale = build_grid_world_telemetry_snapshot(
        state,
        scenario_id=scenario_id,
        observed_at=NOW - timedelta(seconds=120),
        now=NOW,
    )
    step = step_toy_grid_world(
        state,
        ToyGridWorldAction.MOVE_RIGHT,
        telemetry=stale,
        now=NOW,
    )

    assert stale.status == TelemetryHealthStatus.STALE
    assert step.accepted is False
    assert step.safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED


def test_telemetry_scenario_mismatch_blocks_simulator_step():
    state = _basic_state()
    foreign_telemetry = build_grid_world_telemetry_snapshot(
        state,
        scenario_id="different-scenario",
        now=NOW,
    )

    step = step_toy_grid_world(
        state,
        ToyGridWorldAction.MOVE_RIGHT,
        telemetry=foreign_telemetry,
        now=NOW,
    )

    assert step.accepted is False
    assert step.safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED
    assert "telemetry_scenario_mismatch" in step.safety_governor_decision.reasons
    assert step.dry_run_action_envelope is None
    assert step.offline_replay_plan is None


def test_replay_is_deterministic():
    state = _basic_state()
    actions = [ToyGridWorldAction.MOVE_RIGHT, ToyGridWorldAction.MOVE_RIGHT]

    first = run_toy_grid_world_replay(state, actions, now=NOW)
    second = run_toy_grid_world_replay(state, actions, now=NOW)

    assert first.schema_version == TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION
    assert first.final_status == ToyGridWorldStatus.GOAL_REACHED
    assert first.deterministic_hash == second.deterministic_hash
    assert first.trace_id == second.trace_id
    assert first.live_execution_allowed is False
    assert first.physical_execution_invoked is False


def test_no_live_physical_execution_or_missions_api():
    source = inspect.getsource(toy_grid_world)

    assert "httpx" not in source
    assert "requests" not in source
    assert "subprocess" not in source
    assert "dispatch_ros2_action" not in source
    assert "actuator_command" not in source
    assert "/missions" not in source
    assert "CREATE TABLE missions" not in source
    assert not Path("src/gateway/mission_routes.py").exists()
