"""Tests for cross-session task analytics."""

import sqlite3

from src.gateway.task_analytics import (
    compute_analytics,
    compute_replay_improvement,
    compute_step_failure_ranking,
    compute_task_overview,
)
from src.runtime import task_store as task_store_module


def _make_store(tmp_path):
    db_path = tmp_path / "tasks.db"
    return task_store_module.TaskStore(str(db_path))


def _seed_control_loop_task(store, *, title, status="completed", step_trace=None, metadata=None):
    artifacts = {}
    if step_trace is not None:
        artifacts["result"] = {"step_trace": step_trace}
    return store.create(
        kind="control_loop",
        title=title,
        status=status,
        owner_session_id="sess-1",
        owner_user_id="alice",
        artifacts=artifacts,
        metadata=metadata,
    )


def test_overview_counts_tasks_by_status(tmp_path):
    store = _make_store(tmp_path)
    _seed_control_loop_task(store, title="Task A", status="completed")
    _seed_control_loop_task(store, title="Task B", status="failed")
    _seed_control_loop_task(store, title="Task C", status="failed")

    overview = compute_task_overview(store)

    assert overview["total_tasks"] == 3
    assert overview["by_status"]["completed"] == 1
    assert overview["by_status"]["failed"] == 2
    assert overview["total_replays"] == 0


def test_overview_counts_replay_success_rate(tmp_path):
    store = _make_store(tmp_path)
    source = _seed_control_loop_task(store, title="Source", status="failed")
    _seed_control_loop_task(
        store,
        title="Replay OK",
        status="completed",
        metadata={"replay_of_task_id": source["task_id"]},
    )
    _seed_control_loop_task(
        store,
        title="Replay Fail",
        status="failed",
        metadata={"replay_of_task_id": source["task_id"]},
    )

    overview = compute_task_overview(store)

    assert overview["total_replays"] == 2
    assert overview["replay_success_rate"] == 0.5


def test_step_failure_ranking_from_step_events(tmp_path):
    store = _make_store(tmp_path)
    task = _seed_control_loop_task(store, title="Desktop playback", status="failed")
    store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="launch_djay",
        status="running",
        payload={
            "summary": "launched",
            "step": {
                "step_id": "launch_djay",
                "title": "Launch Djay",
                "step_type": "plan",
                "status": "succeeded",
                "failed_criteria": [],
            },
        },
    )
    store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="verify_visual_state",
        status="running",
        payload={
            "summary": "evidence thin",
            "step": {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "step_type": "plan",
                "status": "failed",
                "failed_criteria": ["music_playing", "eq_visible"],
            },
        },
    )

    result = compute_step_failure_ranking(store)
    ranking = result["steps"]

    assert len(ranking) == 2
    assert result["total_events"] >= 2
    assert result["truncated"] is False
    verify_step = next(s for s in ranking if s["step_id"] == "verify_visual_state")
    launch_step = next(s for s in ranking if s["step_id"] == "launch_djay")
    assert verify_step["failed"] == 1
    assert verify_step["failure_rate"] == 1.0
    assert verify_step["top_failed_criteria"][0]["name"] == "music_playing"
    assert launch_step["succeeded"] == 1
    assert launch_step["failure_rate"] == 0.0
    assert ranking[0]["step_id"] == "verify_visual_state"


def test_step_failure_ranking_skips_verification_and_repair_steps(tmp_path):
    store = _make_store(tmp_path)
    task = _seed_control_loop_task(store, title="Test", status="completed")
    store.append_event(
        task["task_id"],
        event_type="step_verification",
        title="Verification",
        payload={
            "step": {
                "step_id": "__verification__",
                "title": "Verification",
                "step_type": "verification",
                "status": "pass",
            },
        },
    )
    store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="real_step",
        payload={
            "step": {
                "step_id": "real_step",
                "title": "Real Step",
                "step_type": "plan",
                "status": "succeeded",
            },
        },
    )

    result = compute_step_failure_ranking(store)
    ranking = result["steps"]

    assert len(ranking) == 1
    assert ranking[0]["step_id"] == "real_step"


def test_replay_improvement_compares_source_and_replay_step_traces(tmp_path):
    store = _make_store(tmp_path)
    source = _seed_control_loop_task(
        store,
        title="Source run",
        status="failed",
        step_trace=[
            {
                "step_id": "launch_djay",
                "title": "Launch Djay",
                "step_type": "plan",
                "status": "succeeded",
                "failed_criteria": [],
            },
            {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "step_type": "plan",
                "status": "failed",
                "failed_criteria": ["music_playing"],
            },
        ],
    )
    _seed_control_loop_task(
        store,
        title="Replay run",
        status="completed",
        step_trace=[
            {
                "step_id": "launch_djay",
                "title": "Launch Djay",
                "step_type": "plan",
                "status": "succeeded",
                "failed_criteria": [],
            },
            {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "step_type": "plan",
                "status": "succeeded",
                "failed_criteria": [],
            },
        ],
        metadata={"replay_of_task_id": source["task_id"]},
    )

    result = compute_replay_improvement(store)
    improvement = result["steps"]

    assert len(improvement) == 1
    assert result["total_replay_tasks"] == 1
    assert result["truncated"] is False
    verify = improvement[0]
    assert verify["step_id"] == "verify_visual_state"
    assert verify["source_fail"] == 1
    assert verify["replay_pass"] == 1
    assert verify["improvement_rate"] == 1.0


def test_compute_analytics_returns_all_sections(tmp_path):
    store = _make_store(tmp_path)
    task = _seed_control_loop_task(store, title="Test", status="completed")
    store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="step_a",
        payload={
            "step": {
                "step_id": "step_a",
                "title": "Step A",
                "step_type": "plan",
                "status": "succeeded",
            },
        },
    )

    result = compute_analytics(store)

    assert "overview" in result
    assert "step_failure_ranking" in result
    assert "replay_improvement" in result
    assert result["overview"]["total_tasks"] == 1


def test_step_failure_ranking_empty_store(tmp_path):
    store = _make_store(tmp_path)

    result = compute_step_failure_ranking(store)

    assert result["steps"] == []
    assert result["total_events"] == 0
    assert result["truncated"] is False


def test_overview_uses_sql_aggregation_beyond_page_limit(tmp_path):
    store = _make_store(tmp_path)
    for i in range(105):
        status = "completed" if i % 2 == 0 else "failed"
        _seed_control_loop_task(store, title=f"Task {i}", status=status)

    overview = compute_task_overview(store)

    assert overview["total_tasks"] == 105
    assert overview["by_status"]["completed"] + overview["by_status"]["failed"] == 105


def test_step_failure_ranking_reports_truncation(tmp_path):
    store = _make_store(tmp_path)
    task = _seed_control_loop_task(store, title="Test", status="completed")
    for i in range(5):
        store.append_event(
            task["task_id"],
            event_type="step_plan",
            title=f"step_{i}",
            payload={
                "step": {
                    "step_id": f"step_{i}",
                    "title": f"Step {i}",
                    "step_type": "plan",
                    "status": "succeeded",
                },
            },
        )

    result = compute_step_failure_ranking(store, limit=3)

    assert result["total_events"] >= 5
    assert result["sampled_events"] == 3
    assert result["truncated"] is True


def test_replay_improvement_no_replay_pairs(tmp_path):
    store = _make_store(tmp_path)
    _seed_control_loop_task(store, title="Solo task", status="completed")

    result = compute_replay_improvement(store)

    assert result["steps"] == []
    assert result["total_replay_tasks"] == 0
    assert result["truncated"] is False
