"""Tests for the toy-grid autonomy safety regression gate entry-point.

Exercises ``build_toy_grid_world_autonomy_safety_regression_gate``: the high-level
aggregator that ties scorecard / review / required safety eval suites together
into a single ``autonomy_gate_result.v1``.

Out of scope:
- baseline vs candidate comparison (next PR)
- promotion / runtime reuse
- UI / runtime integration
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.runtime.mission_contract import build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAction,
    ToyGridWorldAutonomyGateStatus,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_safety_regression_gate,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
)
from src.runtime.toy_grid_world import _DEFAULT_SAFETY_REGRESSION_SUITES


NOW = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


def _basic_state():
    return build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="safety-regression-gate-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-safety-regression-gate",
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


def _clean_episode():
    state = _basic_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=NOW)
    return run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_mission_contract(),
        now=NOW,
    )


def test_default_required_safety_suites_are_the_core_four():
    assert _DEFAULT_SAFETY_REGRESSION_SUITES == (
        "physical_replay_no_live_execution",
        "physical_replay_offline_only",
        "physical_replay_dry_run_envelope_only",
        "toy_grid_replay_determinism",
    )


def test_safety_regression_gate_passes_for_clean_episode():
    episode = _clean_episode()

    gate = build_toy_grid_world_autonomy_safety_regression_gate(episode, now=NOW)

    assert gate.schema_version == TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    assert gate.status == ToyGridWorldAutonomyGateStatus.PASSED
    assert gate.passed is True
    assert gate.blocked_reasons == []
    assert {ref.split(":", 1)[1].split(":", 1)[0] for ref in gate.safety_eval_refs} == set(
        _DEFAULT_SAFETY_REGRESSION_SUITES
    )
    assert gate.operator_approval_required is True
    assert gate.operator_approval_performed is False
    assert gate.stronger_execution_allowed is False
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.metadata["rule_based"] is True
    assert gate.metadata["llm_judge_used"] is False
    assert gate.metadata["entry_point"] == "safety_regression_gate"
    assert gate.metadata["default_required_safety_suite_ids"] == list(
        _DEFAULT_SAFETY_REGRESSION_SUITES
    )


def test_safety_regression_gate_blocks_when_required_suite_missing():
    episode = _clean_episode()

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        safety_eval_results=[],
        now=NOW,
    )

    assert gate.passed is False
    assert gate.status == ToyGridWorldAutonomyGateStatus.BLOCKED
    expected_missing = {
        f"required_safety_suite_missing:{suite_id}"
        for suite_id in _DEFAULT_SAFETY_REGRESSION_SUITES
    }
    assert expected_missing.issubset(set(gate.blocked_reasons))


def test_safety_regression_gate_does_not_emit_missing_for_failing_present_suite():
    episode = _clean_episode()
    broken_trace = deepcopy(episode.replay_trace.model_dump(mode="json"))
    broken_trace["steps"][0]["offline_replay_plan"]["live_execution_allowed"] = True
    failing_offline = run_mission_eval_suite(
        "physical_replay_offline_only",
        {"toy_grid_world_replay_trace": broken_trace},
        subject_id="safety-regression-gate-failing-offline",
        created_at=NOW,
    )

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        required_safety_suite_ids=("physical_replay_offline_only",),
        safety_eval_results=[failing_offline.model_dump(mode="json")],
        now=NOW,
    )

    assert gate.passed is False
    assert "safety_eval_failed:physical_replay_offline_only" in gate.blocked_reasons
    assert "offline_replay_plan_allows_live_execution" in gate.blocked_reasons
    assert (
        "required_safety_suite_missing:physical_replay_offline_only"
        not in gate.blocked_reasons
    )


def test_safety_regression_gate_blocks_when_caller_supplies_partial_suites():
    episode = _clean_episode()
    determinism = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json")},
        subject_id="safety-regression-gate-partial",
        created_at=NOW,
    )

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        safety_eval_results=[determinism.model_dump(mode="json")],
        now=NOW,
    )

    assert gate.passed is False
    missing = {
        reason
        for reason in gate.blocked_reasons
        if reason.startswith("required_safety_suite_missing:")
    }
    assert missing == {
        f"required_safety_suite_missing:{suite_id}"
        for suite_id in _DEFAULT_SAFETY_REGRESSION_SUITES
        if suite_id != "toy_grid_replay_determinism"
    }


def test_safety_regression_gate_blocks_when_episode_has_live_execution_flag():
    episode = _clean_episode()
    episode_payload = episode.model_dump(mode="json")
    episode_payload["steps"][0]["live_execution_allowed"] = True

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode_payload,
        now=NOW,
    )

    assert gate.passed is False
    assert "live_execution_flag_count" in gate.blocked_reasons


def test_safety_regression_gate_is_deterministic():
    episode = _clean_episode()

    first = build_toy_grid_world_autonomy_safety_regression_gate(episode, now=NOW)
    second = build_toy_grid_world_autonomy_safety_regression_gate(episode, now=NOW)

    assert first.gate_id == second.gate_id
    assert first.blocked_reasons == second.blocked_reasons
    assert first.warning_reasons == second.warning_reasons
    assert first.safety_eval_refs == second.safety_eval_refs


@pytest.mark.parametrize(
    "suite_id",
    list(_DEFAULT_SAFETY_REGRESSION_SUITES),
)
def test_safety_regression_gate_runs_each_default_suite(suite_id: str):
    episode = _clean_episode()

    gate = build_toy_grid_world_autonomy_safety_regression_gate(episode, now=NOW)

    assert any(suite_id in ref for ref in gate.safety_eval_refs)
