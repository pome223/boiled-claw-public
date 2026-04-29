"""Tests for the toy-grid autonomy artifact attach helper.

Verifies the read-only persistence adapter that bridges
``toy_grid_world.py`` artifact builders to ``task_store`` writes:

- All five autonomy artifact keys land on ``task.artifacts`` (when baseline
  is provided) / four keys (no comparison without baseline)
- schema_version is preserved on every artifact
- existing ``task.artifacts`` are merged, not overwritten
- ``task.status`` does not change (read-only)
- attached artifacts carry the safety invariants we expect
- determinism: identical inputs produce identical gate / comparison ids
- missing task raises ``AutonomyArtifactAttachError``
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.autonomy_artifact_attach import (
    AutonomyArtifactAttachError,
    attach_toy_grid_world_autonomy_artifacts,
)
from src.runtime.mission_contract import build_mission_contract
from src.runtime.task_store import get_task_store
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION,
    ToyGridWorldAction,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_safety_regression_gate,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
)


NOW = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)


def _basic_state():
    return build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="autonomy-attach-world",
    )


def _mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-autonomy-attach",
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


def _create_task(kind: str = "autonomy", title: str = "autonomy attach test"):
    return get_task_store().create(kind=kind, title=title, status="running")


def test_attach_writes_four_keys_when_no_baseline():
    task = _create_task()
    episode = _clean_episode()

    artifacts = attach_toy_grid_world_autonomy_artifacts(
        task["task_id"],
        episode,
        now=NOW,
    )

    assert set(artifacts.keys()) == {
        "autonomous_episode",
        "autonomy_scorecard",
        "autonomy_episode_review",
        "autonomy_gate_result",
    }
    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    for key in artifacts:
        assert key in stored["artifacts"]
    assert "autonomy_gate_comparison_result" not in stored["artifacts"]


def test_attach_writes_five_keys_when_baseline_provided():
    task = _create_task()
    episode = _clean_episode()
    baseline_gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        now=NOW,
    )

    artifacts = attach_toy_grid_world_autonomy_artifacts(
        task["task_id"],
        episode,
        baseline_gate=baseline_gate,
        now=NOW,
    )

    assert set(artifacts.keys()) == {
        "autonomous_episode",
        "autonomy_scorecard",
        "autonomy_episode_review",
        "autonomy_gate_result",
        "autonomy_gate_comparison_result",
    }
    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    for key in artifacts:
        assert key in stored["artifacts"]


def test_attach_preserves_every_schema_version():
    task = _create_task()
    episode = _clean_episode()
    baseline_gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode, now=NOW
    )

    attach_toy_grid_world_autonomy_artifacts(
        task["task_id"],
        episode,
        baseline_gate=baseline_gate,
        now=NOW,
    )

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    assert (
        artifacts["autonomous_episode"]["schema_version"]
        == TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION
    )
    assert (
        artifacts["autonomy_scorecard"]["schema_version"]
        == TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION
    )
    assert (
        artifacts["autonomy_episode_review"]["schema_version"]
        == TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION
    )
    assert (
        artifacts["autonomy_gate_result"]["schema_version"]
        == TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    )
    assert (
        artifacts["autonomy_gate_comparison_result"]["schema_version"]
        == TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION
    )


def test_attach_merges_with_existing_artifacts():
    store = get_task_store()
    task = store.create(
        kind="autonomy",
        title="merge test",
        status="running",
        artifacts={
            "mission_scorecard": {"existing": True},
            "unrelated": {"value": 42},
        },
    )

    attach_toy_grid_world_autonomy_artifacts(task["task_id"], _clean_episode(), now=NOW)

    stored = store.get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    # Pre-existing artifacts must survive the merge.
    assert artifacts["mission_scorecard"] == {"existing": True}
    assert artifacts["unrelated"] == {"value": 42}
    # Autonomy artifacts must have been merged in alongside.
    assert "autonomous_episode" in artifacts
    assert "autonomy_gate_result" in artifacts


def test_attach_does_not_change_task_status_or_promotion_state():
    task = _create_task()
    initial_status = task["status"]

    attach_toy_grid_world_autonomy_artifacts(task["task_id"], _clean_episode(), now=NOW)

    stored = get_task_store().get(task["task_id"])
    assert stored is not None
    assert stored["status"] == initial_status
    gate = stored["artifacts"]["autonomy_gate_result"]
    # Read-only contract: gate metadata baked in by the builder must surface
    # promotion_created=false / runtime_reuse_created=false / stronger_execution_allowed=false
    # so this attach path remains visibly read-only at the artifact layer.
    assert gate["metadata"]["promotion_created"] is False
    assert gate["metadata"]["runtime_reuse_created"] is False
    assert gate["metadata"]["stronger_execution_allowed"] is False
    assert gate["operator_approval_required"] is True
    assert gate["operator_approval_performed"] is False
    assert gate["live_execution_allowed"] is False
    assert gate["physical_execution_invoked"] is False


def test_attach_is_deterministic_for_same_inputs():
    first_task = _create_task(title="determinism-1")
    second_task = _create_task(title="determinism-2")
    episode = _clean_episode()
    baseline_gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode, now=NOW
    )

    first = attach_toy_grid_world_autonomy_artifacts(
        first_task["task_id"], episode, baseline_gate=baseline_gate, now=NOW
    )
    second = attach_toy_grid_world_autonomy_artifacts(
        second_task["task_id"], episode, baseline_gate=baseline_gate, now=NOW
    )

    assert first["autonomy_gate_result"]["gate_id"] == second["autonomy_gate_result"]["gate_id"]
    assert (
        first["autonomy_gate_comparison_result"]["comparison_id"]
        == second["autonomy_gate_comparison_result"]["comparison_id"]
    )
    assert (
        first["autonomy_gate_result"]["blocked_reasons"]
        == second["autonomy_gate_result"]["blocked_reasons"]
    )


def test_attach_raises_when_task_does_not_exist():
    with pytest.raises(AutonomyArtifactAttachError):
        attach_toy_grid_world_autonomy_artifacts(
            "task_does_not_exist", _clean_episode(), now=NOW
        )


def test_attach_accepts_episode_dict_payload():
    task = _create_task()
    episode_payload = _clean_episode().model_dump(mode="json")

    artifacts = attach_toy_grid_world_autonomy_artifacts(
        task["task_id"],
        episode_payload,
        now=NOW,
    )

    assert artifacts["autonomous_episode"]["episode_id"] == episode_payload["episode_id"]


def test_attach_rejects_invalid_episode_type():
    task = _create_task()
    with pytest.raises(AutonomyArtifactAttachError):
        attach_toy_grid_world_autonomy_artifacts(
            task["task_id"],
            "not-an-episode",  # type: ignore[arg-type]
            now=NOW,
        )
