"""Run the golden toy-grid autonomy gate corpus through the gate builder.

Asserts exact-set equality on blocked_reasons and warning_reasons so any change
to gate logic forces a corpus update.
"""

from __future__ import annotations

import pytest

from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    build_toy_grid_world_autonomy_gate_result,
)
from tests.fixtures.toy_grid_autonomy_corpus import (
    CORPUS_NOW,
    TOY_GRID_SIMULATOR_ID,
    GoldenToyGridAutonomyCase,
    build_golden_toy_grid_autonomy_cases,
    golden_toy_grid_autonomy_case_ids,
)


_EXPECTED_CASE_IDS = {
    "clean_goal_reached",
    "live_execution_flag",
    "physical_execution_invoked",
    "accepted_hazard_move",
    "missing_telemetry",
    "stale_telemetry",
    "telemetry_mismatch",
    "replay_hash_mismatch",
    "dry_run_false",
    "offline_replay_plan_allows_live_execution",
}


def test_corpus_contains_expected_case_ids():
    assert golden_toy_grid_autonomy_case_ids() == _EXPECTED_CASE_IDS


def test_corpus_contains_at_least_one_fully_clean_passing_case():
    cases = build_golden_toy_grid_autonomy_cases()
    clean = [
        case
        for case in cases
        if case.expected_passed
        and case.expected_status == "passed"
        and case.expected_blocked_reasons == ()
        and case.expected_warning_reasons == ()
    ]
    assert clean, "corpus must include at least one fully-clean passing case"


def test_every_case_tags_simulator_and_gate_schema_version():
    for case in build_golden_toy_grid_autonomy_cases():
        assert case.simulator_id == TOY_GRID_SIMULATOR_ID
        assert case.gate_schema_version == TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "case",
    build_golden_toy_grid_autonomy_cases(),
    ids=lambda case: case.case_id,
)
def test_golden_toy_grid_autonomy_gate_case(case: GoldenToyGridAutonomyCase):
    gate = build_toy_grid_world_autonomy_gate_result(
        case.artifacts["scorecard"],
        autonomy_episode_review=case.artifacts["review"],
        safety_eval_results=case.artifacts.get("safety_eval_results"),
        now=CORPUS_NOW,
    )

    assert gate.schema_version == case.gate_schema_version
    assert gate.passed is case.expected_passed
    assert gate.status.value == case.expected_status
    assert set(gate.blocked_reasons) == set(case.expected_blocked_reasons)
    assert set(gate.warning_reasons) == set(case.expected_warning_reasons)
    assert gate.operator_approval_required is True
    assert gate.operator_approval_performed is False
    assert gate.stronger_execution_allowed is False
    assert gate.live_execution_allowed is False
    assert gate.physical_execution_invoked is False
    assert gate.metadata["rule_based"] is True
    assert gate.metadata["llm_judge_used"] is False
