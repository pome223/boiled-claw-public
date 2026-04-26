from __future__ import annotations

from datetime import datetime, timezone
import inspect

import pytest
from pydantic import ValidationError

import src.runtime.toy_grid_world as toy_grid_world
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldAutonomyPlan,
    ToyGridWorldAutonomyPlanStatus,
    ToyGridWorldPosition,
    ToyGridWorldStatus,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_state,
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
        world_id="planner-basic-world",
    )


def _positions_after_actions(
    initial: ToyGridWorldPosition,
    actions: list[ToyGridWorldAction],
) -> list[tuple[int, int]]:
    current = (initial.x, initial.y)
    positions: list[tuple[int, int]] = []
    deltas = {
        ToyGridWorldAction.MOVE_UP: (0, -1),
        ToyGridWorldAction.MOVE_DOWN: (0, 1),
        ToyGridWorldAction.MOVE_LEFT: (-1, 0),
        ToyGridWorldAction.MOVE_RIGHT: (1, 0),
        ToyGridWorldAction.WAIT: (0, 0),
    }
    for action in actions:
        dx, dy = deltas[action]
        current = (current[0] + dx, current[1] + dy)
        positions.append(current)
    return positions


def test_autonomy_plan_to_goal_is_versioned_and_inert():
    state = _basic_state()

    plan = build_toy_grid_world_autonomy_plan(
        state,
        max_step_budget=5,
        now=NOW,
    )

    assert plan.schema_version == TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION
    assert plan.status == ToyGridWorldAutonomyPlanStatus.PLANNED
    assert plan.actions == [ToyGridWorldAction.MOVE_RIGHT, ToyGridWorldAction.MOVE_RIGHT]
    assert plan.predicted_final_position.x == 2
    assert plan.predicted_final_position.y == 0
    assert plan.predicted_status == ToyGridWorldStatus.GOAL_REACHED
    assert plan.execution_allowed is False
    assert plan.operator_approval_required is True
    assert plan.live_execution_allowed is False
    assert plan.physical_execution_invoked is False
    assert plan.metadata["plan_only"] is True
    assert plan.metadata["artifact_only"] is True
    assert "plan_only_no_simulator_step" in plan.constraints_used
    assert "plan_must_be_checked_by_safety_governor_before_execution" in plan.safety_assumptions


def test_autonomy_planner_avoids_obstacles_and_hazards():
    state = build_toy_grid_world_state(
        width=5,
        height=3,
        agent_position=(0, 1),
        goal_position=(4, 1),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="planner-avoid-world",
    )

    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=10, now=NOW)
    visited = set(_positions_after_actions(state.agent_position, plan.actions))

    assert plan.status == ToyGridWorldAutonomyPlanStatus.PLANNED
    assert (1, 1) not in visited
    assert (2, 1) not in visited
    assert (plan.predicted_final_position.x, plan.predicted_final_position.y) == (4, 1)


def test_autonomy_planner_blocks_when_no_safe_path_exists():
    state = build_toy_grid_world_state(
        width=3,
        height=3,
        agent_position=(1, 1),
        goal_position=(2, 2),
        obstacles=[(1, 0), (0, 1), (2, 1)],
        hazards=[(1, 2)],
        world_id="planner-no-path-world",
    )

    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=10, now=NOW)

    assert plan.status == ToyGridWorldAutonomyPlanStatus.BLOCKED
    assert plan.failure_reason == "no_safe_path"
    assert plan.actions == []
    assert plan.predicted_final_position == state.agent_position
    assert plan.execution_allowed is False


def test_autonomy_planner_blocks_when_battery_budget_is_too_low():
    state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        battery=21,
        low_battery_threshold=20,
        world_id="planner-low-battery-world",
    )

    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    assert plan.status == ToyGridWorldAutonomyPlanStatus.BLOCKED
    assert plan.failure_reason == "low_battery"
    assert plan.actions == []


def test_autonomy_planner_blocks_when_step_budget_is_too_small():
    state = _basic_state()

    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=1, now=NOW)

    assert plan.status == ToyGridWorldAutonomyPlanStatus.BLOCKED
    assert plan.failure_reason == "max_step_budget_exhausted"
    assert plan.max_step_budget == 1
    assert plan.actions == []


def test_blocked_autonomy_plan_requires_failure_reason():
    state = _basic_state()

    with pytest.raises(ValidationError, match="failure_reason"):
        ToyGridWorldAutonomyPlan(
            plan_id="invalid-plan",
            world_id=state.world_id,
            status=ToyGridWorldAutonomyPlanStatus.BLOCKED,
            initial_state=state,
            predicted_final_position=state.agent_position,
            predicted_status=ToyGridWorldStatus.BLOCKED,
            max_step_budget=0,
        )


def test_planned_autonomy_plan_rejects_failure_reason():
    state = _basic_state()

    with pytest.raises(ValidationError, match="failure_reason"):
        ToyGridWorldAutonomyPlan(
            plan_id="invalid-planned-plan",
            world_id=state.world_id,
            status=ToyGridWorldAutonomyPlanStatus.PLANNED,
            initial_state=state,
            actions=[ToyGridWorldAction.MOVE_RIGHT],
            predicted_final_position=ToyGridWorldPosition(x=1, y=0),
            predicted_status=ToyGridWorldStatus.RUNNING,
            max_step_budget=5,
            failure_reason="should_not_be_present",
        )


def test_autonomy_plan_is_deterministic_when_inputs_are_fixed():
    state = _basic_state()

    first = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    second = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    assert first.plan_id == second.plan_id
    assert first.actions == second.actions
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_autonomy_planner_does_not_step_or_mutate_simulator(monkeypatch):
    called = {"replay": 0, "step": 0}

    def fail_step_if_called(*_args, **_kwargs):  # pragma: no cover - failure path
        called["step"] += 1
        raise AssertionError("planner must not step the simulator")

    def fail_replay_if_called(*_args, **_kwargs):  # pragma: no cover - failure path
        called["replay"] += 1
        raise AssertionError("planner must not run replay")

    state = _basic_state()
    monkeypatch.setattr(toy_grid_world, "step_toy_grid_world", fail_step_if_called)
    monkeypatch.setattr(toy_grid_world, "run_toy_grid_world_replay", fail_replay_if_called)

    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    assert plan.status == ToyGridWorldAutonomyPlanStatus.PLANNED
    assert called == {"replay": 0, "step": 0}
    assert state.step_count == 0
    assert state.path_trace == [state.agent_position]


def test_autonomy_planner_source_has_no_execution_calls():
    source = inspect.getsource(toy_grid_world.build_toy_grid_world_autonomy_plan)

    assert "step_toy_grid_world" not in source
    assert "run_toy_grid_world_replay" not in source
