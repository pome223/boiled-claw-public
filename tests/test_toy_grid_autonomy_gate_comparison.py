"""Tests for compare_toy_grid_world_autonomy_gate_results.

Exercises ``autonomy_gate_comparison_result.v1`` against pairs of
``autonomy_gate_result.v1`` artifacts built from the existing golden corpus
plus a quality-only candidate variant. Asserts rule-based, deterministic
behavior with explicit safety / quality regression separation.

Out of scope:
- promotion / runtime reuse
- UI integration
- live or stronger execution
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION,
    ToyGridWorldAutonomyGateComparisonStatus,
    build_toy_grid_world_autonomy_gate_result,
    compare_toy_grid_world_autonomy_gate_results,
)
from tests.fixtures.toy_grid_autonomy_corpus import (
    CORPUS_NOW,
    build_golden_toy_grid_autonomy_cases,
)


NOW = datetime(2026, 4, 27, 14, 0, tzinfo=timezone.utc)


def _gate_for_case(case_id: str):
    cases = {case.case_id: case for case in build_golden_toy_grid_autonomy_cases()}
    case = cases[case_id]
    return build_toy_grid_world_autonomy_gate_result(
        case.artifacts["scorecard"],
        autonomy_episode_review=case.artifacts["review"],
        safety_eval_results=case.artifacts.get("safety_eval_results"),
        now=CORPUS_NOW,
    )


def _baseline_clean_gate():
    return _gate_for_case("clean_goal_reached")


def _candidate_with_scorecard_overrides(overrides: dict[str, object]):
    cases = {case.case_id: case for case in build_golden_toy_grid_autonomy_cases()}
    case = cases["clean_goal_reached"]
    scorecard = deepcopy(case.artifacts["scorecard"])
    for key, value in overrides.items():
        scorecard[key] = value
    return build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=case.artifacts["review"],
        safety_eval_results=case.artifacts.get("safety_eval_results"),
        now=CORPUS_NOW,
    )


def test_clean_baseline_clean_candidate_passes():
    baseline = _baseline_clean_gate()
    candidate = _baseline_clean_gate()

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert (
        comparison.schema_version
        == TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION
    )
    assert comparison.status == ToyGridWorldAutonomyGateComparisonStatus.PASSED
    assert comparison.passed is True
    assert comparison.blocked_reasons == []
    assert comparison.warning_reasons == []
    assert comparison.baseline_gate_id == baseline.gate_id
    assert comparison.candidate_gate_id == candidate.gate_id
    assert comparison.operator_approval_required is True
    assert comparison.operator_approval_performed is False
    assert comparison.stronger_execution_allowed is False
    assert comparison.live_execution_allowed is False
    assert comparison.physical_execution_invoked is False
    assert comparison.metadata["rule_based"] is True
    assert comparison.metadata["llm_judge_used"] is False
    assert comparison.metadata["promotion_created"] is False
    assert comparison.metadata["runtime_reuse_created"] is False
    assert comparison.metadata["stronger_execution_allowed"] is False


def test_clean_baseline_live_execution_candidate_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("live_execution_flag")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    assert "metric_regressed:live_execution_flag_count" in comparison.blocked_reasons
    delta = comparison.metric_deltas["live_execution_flag_count"]
    assert delta.baseline == 0.0
    assert delta.candidate == 1.0
    assert delta.delta == 1.0


def test_clean_baseline_physical_execution_candidate_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("physical_execution_invoked")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    assert (
        "metric_regressed:physical_execution_flag_count"
        in comparison.blocked_reasons
    )


def test_clean_baseline_accepted_hazard_candidate_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("accepted_hazard_move")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    assert "metric_regressed:safety_violation_count" in comparison.blocked_reasons


def test_clean_baseline_stale_telemetry_candidate_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("stale_telemetry")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    assert "metric_regressed:telemetry_stale_count" in comparison.blocked_reasons


def test_clean_baseline_replay_hash_mismatch_candidate_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("replay_hash_mismatch")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    # replay_not_deterministic is bucket-derived, not a metric, so no
    # metric_regressed reason fires for it.
    assert not any(
        reason.startswith("metric_regressed:")
        for reason in comparison.blocked_reasons
    )


def test_clean_baseline_dry_run_compliance_regression_blocks():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("dry_run_false")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" in comparison.blocked_reasons
    assert (
        "metric_regressed:dry_run_compliance_rate" in comparison.blocked_reasons
    )
    delta = comparison.metric_deltas["dry_run_compliance_rate"]
    assert delta.baseline == 1.0
    assert delta.candidate == 0.5
    assert delta.delta == -0.5


def test_path_efficiency_regression_emits_warning_only():
    baseline = _baseline_clean_gate()
    # Candidate is fully safe (passes scorecard) but has lower path efficiency.
    candidate = _candidate_with_scorecard_overrides({"path_efficiency": 0.5})

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is True
    assert comparison.blocked_reasons == []
    assert "quality_metric_regressed:path_efficiency" in comparison.warning_reasons
    assert not any(
        reason.startswith("metric_regressed:") for reason in comparison.blocked_reasons
    )
    delta = comparison.metric_deltas["path_efficiency"]
    assert delta.baseline == 1.0
    assert delta.candidate == 0.5
    assert delta.delta == -0.5


def test_quality_count_regressions_emit_warning_only():
    baseline = _baseline_clean_gate()
    candidate = _candidate_with_scorecard_overrides(
        {"recovery_attempt_count": 3, "replan_count": 2}
    )

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is True
    assert comparison.blocked_reasons == []
    assert (
        "quality_metric_regressed:recovery_attempt_count"
        in comparison.warning_reasons
    )
    assert "quality_metric_regressed:replan_count" in comparison.warning_reasons


def test_comparison_is_deterministic():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("live_execution_flag")

    first = compare_toy_grid_world_autonomy_gate_results(baseline, candidate, now=NOW)
    second = compare_toy_grid_world_autonomy_gate_results(baseline, candidate, now=NOW)

    assert first.comparison_id == second.comparison_id
    assert first.blocked_reasons == second.blocked_reasons
    assert first.warning_reasons == second.warning_reasons
    assert first.metric_deltas == second.metric_deltas


def test_comparison_blocked_reasons_are_stable_sorted_and_deduped():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("accepted_hazard_move")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.blocked_reasons == sorted(comparison.blocked_reasons)
    assert len(comparison.blocked_reasons) == len(set(comparison.blocked_reasons))
    assert comparison.warning_reasons == sorted(comparison.warning_reasons)
    assert len(comparison.warning_reasons) == len(set(comparison.warning_reasons))


def test_comparison_accepts_dict_inputs():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("live_execution_flag")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline.model_dump(mode="json"),
        candidate.model_dump(mode="json"),
        now=NOW,
    )

    assert comparison.passed is False
    assert "candidate_gate_blocked" in comparison.blocked_reasons


def test_failing_baseline_does_not_emit_regression_marker():
    # Both gates fail. The "regression_from_passing_baseline" marker should
    # not fire because baseline did not pass to begin with. candidate_gate_blocked
    # still fires.
    baseline = _gate_for_case("live_execution_flag")
    candidate = _gate_for_case("physical_execution_invoked")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert "candidate_gate_blocked" in comparison.blocked_reasons
    assert "regression_from_passing_baseline" not in comparison.blocked_reasons


@pytest.mark.parametrize(
    "case_id, expected_metric",
    [
        ("live_execution_flag", "live_execution_flag_count"),
        ("physical_execution_invoked", "physical_execution_flag_count"),
        ("accepted_hazard_move", "safety_violation_count"),
        ("missing_telemetry", "telemetry_missing_count"),
        ("stale_telemetry", "telemetry_stale_count"),
        ("telemetry_mismatch", "telemetry_mismatch_count"),
    ],
)
def test_each_safety_metric_regression_blocks(case_id: str, expected_metric: str):
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case(case_id)

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    assert comparison.passed is False
    assert f"metric_regressed:{expected_metric}" in comparison.blocked_reasons


# ---------------------------------------------------------------------------
# direction + severity (per-row metric metadata)
# ---------------------------------------------------------------------------


_LOWER_IS_BETTER_METRICS = {
    "safety_violation_count",
    "live_execution_flag_count",
    "physical_execution_flag_count",
    "telemetry_missing_count",
    "telemetry_stale_count",
    "telemetry_mismatch_count",
    "blocked_step_count",
    "recovery_attempt_count",
    "replan_count",
}
_HIGHER_IS_BETTER_METRICS = {
    "dry_run_compliance_rate",
    "path_efficiency",
}
_SAFETY_METRICS = {
    "safety_violation_count",
    "live_execution_flag_count",
    "physical_execution_flag_count",
    "telemetry_missing_count",
    "telemetry_stale_count",
    "telemetry_mismatch_count",
    "blocked_step_count",
    "dry_run_compliance_rate",
}
_QUALITY_METRICS = {
    "recovery_attempt_count",
    "replan_count",
    "path_efficiency",
}


def test_metric_deltas_carry_correct_direction_for_every_metric():
    baseline = _baseline_clean_gate()
    candidate = _baseline_clean_gate()

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    for name, delta in comparison.metric_deltas.items():
        if name in _LOWER_IS_BETTER_METRICS:
            assert delta.direction.value == "lower_is_better", name
        elif name in _HIGHER_IS_BETTER_METRICS:
            assert delta.direction.value == "higher_is_better", name
        else:
            raise AssertionError(f"unexpected metric in comparison: {name}")


def test_clean_clean_severity_is_info_for_every_metric():
    baseline = _baseline_clean_gate()
    candidate = _baseline_clean_gate()

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    for name, delta in comparison.metric_deltas.items():
        assert delta.severity.value == "info", name


def test_safety_metric_regression_severity_is_blocking_only_for_that_row():
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("accepted_hazard_move")  # raises safety_violation_count

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    safety_violation = comparison.metric_deltas["safety_violation_count"]
    assert safety_violation.severity.value == "blocking"
    # Other safety metrics did not regress, so their severity stays "info".
    assert (
        comparison.metric_deltas["live_execution_flag_count"].severity.value == "info"
    )
    # Quality metrics did not regress, so their severity stays "info".
    assert comparison.metric_deltas["path_efficiency"].severity.value == "info"


def test_quality_metric_regression_severity_is_warning_only_for_that_row():
    baseline = _baseline_clean_gate()
    candidate = _candidate_with_scorecard_overrides({"path_efficiency": 0.5})

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    path_efficiency = comparison.metric_deltas["path_efficiency"]
    assert path_efficiency.severity.value == "warning"
    # No other metric regressed, so all other severities are "info".
    for name, delta in comparison.metric_deltas.items():
        if name == "path_efficiency":
            continue
        assert delta.severity.value == "info", name


def test_safety_metric_severity_class_is_never_warning_when_regressed():
    # A safety-class metric MUST emit "blocking" when regressed, never "warning".
    baseline = _baseline_clean_gate()
    candidate = _gate_for_case("live_execution_flag")

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    for name in _SAFETY_METRICS:
        delta = comparison.metric_deltas[name]
        # Either it didn't regress (info) or it did (must be blocking, never warning)
        assert delta.severity.value in {"info", "blocking"}, name


def test_quality_metric_severity_class_is_never_blocking_when_regressed():
    # A quality-class metric MUST emit "warning" when regressed, never "blocking".
    baseline = _baseline_clean_gate()
    candidate = _candidate_with_scorecard_overrides(
        {"path_efficiency": 0.3, "recovery_attempt_count": 5, "replan_count": 4}
    )

    comparison = compare_toy_grid_world_autonomy_gate_results(
        baseline, candidate, now=NOW
    )

    for name in _QUALITY_METRICS:
        delta = comparison.metric_deltas[name]
        assert delta.severity.value in {"info", "warning"}, name
