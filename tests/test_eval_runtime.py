import json
from pathlib import Path

from click.testing import CliRunner

from src.computer_use.trajectory_store import ComputerTrajectoryStore
from src.evals.failure_taxonomy import normalize_trajectory_failure
from src.evals import runtime as eval_runtime
from src.main import cli
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
    assert result["reports"][0]["task_node"]["status"] == "failed"
    assert result["reports"][0]["checkpoint"]["next_actionable_task_node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert result["reports"][0]["job_run"]["status"] == "failed"
    assert result["durable_execution"]["task_graph"]["nodes"][0]["node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )
    assert result["durable_execution"]["resume_state"]["next_actionable_task_node_id"] == (
        "current_tab_google_sheets_phase0/run-1"
    )

    persisted = store.get(trajectory_id)
    assert persisted["normalized_failure_type"] == "target_context_mismatch"
    assert persisted["classified_by"] == ["replay_analysis"]

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
    assert result["reports"][0]["task_node"]["status"] == "blocked"
    assert result["reports"][0]["checkpoint"]["blocked_task_node_ids"] == [
        "current_tab_google_sheets_phase0/run-1"
    ]
    assert result["durable_execution"]["resume_state"]["next_actionable_task_node_id"] is None


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
    assert run_payload["run_jobs"][0]["task_node"]["status"] == "blocked"
    assert run_payload["run_jobs"][0]["job_run"]["status"] == "blocked"
    assert run_payload["run_jobs"][0]["checkpoint"]["blocked_task_node_ids"] == [
        "current_tab_google_sheets_phase0/run-1"
    ]
    assert run_payload["durable_execution"]["task_graph"]["graph_id"] == (
        "current_tab_google_sheets_phase0/task-graph"
    )
    assert run_payload["durable_execution"]["resume_state"]["reason"] == (
        "awaiting_unblock_or_human_input"
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
    assert report_payload["report"]["durable_execution"]["resume_state"]["reason"] == (
        "awaiting_unblock_or_human_input"
    )


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
