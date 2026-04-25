from datetime import datetime, timezone

from src.runtime.mission_evals import (
    MISSION_EVAL_RESULT_SCHEMA_VERSION,
    MISSION_REGRESSION_GATE_SCHEMA_VERSION,
    compare_mission_eval_results,
    get_mission_eval_suite,
    list_mission_eval_suites,
    run_mission_eval_suite,
)
from src.runtime.mission_runtime import build_post_mission_review_artifacts
from src.runtime.mission_templates import build_mission_contract_from_template

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _base_durable_execution(
    *,
    contract=None,
    node_status="done",
    verdict="pass",
    failure_type="",
    evidence_refs=None,
    recovery_decisions=None,
    escalations=None,
):
    resolved_contract = contract or build_mission_contract_from_template(
        "observation_review",
        {"target": "the dashboard"},
    )
    verdict_payload = {
        "schema_version": "durable_verifier_verdict.v1",
        "verdict": verdict,
        "failure_type": failure_type,
        "evidence_refs": evidence_refs or ["artifact:visible_state"],
    }
    return {
        "mission_contract": resolved_contract.model_dump(mode="json"),
        "task_graph": {
            "graph_id": "mission-eval/graph",
            "nodes": [
                {
                    "node_id": "mission-eval/observe",
                    "status": node_status,
                    "scheduler_queue": (
                        "completed" if node_status == "done" else node_status
                    ),
                    "verifier_verdict": verdict_payload,
                }
            ],
        },
        "scheduler_state": {"completed_queue": []},
        "job_runs": [
            {
                "run_id": "mission-eval/run-1",
                "node_id": "mission-eval/observe",
                "status": "completed" if verdict == "pass" else "failed",
                "verifier_verdict": verdict_payload,
            }
        ],
        "verifier_verdicts": [verdict_payload],
        "recovery_decisions": recovery_decisions or [],
        "escalations": escalations or [],
    }


def _artifacts(durable_execution, *, final_status="completed"):
    return build_post_mission_review_artifacts(
        durable_execution,
        final_status=final_status,
        source_task_id=f"task-{final_status}",
        child_task_ids=["task-child"],
        created_at=NOW,
    )


def _completed_artifacts():
    return _artifacts(_base_durable_execution(), final_status="completed")


def _weak_evidence_artifacts():
    contract = build_mission_contract_from_template(
        "weak_evidence_probe",
        {"target": "the current tab"},
    )
    durable = _base_durable_execution(
        contract=contract,
        node_status="blocked",
        verdict="uncertain",
        failure_type="weak_evidence",
        recovery_decisions=[
            {
                "schema_version": "recovery_decision.v1",
                "failure_type": "weak_evidence",
                "selected_step": "request_approval",
                "reason": "weak evidence requires operator review",
                "attempt_index": 1,
                "budget_before": {"pending_approvals": 0},
                "budget_after": {"pending_approvals": 1},
                "outcome": "paused",
                "source_refs": ["verifier:weak_evidence"],
            }
        ],
        escalations=[
            {
                "schema_version": "durable_escalation.v1",
                "escalation_id": "esc-1",
                "approval_request_id": "approval-1",
            }
        ],
    )
    return _artifacts(durable, final_status="paused")


def _blocked_artifacts():
    contract = build_mission_contract_from_template(
        "budget_exhaustion_probe",
        {"target": "the current tab"},
    )
    durable = _base_durable_execution(
        contract=contract,
        node_status="blocked",
        verdict="fail",
        failure_type="tool_failure",
        recovery_decisions=[
            {
                "schema_version": "recovery_decision.v1",
                "failure_type": "tool_failure",
                "selected_step": "pause_or_block",
                "reason": "retry budget exhausted",
                "attempt_index": 1,
                "budget_before": {"max_same_failure_retries": 0},
                "budget_after": {"max_same_failure_retries": 0},
                "budget_exhausted": True,
                "budget_exhausted_reasons": ["max_same_failure_retries_exhausted"],
                "outcome": "blocked",
                "source_refs": ["tool:failure"],
            }
        ],
    )
    return _artifacts(durable, final_status="blocked")


def test_list_and_get_mission_eval_suites():
    suite_ids = {suite["suite_id"] for suite in list_mission_eval_suites()}

    assert suite_ids == {
        "approval_required_action",
        "blocked_state_correctness",
        "budget_exhaustion_probe",
        "control_ui_mission_panel_smoke",
        "memory_candidate_approval_boundary",
        "mission_review_artifact_shape",
        "template_contract_generation",
        "weak_evidence_probe",
    }
    assert get_mission_eval_suite("weak_evidence_probe").security_sensitive is False


def test_target_eval_can_pass_against_mission_artifacts():
    result = run_mission_eval_suite(
        "mission_review_artifact_shape",
        _completed_artifacts(),
        subject_id="baseline",
        created_at=NOW,
    )

    assert result.schema_version == MISSION_EVAL_RESULT_SCHEMA_VERSION
    assert result.passed is True
    assert result.metrics["artifact_shape_compatible"] == 1.0
    assert result.metrics["mission_completion_rate"] == 1.0
    assert "artifact:mission_review" in result.artifact_refs


def test_weak_evidence_eval_buckets_uncertain_evidence():
    result = run_mission_eval_suite(
        "weak_evidence_probe",
        _weak_evidence_artifacts(),
        subject_id="weak",
    )

    assert result.passed is True
    assert result.metrics["approval_correctness"] == 1.0
    assert result.metrics["artifact_shape_compatible"] == 1.0


def test_blocked_state_eval_passes_for_budget_exhaustion():
    result = run_mission_eval_suite(
        "blocked_state_correctness",
        _blocked_artifacts(),
        subject_id="blocked",
    )

    assert result.passed is True
    assert result.metrics["blocked_correctness"] == 1.0


def test_memory_candidate_boundary_keeps_candidates_candidate_only():
    result = run_mission_eval_suite(
        "memory_candidate_approval_boundary",
        _weak_evidence_artifacts(),
        subject_id="memory-boundary",
    )

    assert result.passed is True
    assert result.metrics["memory_reuse_precision"] == 1.0
    assert result.metrics["security_eval_pass_rate"] == 1.0


def test_memory_candidate_boundary_fails_when_pending_candidate_is_reused():
    artifacts = _weak_evidence_artifacts()
    candidate_id = artifacts["memory_promotion_candidates"][0]["candidate_id"]
    artifacts["reuse_plan"] = {"selected_memories": [{"candidate_id": candidate_id}]}

    result = run_mission_eval_suite(
        "memory_candidate_approval_boundary",
        artifacts,
        subject_id="memory-boundary-broken",
    )

    assert result.passed is False
    assert "non_reusable_memory_candidate_reused" in result.failures
    assert "security_eval_failed" in result.failures
    assert result.metrics["security_eval_pass_rate"] == 0.0


def test_memory_candidate_boundary_allows_approved_candidate_artifact_without_reuse():
    artifacts = _weak_evidence_artifacts()
    candidate = artifacts["memory_promotion_candidates"][0]
    candidate["approval_status"] = "approved"
    candidate["approved_by"] = "operator"
    candidate["approved_at"] = "2026-01-01T00:00:00Z"

    result = run_mission_eval_suite(
        "memory_candidate_approval_boundary",
        artifacts,
        subject_id="approved-artifact-only",
    )

    assert result.passed is True
    assert result.metrics["memory_reuse_precision"] == 1.0
    assert result.metrics["security_eval_pass_rate"] == 1.0


def test_memory_candidate_boundary_fails_when_approved_candidate_lacks_metadata():
    artifacts = _weak_evidence_artifacts()
    candidate = artifacts["memory_promotion_candidates"][0]
    candidate["approval_status"] = "approved"
    candidate.pop("approved_by", None)
    candidate.pop("approved_at", None)

    result = run_mission_eval_suite(
        "memory_candidate_approval_boundary",
        artifacts,
        subject_id="approved-missing-metadata",
    )

    assert result.passed is False
    assert "approved_memory_candidate_missing_approved_by" in result.failures
    assert "approved_memory_candidate_missing_approved_at" in result.failures


def test_memory_candidate_boundary_fails_when_rejected_or_expired_candidate_is_reused():
    for status in ("rejected", "expired"):
        artifacts = _weak_evidence_artifacts()
        candidate = artifacts["memory_promotion_candidates"][0]
        candidate["approval_status"] = status
        artifacts["reuse_plan"] = {
            "selected_memories": [{"candidate_id": candidate["candidate_id"]}]
        }

        result = run_mission_eval_suite(
            "memory_candidate_approval_boundary",
            artifacts,
            subject_id=f"{status}-reused",
        )

        assert result.passed is False
        assert "non_reusable_memory_candidate_reused" in result.failures
        assert "security_eval_failed" in result.failures
        assert result.metrics["security_eval_pass_rate"] == 0.0


def test_template_contract_generation_eval_passes_for_template_contract():
    contract = build_mission_contract_from_template(
        "current_tab_research_to_report",
        {"topic": "mission evals", "report_target": "reports/evals.md"},
    )
    artifacts = _artifacts(
        _base_durable_execution(contract=contract),
        final_status="completed",
    )

    result = run_mission_eval_suite("template_contract_generation", artifacts)

    assert result.passed is True
    assert result.artifact_refs


def test_missing_artifact_shape_blocks_gate():
    baseline = run_mission_eval_suite(
        "mission_review_artifact_shape",
        _completed_artifacts(),
        subject_id="baseline",
    )
    candidate = run_mission_eval_suite(
        "mission_review_artifact_shape",
        {"mission_scorecard": _completed_artifacts()["mission_scorecard"]},
        subject_id="candidate",
    )
    gate = compare_mission_eval_results(baseline, candidate)

    assert candidate.passed is False
    assert candidate.metrics["artifact_shape_compatible"] == 0.0
    assert gate.schema_version == MISSION_REGRESSION_GATE_SCHEMA_VERSION
    assert gate.passed is False
    assert "artifact_shape_incompatible" in gate.blocked_reasons
    assert "regression_failure" in gate.blocked_reasons
    assert gate.requires_operator_approval is True


def test_regression_failure_blocks_promotion_gate():
    baseline = run_mission_eval_suite(
        "mission_review_artifact_shape",
        _completed_artifacts(),
        subject_id="baseline",
    )
    degraded_artifacts = _artifacts(
        _base_durable_execution(
            node_status="blocked",
            verdict="fail",
            failure_type="tool_failure",
        ),
        final_status="failed",
    )
    candidate = run_mission_eval_suite(
        "mission_review_artifact_shape",
        degraded_artifacts,
        subject_id="candidate",
    )
    gate = compare_mission_eval_results(baseline, candidate)

    assert gate.passed is False
    assert "metric_regressed:mission_completion_rate" in gate.blocked_reasons
    assert "metric_regressed:verification_pass_rate" in gate.blocked_reasons


def test_security_eval_failure_blocks_promotion_gate():
    baseline = run_mission_eval_suite(
        "approval_required_action",
        _weak_evidence_artifacts(),
        subject_id="baseline",
    )
    candidate = run_mission_eval_suite(
        "approval_required_action",
        _completed_artifacts(),
        subject_id="candidate",
    )
    gate = compare_mission_eval_results(baseline, candidate)

    assert baseline.passed is True
    assert candidate.passed is False
    assert "security_eval_failed" in gate.blocked_reasons
    assert gate.passed is False


def test_gate_passes_but_still_requires_operator_approval():
    baseline = run_mission_eval_suite(
        "template_contract_generation",
        _completed_artifacts(),
        subject_id="baseline",
    )
    candidate = run_mission_eval_suite(
        "template_contract_generation",
        _completed_artifacts(),
        subject_id="candidate",
    )
    gate = compare_mission_eval_results(baseline, candidate)

    assert gate.passed is True
    assert gate.blocked_reasons == []
    assert gate.requires_operator_approval is True
    assert gate.model_dump(mode="json")["candidate_result"]["schema_version"] == (
        "mission_eval_result.v1"
    )
