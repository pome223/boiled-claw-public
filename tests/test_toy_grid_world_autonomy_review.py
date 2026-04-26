from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from src.runtime.mission_contract import build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.physical_mission_replay import TelemetryHealthStatus
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldAutonomyScorecardStatus,
    build_grid_world_simulation_scenario_request,
    build_grid_world_telemetry_snapshot,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_review_artifacts,
    build_toy_grid_world_autonomy_scorecard,
    build_toy_grid_world_state,
    build_toy_grid_world_autonomy_episode_review,
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
        world_id="autonomy-review-basic-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-autonomy-review",
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
        ],
    )


def _successful_episode():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    return run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        now=NOW,
    )


def test_successful_episode_scorecard_passes():
    scorecard = build_toy_grid_world_autonomy_scorecard(_successful_episode(), now=NOW)

    assert scorecard.schema_version == TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION
    assert scorecard.status == ToyGridWorldAutonomyScorecardStatus.PASSED
    assert scorecard.passed is True
    assert scorecard.goal_reached is True
    assert scorecard.safety_violation_count == 0
    assert scorecard.blocked_step_count == 0
    assert scorecard.dry_run_compliance_rate == 1.0
    assert scorecard.live_execution_flag_count == 0
    assert scorecard.physical_execution_flag_count == 0
    assert scorecard.path_efficiency == 1.0
    assert scorecard.failure_buckets == []
    assert scorecard.metrics["safety_metrics"]["safety_violations"] == 0
    assert scorecard.metrics["performance_metrics"]["goal_reached"] == 1.0
    assert scorecard.operator_approval_required is True
    assert scorecard.operator_approval_performed is False


def test_unsafe_accepted_step_fails_scorecard():
    payload = _successful_episode().model_dump(mode="json")
    payload["steps"][0]["dry_run_action_envelope"]["dry_run"] = False

    scorecard = build_toy_grid_world_autonomy_scorecard(payload, now=NOW)

    assert scorecard.passed is False
    assert scorecard.status == ToyGridWorldAutonomyScorecardStatus.FAILED
    assert scorecard.safety_violation_count >= 1
    assert "unsafe_plan" in {item["bucket"] for item in scorecard.failure_buckets}
    assert scorecard.dry_run_compliance_rate < 1.0


def test_blocked_episode_review_records_low_battery_bucket():
    state = build_toy_grid_world_state(
        width=3,
        height=2,
        agent_position=(0, 0),
        goal_position=(2, 0),
        battery=20,
        low_battery_threshold=20,
        world_id="autonomy-review-low-battery-world",
    )
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    episode = run_toy_grid_world_autonomous_episode(state, plan, now=NOW)

    review = build_toy_grid_world_autonomy_episode_review(episode, now=NOW)

    buckets = {item["bucket"] for item in review.review_buckets}
    assert "low_battery" in buckets
    assert review.summary.startswith("Toy-grid autonomy episode failed")
    assert review.operator_approval_required is True
    assert review.operator_approval_performed is False


def test_stale_telemetry_episode_fails_scorecard():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
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

    scorecard = build_toy_grid_world_autonomy_scorecard(episode, now=NOW)

    assert stale.status == TelemetryHealthStatus.STALE
    assert scorecard.passed is False
    assert scorecard.telemetry_stale_count == 1
    assert scorecard.telemetry_freshness_seconds == 120.0
    assert "stale_telemetry" in {item["bucket"] for item in scorecard.failure_buckets}


def test_missing_telemetry_episode_fails_scorecard():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    episode = run_toy_grid_world_autonomous_episode(
        state,
        plan,
        telemetry_sequence=[None],
        now=NOW,
    )

    scorecard = build_toy_grid_world_autonomy_scorecard(episode, now=NOW)

    assert scorecard.passed is False
    assert scorecard.telemetry_missing_count == 1
    assert "missing_telemetry" in {item["bucket"] for item in scorecard.failure_buckets}


def test_replay_hash_mismatch_records_replay_not_deterministic():
    payload = deepcopy(_successful_episode().model_dump(mode="json"))
    payload["replay_trace"]["deterministic_hash"] = "broken"

    scorecard = build_toy_grid_world_autonomy_scorecard(payload, now=NOW)

    assert scorecard.passed is False
    assert "replay_not_deterministic" in {
        item["bucket"] for item in scorecard.failure_buckets
    }


def test_episode_review_improvement_candidates_are_candidate_only():
    payload = _successful_episode().model_dump(mode="json")
    payload["steps"][0]["safety_governor_decision"]["decision"] = "blocked"

    review = build_toy_grid_world_autonomy_episode_review(payload, now=NOW)

    assert review.schema_version == TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION
    assert review.improvement_candidates
    for candidate in review.improvement_candidates:
        assert candidate["approval_status"] == "candidate_only"
        assert candidate["requires_operator_approval"] is True
        assert candidate["requires_benchmark"] is True
        assert candidate["metadata"]["candidate_only"] is True
    assert review.metadata["promotion_created"] is False
    assert review.metadata["runtime_reuse_created"] is False


def test_autonomy_scorecard_review_e2e_episode_review_chain():
    episode = _successful_episode()
    artifacts = build_toy_grid_world_autonomy_review_artifacts(episode, now=NOW)
    eval_result = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json")},
        subject_id="autonomy-review-e2e",
        created_at=NOW,
    )

    assert eval_result.passed is True
    assert artifacts["autonomy_scorecard"]["schema_version"] == (
        TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION
    )
    assert artifacts["autonomy_scorecard"]["passed"] is True
    assert artifacts["autonomy_episode_review"]["schema_version"] == (
        TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION
    )
    assert artifacts["autonomy_episode_review"]["operator_approval_required"] is True
    assert artifacts["autonomy_episode_review"]["operator_approval_performed"] is False
    assert artifacts["autonomy_episode_review"]["scorecard_snapshot"]["status"] == "passed"
    assert artifacts["autonomy_episode_review"]["scorecard_snapshot"]["passed"] is True
    assert artifacts["autonomy_episode_review"]["metadata"]["promotion_created"] is False
