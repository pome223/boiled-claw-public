import json
from pathlib import Path
import sqlite3

from click.testing import CliRunner

from src.computer_use.trajectory_store import ComputerTrajectoryStore
from src.evals.failure_taxonomy import normalize_trajectory_failure
from src.evals import runtime as eval_runtime
from src.main import cli
from src.runtime.task_store import TaskStore
from src.runtime.task_store import get_task_store
from src.tools.self_improvement_runtime.promotion import REUSE_MEMORY_KINDS


class _StubMemoryStore:
    def __init__(self, items):
        self._items = items

    def search(self, query=None, kinds=None, limit=10):
        assert query is None
        assert kinds == list(REUSE_MEMORY_KINDS)
        return list(self._items)[:limit]


def test_normalize_trajectory_failure_prefers_target_context_mismatch_for_url_failures():
    classification = normalize_trajectory_failure(
        {
            "status": "failed",
            "verification": {
                "status": "fail",
                "checks": [
                    {
                        "name": "url_contains",
                        "expected": "docs.google.com/spreadsheets",
                        "actual": "https://example.com",
                        "passed": False,
                    }
                ],
            },
        },
        classified_by="verifier",
    )

    assert classification["preliminary_failure_type"] == "target_context_mismatch"
    assert classification["normalized_failure_type"] == "target_context_mismatch"
    assert classification["failure_type"] == "target_context_mismatch"
    assert classification["classified_by"] == ["verifier"]


def test_normalize_trajectory_failure_detects_wrong_surface_from_preferred_surface():
    classification = normalize_trajectory_failure(
        {
            "status": "failed",
            "final_surface": "desktop",
            "observation": {
                "preferred_surface": "current_tab",
                "available_surfaces": ["current_tab", "desktop"],
            },
            "attempts": [
                {
                    "surface": "desktop",
                    "result": {
                        "error": "preferred surface current_tab but final surface desktop",
                    },
                }
            ],
            "verification": {
                "status": "fail",
                "checks": [
                    {
                        "name": "surface",
                        "expected": "current_tab",
                        "actual": "desktop",
                        "passed": False,
                    }
                ],
            },
        },
        classified_by="replay_analysis",
    )

    assert classification["preliminary_failure_type"] == "wrong_surface"
    assert classification["normalized_failure_type"] == "wrong_surface"
    assert classification["failure_type"] == "wrong_surface"
    assert classification["classified_by"] == ["replay_analysis"]


def test_run_eval_spec_persists_report_and_backfills_failure_classification(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    trajectory_id = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com",
                    "passed": False,
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
surfaces:
  - current_tab
runs: 5
failure_buckets:
  - weak_evidence
  - focus_mismatch
  - target_context_mismatch
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - failed
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    stub_memory = _StubMemoryStore(
        [
            {
                "id": 7,
                "content": "Use destination-bound verification before typing into Sheets.",
                "kind": "approved_improvement",
                "metadata": {
                    "trajectory_key": "fill::current_tab::.cell-input",
                    "selector": ".cell-input",
                    "surface": "current_tab",
                },
                "tags": ["sheets"],
                "created_at": 123.0,
            }
        ]
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: stub_memory)
    assert store.update_reuse_trace(
        trajectory_id,
        reuse_trace={
            "source": "self_improvement_demo",
            "memory_ids": [7],
            "used_memory_ids": [7],
            "policy": {"enabled": True, "source": "default"},
        },
    )

    result = eval_runtime.run_eval_spec(spec_path)

    assert result["success"] is True
    assert result["runs_evaluated"] == 1
    assert result["failure_buckets"]["target_context_mismatch"] == 1
    assert result["slice"]["type"] == "bounded_long_running"
    assert "candidate_promotion_artifacts" in result["slice"]["expected_artifacts"]
    assert "task_graph" in result["slice"]["expected_artifacts"]
    assert "checkpoint" in result["slice"]["expected_artifacts"]
    assert "verifier_verdict" in result["slice"]["expected_artifacts"]
    assert result["run_jobs"][0]["run_job_id"] == "current_tab_google_sheets_phase0/run-1"
    assert result["reports"][0]["trajectory_id"] == trajectory_id
    assert result["reports"][0]["failure_type"] == "target_context_mismatch"
    assert result["reports"][0]["verifier_result"]["status"] == "fail"
    assert result["reports"][0]["verifier_verdict"]["verdict"] == "fail"
    assert result["reports"][0]["verifier_verdict"]["confidence_source"] == "synthetic_default"
    assert result["reports"][0]["candidate_promotion_artifacts"] == [
        "approved_improvement_memory",
        "capability_patch",
    ]
    assert result["reports"][0]["replay_reference"]["trajectory_id"] == trajectory_id
    assert result["reports"][0]["reuse_suggestions"][0]["memory_id"] == 7
    assert result["reports"][0]["reuse_memory_ids"] == [7]
    assert result["reports"][0]["reuse_policy"]["enabled"] is True
    assert result["reports"][0]["reuse_trace"]["used_memory_ids"] == [7]
    assert result["reports"][0]["task_node"]["status"] == "failed"
    assert result["reports"][0]["scheduler_queue"] == "retry_later"
    assert result["reports"][0]["recovery_policy"]["failure_type"] == "target_context_mismatch"
    assert result["reports"][0]["recovery_decision"]["chosen_action"] == "switch_surface"
    assert result["reports"][0]["checkpoint"]["next_actionable_task_node_id"] is None
    assert result["reports"][0]["budget_state"]["policy"]["max_same_failure_retries"] == 3
    assert result["reports"][0]["job_run"]["status"] == "failed"
    assert result["reports"][0]["job_run"]["scheduler_queue"] == "retry_later"
    assert result["durable_execution"]["task_graph"]["nodes"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert result["durable_execution"]["scheduler_state"]["retry_later_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert "tool_timeout" in result["durable_execution"]["recovery_policies"]
    assert result["durable_execution"]["resume_state"]["scheduler_queue_counts"]["retry_later"] == 1
    assert result["durable_execution"]["resume_state"]["next_actionable_task_node_id"] is None
    assert result["durable_execution"]["resume_state"]["reason"] == "awaiting_unblock_or_human_input"

    persisted = store.get(trajectory_id)
    assert persisted["normalized_failure_type"] == "target_context_mismatch"
    assert persisted["classified_by"] == ["replay_analysis"]
    assert persisted["reuse_trace"]["used_memory_ids"] == [7]

    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["status"] == "completed"
    assert task["metadata"]["eval_id"] == "current_tab_google_sheets_phase0"
    assert task["artifacts"]["report"]["reports"][0]["failure_type"] == "target_context_mismatch"
    assert task["artifacts"]["report"]["run_jobs"][0]["run_job_id"] == "current_tab_google_sheets_phase0/run-1"
    assert task["artifacts"]["report"]["durable_execution"]["task_graph"]["graph_id"] == (
        "current_tab_google_sheets_phase0/task-graph"
    )


def test_run_eval_spec_marks_weak_evidence_as_uncertain_verdict(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    trajectory_id = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "partial_pass",
            "success": False,
            "checks": [
                {
                    "name": "text_contains",
                    "expected": "saved",
                    "actual": "",
                    "passed": False,
                    "evidence_refs": ["post_action.png"],
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
runs: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - failed
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    result = eval_runtime.run_eval_spec(spec_path)

    assert result["success"] is True
    assert result["reports"][0]["trajectory_id"] == trajectory_id
    assert result["reports"][0]["failure_type"] == "weak_evidence"
    assert result["reports"][0]["verifier_verdict"]["verdict"] == "uncertain"
    assert result["reports"][0]["verifier_verdict"]["confidence_source"] == "synthetic_default"
    assert result["reports"][0]["scheduler_queue"] == "waiting_for_approval"
    assert result["reports"][0]["recovery_decision"]["chosen_action"] == "request_human_approval"
    assert result["reports"][0]["escalation_record"]["status"] == "waiting_for_approval"
    assert result["reports"][0]["task_node"]["status"] == "blocked"
    assert result["reports"][0]["checkpoint"]["blocked_task_node_ids"] == [
        "current_tab_google_sheets_phase0/run-1"
    ]
    assert result["reports"][0]["checkpoint"]["pending_approval_ids"] == [
        "approval:current_tab_google_sheets_phase0/run-1"
    ]
    assert result["durable_execution"]["scheduler_state"]["waiting_for_approval_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert result["durable_execution"]["resume_state"]["scheduler_queue_counts"]["waiting_for_approval"] == 1
    assert result["durable_execution"]["resume_state"]["next_actionable_task_node_id"] is None
    assert result["durable_execution"]["resume_state"]["reason"] == "awaiting_approval"


def test_run_eval_spec_respects_reuse_policy_disable(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    trajectory_id = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification={"status": "fail", "success": False},
        request={
            "selector": ".cell-input",
            "policy": {"allow_approved_improvement_reuse": False},
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    class _FailingMemoryStore:
        def search(self, **kwargs):
            raise AssertionError("prefilter search should not run when reuse is disabled by policy")

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
runs: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - failed
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _FailingMemoryStore())

    result = eval_runtime.run_eval_spec(spec_path)

    assert result["success"] is True
    assert result["reports"][0]["trajectory_id"] == trajectory_id
    assert result["reports"][0]["reuse_suggestions"] == []
    assert result["reports"][0]["reuse_memory_ids"] == []
    assert result["reports"][0]["reuse_policy"]["enabled"] is False
    assert result["reports"][0]["reuse_policy"]["source"] == "request.policy.allow_approved_improvement_reuse"


def test_override_trajectory_failure_type_applies_and_clears_operator_override(tmp_path):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    trajectory_id = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com",
                    "passed": False,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    applied = eval_runtime.override_trajectory_failure_type(
        trajectory_id,
        failure_type="focus_mismatch",
        get_trajectory_store_fn=lambda: store,
    )

    assert applied["success"] is True
    assert applied["trajectory"]["operator_override"] == "focus_mismatch"
    assert applied["trajectory"]["normalized_failure_type"] == "focus_mismatch"
    assert "operator" in applied["trajectory"]["classified_by"]

    cleared = eval_runtime.override_trajectory_failure_type(
        trajectory_id,
        failure_type=None,
        get_trajectory_store_fn=lambda: store,
    )

    assert cleared["success"] is True
    assert cleared["trajectory"]["operator_override"] is None
    assert cleared["trajectory"]["normalized_failure_type"] == "target_context_mismatch"
    assert "operator" not in cleared["trajectory"]["classified_by"]


def test_get_eval_report_can_compare_against_baseline(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    baseline_trajectory = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com",
                    "passed": False,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    current_trajectory = store.record(
        action="fill",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "pass",
            "success": True,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://docs.google.com/spreadsheets/d/1",
                    "passed": True,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
runs: 1
failure_buckets:
  - weak_evidence
  - focus_mismatch
  - target_context_mismatch
match:
  action: fill
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    baseline = eval_runtime.run_eval_spec(
        spec_path,
        trajectory_id=baseline_trajectory,
    )
    current = eval_runtime.run_eval_spec(
        spec_path,
        trajectory_id=current_trajectory,
    )
    report = eval_runtime.get_eval_report(
        task_id=current["task_id"],
        compare_to_task_id=baseline["task_id"],
    )

    assert report["success"] is True
    assert report["comparison"]["success_rate"]["current"] == 1.0
    assert report["comparison"]["success_rate"]["baseline"] == 0.0
    assert report["comparison"]["success_rate"]["delta"] == 1.0
    assert report["comparison"]["failure_buckets"]["target_context_mismatch"]["delta"] == -1
    assert "target_context_mismatch" in report["comparison"]["improved_buckets"]


def test_get_eval_report_prefers_latest_terminal_task_over_running_or_updated_older_run(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))

    def _create_eval_task(task_id: str, status: str, *, created_at: float, updated_at: float) -> None:
        task = store.create(
            task_id=task_id,
            kind="eval_run",
            title=f"{status} eval",
            status=status,
            owner_session_id="session-1",
            owner_user_id="user-1",
            metadata={"eval_id": "current_tab_google_sheets_phase0"},
            artifacts={
                "spec": {"id": "current_tab_google_sheets_phase0"},
                "report": {
                    "task_id": task_id,
                    "status": status,
                    "created_at": created_at,
                },
            },
        )
        assert task["task_id"] == task_id
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET created_at = ?, updated_at = ?, started_at = ?, ended_at = ?
                WHERE task_id = ?
                """,
                (
                    created_at,
                    updated_at,
                    created_at if status in {"accepted", "running", "idle"} else None,
                    updated_at if status in {"completed", "failed", "cancelled", "expired"} else None,
                    task_id,
                ),
            )
            conn.commit()

    _create_eval_task(
        "task_eval_completed_older",
        "completed",
        created_at=100.0,
        updated_at=300.0,
    )
    _create_eval_task(
        "task_eval_completed_latest",
        "completed",
        created_at=200.0,
        updated_at=200.0,
    )
    _create_eval_task(
        "task_eval_running_newest",
        "running",
        created_at=400.0,
        updated_at=400.0,
    )

    report = eval_runtime.get_eval_report(
        eval_id="current_tab_google_sheets_phase0",
        get_task_store_fn=lambda: store,
    )

    assert report["success"] is True
    assert report["task_id"] == "task_eval_completed_latest"
    assert report["status"] == "completed"
    assert report["report"]["task_id"] == "task_eval_completed_latest"


def test_run_eval_spec_blocks_when_guardrail_budget_is_exhausted(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    first_trajectory = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com",
                    "passed": False,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    second_trajectory = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com/same-failure",
                    "passed": False,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
runs: 2
budget:
  max_same_failure_retries: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - failed
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    result = eval_runtime.run_eval_spec(spec_path)

    assert result["success"] is True
    assert [item["trajectory_id"] for item in result["reports"]] == [second_trajectory, first_trajectory]
    assert result["reports"][0]["scheduler_queue"] == "retry_later"
    assert result["reports"][0]["budget_state"]["budget_exhausted"] is False
    assert result["reports"][1]["scheduler_queue"] == "blocked"
    assert result["reports"][1]["recovery_decision"]["budget_exhausted"] is True
    assert result["reports"][1]["recovery_decision"]["budget_exhausted_reasons"] == [
        "max_same_failure_retries_exhausted"
    ]
    assert result["durable_execution"]["scheduler_state"]["blocked_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-2"
    )


def test_run_eval_spec_uses_checked_in_google_sheets_long_running_vertical_slice(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    success_trajectory = store.record(
        action="fill",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "pass",
            "success": True,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://docs.google.com/spreadsheets/d/1",
                    "passed": True,
                },
                {
                    "name": "text_contains",
                    "expected": "Summary",
                    "actual": "Summary row written",
                    "passed": True,
                    "evidence_refs": ["sheet-success.png"],
                },
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {"url_contains": "docs.google.com/spreadsheets"},
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab", "desktop"]},
    )
    wrong_surface_trajectory = store.record(
        action="fill",
        status="failed",
        final_surface="desktop",
        attempts=[
            {
                "surface": "desktop",
                "strategy": "desktop_ax",
                "result": {
                    "error": "preferred surface current_tab but final surface desktop",
                },
            }
        ],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "surface",
                    "expected": "current_tab",
                    "actual": "desktop",
                    "passed": False,
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {"url_contains": "docs.google.com/spreadsheets"},
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab", "desktop"]},
    )
    weak_evidence_trajectory = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "partial_pass",
            "success": False,
            "checks": [
                {
                    "name": "text_contains",
                    "expected": "Summary",
                    "actual": "",
                    "passed": False,
                    "evidence_refs": ["sheet-weak-evidence.png"],
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {"url_contains": "docs.google.com/spreadsheets"},
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab", "desktop"]},
    )

    spec_path = Path(__file__).resolve().parents[1] / "evals" / "current_tab_google_sheets.yaml"

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    result = eval_runtime.run_eval_spec(spec_path, limit=3)

    assert result["success"] is True
    assert result["slice"]["type"] == "long_running_vertical_slice"
    assert result["runs_evaluated"] == 3
    assert result["success_rate"] == 0.3333
    assert result["failure_buckets"]["weak_evidence"] == 1
    assert result["failure_buckets"]["wrong_surface"] == 1
    assert result["failure_buckets"]["focus_mismatch"] == 0
    assert result["failure_buckets"]["target_context_mismatch"] == 0
    assert [item["trajectory_id"] for item in result["run_jobs"]] == [
        weak_evidence_trajectory,
        wrong_surface_trajectory,
        success_trajectory,
    ]
    assert result["run_jobs"][0]["failure_type"] == "weak_evidence"
    assert result["run_jobs"][0]["scheduler_queue"] == "waiting_for_approval"
    assert result["run_jobs"][0]["recommended_repair_targets"][0] == (
        "strengthen destination-bound verifier"
    )
    assert result["run_jobs"][1]["failure_type"] == "wrong_surface"
    assert result["run_jobs"][1]["scheduler_queue"] == "retry_later"
    assert result["run_jobs"][1]["recovery_decision"]["chosen_action"] == "switch_surface"
    assert result["run_jobs"][1]["recommended_repair_targets"][0] == (
        "rebind the task to the intended execution surface before acting"
    )
    assert result["run_jobs"][1]["checkpoint"]["trajectory_ids"] == [wrong_surface_trajectory]
    assert result["run_jobs"][1]["checkpoint"]["replay_references"][0]["trajectory_id"] == (
        wrong_surface_trajectory
    )
    assert result["run_jobs"][2]["trajectory_id"] == success_trajectory
    assert result["run_jobs"][2]["job_run"]["status"] == "completed"
    assert result["durable_execution"]["scheduler_state"]["waiting_for_approval_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert result["durable_execution"]["scheduler_state"]["retry_later_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-2"
    )
    assert result["durable_execution"]["scheduler_state"]["completed_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-3"
    )
    assert result["durable_execution"]["resume_state"]["reason"] == "awaiting_approval"


def test_eval_cli_run_and_report_use_persisted_task(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    store.record(
        action="fill",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "pass",
            "success": True,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://docs.google.com/spreadsheets/d/1",
                    "passed": True,
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
surfaces:
  - current_tab
runs: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    runner = CliRunner()
    run = runner.invoke(cli, ["eval", "run", str(spec_path)])

    assert run.exit_code == 0
    run_payload = json.loads(run.output)
    assert run_payload["success"] is True
    assert run_payload["success_rate"] == 1.0

    report = runner.invoke(
        cli,
        ["eval", "report", "--task-id", run_payload["task_id"]],
    )

    assert report.exit_code == 0
    report_payload = json.loads(report.output)
    assert report_payload["success"] is True
    assert report_payload["report"]["eval_id"] == "current_tab_google_sheets_phase0"
    assert report_payload["report"]["run_jobs"][0]["verifier_result"]["status"] == "pass"


def test_eval_cli_run_and_report_emit_durable_execution_artifacts_end_to_end(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "partial_pass",
            "success": False,
            "checks": [
                {
                    "name": "text_contains",
                    "expected": "saved",
                    "actual": "",
                    "passed": False,
                    "evidence_refs": ["post_action.png"],
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
surfaces:
  - current_tab
runs: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - failed
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    runner = CliRunner()
    run = runner.invoke(cli, ["eval", "run", str(spec_path)])

    assert run.exit_code == 0
    run_payload = json.loads(run.output)
    assert run_payload["success"] is True
    assert run_payload["run_jobs"][0]["verifier_verdict"]["verdict"] == "uncertain"
    assert run_payload["run_jobs"][0]["verifier_verdict"]["confidence_source"] == "synthetic_default"
    assert run_payload["run_jobs"][0]["scheduler_queue"] == "waiting_for_approval"
    assert run_payload["run_jobs"][0]["recovery_decision"]["chosen_action"] == "request_human_approval"
    assert run_payload["run_jobs"][0]["budget_state"]["pending_approvals_count"] == 1
    assert run_payload["run_jobs"][0]["task_node"]["status"] == "blocked"
    assert run_payload["run_jobs"][0]["job_run"]["status"] == "blocked"
    assert run_payload["run_jobs"][0]["job_run"]["scheduler_queue"] == "waiting_for_approval"
    assert run_payload["run_jobs"][0]["checkpoint"]["blocked_task_node_ids"] == [
        "current_tab_google_sheets_phase0/run-1"
    ]
    assert run_payload["run_jobs"][0]["checkpoint"]["pending_approval_ids"] == [
        "approval:current_tab_google_sheets_phase0/run-1"
    ]
    assert run_payload["durable_execution"]["task_graph"]["graph_id"] == (
        "current_tab_google_sheets_phase0/task-graph"
    )
    assert run_payload["durable_execution"]["scheduler_state"]["waiting_for_approval_queue"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert run_payload["durable_execution"]["escalations"][0]["approval_request_id"] == (
        "approval:current_tab_google_sheets_phase0/run-1"
    )
    assert run_payload["durable_execution"]["resume_state"]["reason"] == (
        "awaiting_approval"
    )

    report = runner.invoke(
        cli,
        ["eval", "report", "--task-id", run_payload["task_id"]],
    )

    assert report.exit_code == 0
    report_payload = json.loads(report.output)
    assert report_payload["success"] is True
    assert report_payload["report"]["run_jobs"][0]["verifier_verdict"]["verdict"] == "uncertain"
    assert report_payload["report"]["run_jobs"][0]["verifier_verdict"]["confidence_source"] == "synthetic_default"
    assert report_payload["report"]["run_jobs"][0]["checkpoint"]["checkpoint_id"] == (
        "current_tab_google_sheets_phase0/run-1/checkpoint"
    )
    assert report_payload["report"]["run_jobs"][0]["scheduler_queue"] == "waiting_for_approval"
    assert report_payload["report"]["durable_execution"]["resume_state"]["reason"] == (
        "awaiting_approval"
    )


def test_eval_cli_report_by_eval_id_prefers_latest_completed_terminal_run_end_to_end(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    first_trajectory = store.record(
        action="fill",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "pass",
            "success": True,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://docs.google.com/spreadsheets/d/1",
                    "passed": True,
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    second_trajectory = store.record(
        action="fill",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "pass",
            "success": True,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://docs.google.com/spreadsheets/d/2",
                    "passed": True,
                }
            ],
        },
        request={
            "selector": ".cell-input",
            "verify": {
                "url_contains": "docs.google.com/spreadsheets",
            },
        },
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    spec_path = tmp_path / "current_tab_google_sheets.yaml"
    spec_path.write_text(
        """
id: current_tab_google_sheets_phase0
goal: "Write into Google Sheets"
runs: 1
match:
  action: fill
  final_surface_any:
    - current_tab
  status_any:
    - success
  request:
    verify:
      url_contains: "docs.google.com/spreadsheets"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    task_store = TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)
    monkeypatch.setattr(eval_runtime, "get_task_store", lambda: task_store)
    monkeypatch.setattr(eval_runtime, "get_memory_store", lambda: _StubMemoryStore([]))

    runner = CliRunner()
    first_run = runner.invoke(cli, ["eval", "run", str(spec_path), "--trajectory-id", str(first_trajectory)])
    assert first_run.exit_code == 0
    first_payload = json.loads(first_run.output)

    second_run = runner.invoke(cli, ["eval", "run", str(spec_path), "--trajectory-id", str(second_trajectory)])
    assert second_run.exit_code == 0
    second_payload = json.loads(second_run.output)

    with sqlite3.connect(task_store.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (9999.0, first_payload["task_id"]),
        )
        conn.commit()

    running_task = task_store.create(
        task_id="task_eval_running_newest",
        kind="eval_run",
        title="running eval",
        status="running",
        owner_session_id="session-1",
        owner_user_id="user-1",
        metadata={"eval_id": "current_tab_google_sheets_phase0"},
        artifacts={
            "spec": {"id": "current_tab_google_sheets_phase0"},
            "report": {"task_id": "task_eval_running_newest", "status": "running"},
        },
    )
    assert running_task["task_id"] == "task_eval_running_newest"

    report = runner.invoke(
        cli,
        ["eval", "report", "--eval-id", "current_tab_google_sheets_phase0"],
    )

    assert report.exit_code == 0
    report_payload = json.loads(report.output)
    assert report_payload["success"] is True
    assert report_payload["task_id"] == second_payload["task_id"]
    assert report_payload["status"] == "completed"
    assert report_payload["report"]["run_jobs"][0]["trajectory_id"] == second_trajectory
    assert report_payload["report"]["run_jobs"][0]["status"] == "success"


def test_eval_cli_classify_updates_operator_override(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    trajectory_id = store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={
            "status": "fail",
            "success": False,
            "checks": [
                {
                    "name": "url_contains",
                    "expected": "docs.google.com/spreadsheets",
                    "actual": "https://example.com",
                    "passed": False,
                }
            ],
        },
        request={"verify": {"url_contains": "docs.google.com/spreadsheets"}},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    monkeypatch.setattr(eval_runtime, "get_computer_trajectory_store", lambda: store)

    runner = CliRunner()
    classify = runner.invoke(
        cli,
        ["eval", "classify", "--trajectory-id", str(trajectory_id), "--failure-type", "focus_mismatch"],
    )

    assert classify.exit_code == 0
    payload = json.loads(classify.output)
    assert payload["success"] is True
    assert payload["trajectory"]["operator_override"] == "focus_mismatch"
