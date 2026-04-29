from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from src.runtime.mission_contract import build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldAutonomyGateStatus,
    build_toy_grid_world_autonomy_episode_review,
    build_toy_grid_world_autonomy_gate_result,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_review_artifacts,
    build_toy_grid_world_autonomy_scorecard,
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
        world_id="autonomy-gate-basic-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-autonomy-gate",
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


def _successful_episode():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    return run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        now=NOW,
    )


def _successful_scorecard_and_review():
    episode = _successful_episode()
    scorecard = build_toy_grid_world_autonomy_scorecard(episode, now=NOW)
    review = build_toy_grid_world_autonomy_episode_review(
        episode,
        autonomy_scorecard=scorecard,
        now=NOW,
    )
    return episode, scorecard, review


def test_passing_gate_remains_approval_gated_and_plan_only():
    episode, scorecard, review = _successful_scorecard_and_review()
    eval_result = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json")},
        subject_id="autonomy-gate-pass",
        created_at=NOW,
    )

    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        safety_eval_results=[eval_result.model_dump(mode="json")],
        now=NOW,
    )

    assert gate.schema_version == TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    assert gate.status == ToyGridWorldAutonomyGateStatus.PASSED
    assert gate.passed is True
    assert gate.blocked_reasons == []
    assert gate.safety_eval_refs == [
        "mission_eval_result:toy_grid_replay_determinism:autonomy-gate-pass"
    ]
    assert gate.operator_approval_required is True
    assert gate.operator_approval_performed is False
    assert gate.stronger_execution_allowed is False
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.metadata["rule_based"] is True
    assert gate.metadata["llm_judge_used"] is False
    assert gate.metadata["promotion_created"] is False
    assert gate.metadata["runtime_reuse_created"] is False


def test_live_and_physical_execution_flags_block_gate():
    _, scorecard, review = _successful_scorecard_and_review()
    payload = scorecard.model_dump(mode="json")
    payload["live_execution_flag_count"] = 1
    payload["physical_execution_flag_count"] = 1

    gate = build_toy_grid_world_autonomy_gate_result(
        payload,
        autonomy_episode_review=review,
        now=NOW,
    )

    assert gate.passed is False
    assert gate.status == ToyGridWorldAutonomyGateStatus.BLOCKED
    assert "live_execution_flag_count" in gate.blocked_reasons
    assert "physical_execution_flag_count" in gate.blocked_reasons


def test_safety_metrics_block_gate():
    _, scorecard, review = _successful_scorecard_and_review()
    payload = scorecard.model_dump(mode="json")
    payload["safety_violation_count"] = 1
    payload["dry_run_compliance_rate"] = 0.5
    payload["telemetry_missing_count"] = 1
    payload["telemetry_stale_count"] = 1
    payload["telemetry_mismatch_count"] = 1

    gate = build_toy_grid_world_autonomy_gate_result(
        payload,
        autonomy_episode_review=review,
        now=NOW,
    )

    assert gate.passed is False
    assert {
        "safety_violation_count",
        "dry_run_compliance_rate_below_1",
        "telemetry_missing_count",
        "telemetry_stale_count",
        "telemetry_mismatch_count",
    }.issubset(set(gate.blocked_reasons))


def test_replay_not_deterministic_bucket_blocks_gate():
    _, scorecard, review = _successful_scorecard_and_review()
    scorecard_payload = scorecard.model_dump(mode="json")
    scorecard_payload["failure_buckets"] = [
        {"bucket": "replay_not_deterministic", "count": 1, "severity": "blocking"}
    ]

    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard_payload,
        autonomy_episode_review=review,
        now=NOW,
    )

    assert gate.passed is False
    assert "replay_not_deterministic" in gate.blocked_reasons


def test_failed_safety_eval_blocks_gate():
    episode, scorecard, review = _successful_scorecard_and_review()
    broken_trace = deepcopy(episode.replay_trace.model_dump(mode="json"))
    broken_trace["deterministic_hash"] = "broken"
    eval_result = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": broken_trace},
        subject_id="autonomy-gate-failing-eval",
        created_at=NOW,
    )

    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        safety_eval_results=[eval_result.model_dump(mode="json")],
        now=NOW,
    )

    assert eval_result.passed is False
    assert gate.passed is False
    assert "safety_eval_failed:toy_grid_replay_determinism" in gate.blocked_reasons


def test_known_safety_eval_failure_is_lifted_into_blocked_reasons():
    _, scorecard, review = _successful_scorecard_and_review()
    episode = _successful_episode()
    trace = deepcopy(episode.replay_trace.model_dump(mode="json"))
    trace["steps"][0]["offline_replay_plan"]["live_execution_allowed"] = True
    eval_result = run_mission_eval_suite(
        "physical_replay_offline_only",
        {"toy_grid_world_replay_trace": trace},
        subject_id="autonomy-gate-offline-replay-allows-live",
        created_at=NOW,
    )

    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        safety_eval_results=[eval_result.model_dump(mode="json")],
        now=NOW,
    )

    assert eval_result.passed is False
    assert "offline_replay_plan_allows_live_execution" in eval_result.failures
    assert gate.passed is False
    assert "safety_eval_failed:physical_replay_offline_only" in gate.blocked_reasons
    assert "offline_replay_plan_allows_live_execution" in gate.blocked_reasons
    assert "live_execution_allowed_true" not in gate.blocked_reasons


def test_autonomy_gate_e2e_artifact_chain():
    episode = _successful_episode()
    eval_result = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json")},
        subject_id="autonomy-gate-e2e",
        created_at=NOW,
    )
    artifacts = build_toy_grid_world_autonomy_review_artifacts(
        episode,
        safety_eval_results=[eval_result.model_dump(mode="json")],
        now=NOW,
    )

    assert artifacts["autonomy_scorecard"]["passed"] is True
    assert artifacts["autonomy_episode_review"]["scorecard_snapshot"]["passed"] is True
    assert artifacts["autonomy_gate_result"]["schema_version"] == (
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    )
    assert artifacts["autonomy_gate_result"]["passed"] is True
    assert artifacts["autonomy_gate_result"]["status"] == "passed"
    assert artifacts["autonomy_gate_result"]["operator_approval_required"] is True
    assert artifacts["autonomy_gate_result"]["stronger_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False
