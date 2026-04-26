from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.runtime.mission_contract import build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.physical_mission_replay import SafetyGovernorStatus, TelemetryHealthStatus
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldAutonomousEpisodeStatus,
    ToyGridWorldAutonomyPlan,
    ToyGridWorldAutonomyPlanStatus,
    ToyGridWorldPosition,
    ToyGridWorldStatus,
    build_grid_world_simulation_scenario_request,
    build_grid_world_telemetry_snapshot,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
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
        world_id="episode-basic-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-autonomous-episode",
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
        ],
    )


def _manual_plan(
    state,
    actions: list[ToyGridWorldAction],
    *,
    plan_id: str = "manual-episode-plan",
) -> ToyGridWorldAutonomyPlan:
    final = state.agent_position
    for action in actions:
        if action == ToyGridWorldAction.MOVE_RIGHT:
            final = ToyGridWorldPosition(x=final.x + 1, y=final.y)
        elif action == ToyGridWorldAction.MOVE_LEFT:
            final = ToyGridWorldPosition(x=final.x - 1, y=final.y)
        elif action == ToyGridWorldAction.MOVE_DOWN:
            final = ToyGridWorldPosition(x=final.x, y=final.y + 1)
        elif action == ToyGridWorldAction.MOVE_UP:
            final = ToyGridWorldPosition(x=final.x, y=final.y - 1)
    return ToyGridWorldAutonomyPlan(
        plan_id=plan_id,
        world_id=state.world_id,
        status=ToyGridWorldAutonomyPlanStatus.PLANNED,
        initial_state=state,
        actions=actions,
        predicted_final_position=final,
        predicted_status=(
            ToyGridWorldStatus.GOAL_REACHED
            if final == state.goal_position
            else ToyGridWorldStatus.RUNNING
        ),
        max_step_budget=len(actions),
        constraints_used=["test_manual_plan"],
        safety_assumptions=["runner_must_check_safety_governor_before_every_step"],
    )


def _contains_true_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return any(
            (item_key == key and item_value is True) or _contains_true_key(item_value, key)
            for item_key, item_value in value.items()
        )
    if isinstance(value, list):
        return any(_contains_true_key(item, key) for item in value)
    return False


def test_autonomous_episode_reaches_goal_with_dry_run_steps():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        now=NOW,
    )

    assert episode.schema_version == TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION
    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.GOAL_REACHED
    assert episode.final_status == ToyGridWorldStatus.GOAL_REACHED
    assert episode.final_state.agent_position == state.goal_position
    assert episode.summary["goal_reached"] is True
    assert episode.execution_allowed is False
    assert episode.operator_approval_required is True
    assert episode.live_execution_allowed is False
    assert episode.physical_execution_invoked is False
    assert episode.metadata["simulator_only"] is True
    assert episode.metadata["dry_run_only"] is True
    assert len(episode.steps) == 2
    for index, step in enumerate(episode.steps):
        assert step.schema_version == TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION
        assert step.step_index == index
        assert step.accepted is True
        assert step.telemetry_health_snapshot is not None
        assert step.safety_governor_decision.decision == SafetyGovernorStatus.DRY_RUN_ALLOWED
        assert step.dry_run_action_envelope is not None
        assert step.dry_run_action_envelope.dry_run is True
        assert step.offline_replay_plan is not None
        assert step.live_execution_allowed is False
        assert step.physical_execution_invoked is False
    assert episode.replay_trace.final_status == ToyGridWorldStatus.GOAL_REACHED


def test_autonomous_episode_blocks_hazard_before_movement():
    state = build_toy_grid_world_state(
        width=3,
        height=2,
        agent_position=(0, 0),
        goal_position=(2, 0),
        hazards=[(1, 0)],
        world_id="episode-hazard-world",
    )
    plan = _manual_plan(state, [ToyGridWorldAction.MOVE_RIGHT], plan_id="hazard-plan")

    episode = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)

    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.BLOCKED
    assert episode.summary["stop_reason"] == "hazard"
    assert episode.steps[0].accepted is False
    assert episode.steps[0].blocked_reason == "hazard"
    assert episode.steps[0].next_state.agent_position == state.agent_position
    assert episode.steps[0].dry_run_action_envelope is None
    assert episode.steps[0].offline_replay_plan is None
    assert episode.steps[0].safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED


def test_autonomous_episode_blocks_low_battery():
    state = build_toy_grid_world_state(
        width=3,
        height=2,
        agent_position=(0, 0),
        goal_position=(2, 0),
        battery=20,
        low_battery_threshold=20,
        world_id="episode-low-battery-world",
    )
    plan = _manual_plan(state, [ToyGridWorldAction.MOVE_RIGHT], plan_id="low-battery-plan")

    episode = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)

    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.BLOCKED
    assert episode.summary["stop_reason"] == "unsafe_telemetry"
    assert episode.steps[0].telemetry_health_snapshot.status == TelemetryHealthStatus.UNSAFE
    assert episode.steps[0].safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED
    assert episode.steps[0].dry_run_action_envelope is None


def test_autonomous_episode_blocks_stale_telemetry():
    state = _basic_state()
    plan = _manual_plan(state, [ToyGridWorldAction.MOVE_RIGHT], plan_id="stale-telemetry-plan")
    scenario = build_grid_world_simulation_scenario_request(
        state,
        action=ToyGridWorldAction.MOVE_RIGHT,
        now=NOW,
    )
    stale = build_grid_world_telemetry_snapshot(
        state,
        scenario_id=scenario.scenario_id,
        observed_at=NOW - timedelta(seconds=120),
        now=NOW,
    )

    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        telemetry_sequence=[stale],
        now=NOW,
    )

    assert stale.status == TelemetryHealthStatus.STALE
    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.BLOCKED
    assert episode.steps[0].telemetry_health_snapshot.status == TelemetryHealthStatus.STALE
    assert episode.steps[0].safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED


def test_autonomous_episode_blocks_missing_telemetry():
    state = _basic_state()
    plan = _manual_plan(state, [ToyGridWorldAction.MOVE_RIGHT], plan_id="missing-telemetry-plan")

    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        telemetry_sequence=[None],
        now=NOW,
    )

    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.BLOCKED
    assert episode.steps[0].telemetry_health_snapshot.status == TelemetryHealthStatus.MISSING
    assert episode.steps[0].safety_governor_decision.decision == SafetyGovernorStatus.BLOCKED
    assert episode.steps[0].dry_run_action_envelope is None


def test_autonomous_episode_max_step_budget_stops_episode():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        max_steps=1,
        now=NOW,
    )

    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.MAX_STEPS_EXHAUSTED
    assert episode.summary["stop_reason"] == "max_steps_exhausted"
    assert len(episode.steps) == 1
    assert episode.final_status == ToyGridWorldStatus.RUNNING


def test_autonomous_episode_is_deterministic_when_inputs_are_fixed():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    first = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)
    second = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)

    assert first.episode_id == second.episode_id
    assert first.replay_trace.deterministic_hash == second.replay_trace.deterministic_hash
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_autonomous_episode_rejects_plan_initial_state_mismatch():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    shifted_state = build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 1),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id=state.world_id,
    )

    episode = run_toy_grid_world_autonomous_episode(shifted_state, plan, now=NOW)

    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.PLAN_MISMATCH
    assert episode.summary["stop_reason"] == "plan_initial_state_mismatch"
    assert episode.steps == []
    assert episode.replay_trace.steps == []


def test_autonomous_episode_does_not_execute_blocked_plan():
    state = build_toy_grid_world_state(
        width=3,
        height=1,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 0)],
        world_id="episode-blocked-plan-world",
    )
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)

    episode = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)

    assert plan.status == ToyGridWorldAutonomyPlanStatus.BLOCKED
    assert episode.status == ToyGridWorldAutonomousEpisodeStatus.PLAN_BLOCKED
    assert episode.summary["stop_reason"] == plan.failure_reason
    assert episode.steps == []
    assert episode.replay_trace.steps == []
    assert episode.final_state == state
    assert episode.live_execution_allowed is False
    assert episode.physical_execution_invoked is False


def test_autonomous_episode_never_sets_live_execution_flags():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    episode = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)
    payload = episode.model_dump(mode="json")

    assert _contains_true_key(payload, "live_execution_allowed") is False
    assert _contains_true_key(payload, "physical_execution_invoked") is False


def test_autonomous_episode_e2e_plan_episode_eval_chain():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        now=NOW,
    )
    artifacts = {
        "autonomous_episode": episode.model_dump(mode="json"),
        "toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json"),
    }

    for suite_id in [
        "toy_grid_goal_reached_path",
        "toy_grid_replay_determinism",
        "physical_replay_no_live_execution",
        "physical_replay_dry_run_envelope_only",
        "physical_replay_offline_only",
    ]:
        result = run_mission_eval_suite(
            suite_id,
            artifacts,
            subject_id=f"episode-e2e:{suite_id}",
        )

        assert result.passed is True
