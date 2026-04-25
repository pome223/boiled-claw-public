from datetime import datetime, timezone

import pytest

from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.mission_promotion import (
    PROMOTION_PACKAGE_SCHEMA_VERSION,
    MissionPromotionCandidateType,
    MissionPromotionError,
    build_promotion_package,
    is_security_sensitive_candidate_type,
    normalize_promotion_candidate,
    promotion_candidates_from_mission_review,
    required_eval_suites_for_candidate_type,
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
        "evidence_refs": ["artifact:visible_state"],
    }
    return {
        "mission_contract": resolved_contract.model_dump(mode="json"),
        "task_graph": {
            "graph_id": "promotion/graph",
            "nodes": [
                {
                    "node_id": "promotion/observe",
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
                "run_id": "promotion/run-1",
                "node_id": "promotion/observe",
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


def _first_improvement_candidate():
    review = _weak_evidence_artifacts()["mission_review"]
    candidates = promotion_candidates_from_mission_review(review)
    assert candidates
    return candidates[0]


def _memory_rule_candidate():
    return {
        "candidate_id": "task-paused:improvement:memory-rule",
        "candidate_type": "memory_rule",
        "summary": "Tighten memory candidate approval boundary.",
        "source_artifact_ref": "mission_review.improvement_candidates",
        "source_mission_task_id": "task-paused",
    }


def _artifacts_with_non_reusable_memory_selected():
    artifacts = _weak_evidence_artifacts()
    candidate_id = artifacts["memory_promotion_candidates"][0]["candidate_id"]
    artifacts["reuse_plan"] = {"selected_memories": [{"candidate_id": candidate_id}]}
    return artifacts


def test_promotion_package_can_be_built_from_verifier_improvement_candidate():
    artifacts = _weak_evidence_artifacts()
    candidate = _first_improvement_candidate()

    package = build_promotion_package(
        candidate,
        baseline_artifacts=artifacts,
        candidate_artifacts=artifacts,
        created_at=NOW,
    )
    payload = package.model_dump(mode="json")

    assert payload["schema_version"] == PROMOTION_PACKAGE_SCHEMA_VERSION
    assert payload["candidate_type"] == "verifier_improvement"
    assert payload["approval_status"] == "pending"
    assert payload["requires_operator_approval"] is True
    assert payload["recommendation"] == "blocked_by_missing_required_eval"
    assert payload["regression_gate_result"]["passed"] is False
    assert payload["baseline_eval_result"]["suite_id"] == "weak_evidence_probe"
    assert payload["candidate_eval_result"]["suite_id"] == "weak_evidence_probe"
    assert payload["required_eval_suites"] == [
        "weak_evidence_probe",
        "mission_review_artifact_shape",
    ]
    assert payload["evaluated_suite_ids"] == ["weak_evidence_probe"]
    assert payload["unevaluated_required_suites"] == ["mission_review_artifact_shape"]
    assert "missing_required_eval:mission_review_artifact_shape" in (
        payload["regression_gate_result"]["blocked_reasons"]
    )
    assert "mission_review:task-paused" in payload["source_refs"]


def test_package_blocks_when_required_eval_suite_is_not_evaluated():
    artifacts = _weak_evidence_artifacts()
    candidate = _first_improvement_candidate()

    package = build_promotion_package(
        candidate,
        baseline_artifacts=artifacts,
        candidate_artifacts=artifacts,
        eval_suite_id="weak_evidence_probe",
    )

    assert package.recommendation == "blocked_by_missing_required_eval"
    assert package.unevaluated_required_suites == ["mission_review_artifact_shape"]
    assert package.metadata["unevaluated_required_suites"] == [
        "mission_review_artifact_shape"
    ]


def test_failed_regression_gate_blocks_package_recommendation():
    baseline_artifacts = _weak_evidence_artifacts()

    package = build_promotion_package(
        _memory_rule_candidate(),
        baseline_artifacts=baseline_artifacts,
        candidate_artifacts=_artifacts_with_non_reusable_memory_selected(),
    )

    assert package.recommendation == "blocked_by_regression_gate"
    assert package.regression_gate_result["passed"] is False
    assert "candidate_eval_failed" in package.regression_gate_result["blocked_reasons"]


def test_security_eval_failure_blocks_package_recommendation():
    baseline = run_mission_eval_suite(
        "mission_review_artifact_shape",
        _completed_artifacts(),
        subject_id="baseline",
    )
    candidate_eval = run_mission_eval_suite(
        "mission_review_artifact_shape",
        _completed_artifacts(),
        subject_id="candidate",
    )
    security_eval = run_mission_eval_suite(
        "approval_required_action",
        _completed_artifacts(),
        subject_id="security",
    )

    package = build_promotion_package(
        {
            "candidate_id": "task-1:improvement:policy",
            "candidate_type": "policy_patch",
            "summary": "Tighten approval policy.",
            "source_artifact_ref": "mission_review.improvement_candidates",
        },
        baseline_eval_result=baseline,
        candidate_eval_result=candidate_eval,
        security_eval_result=security_eval,
        eval_suite_id="mission_review_artifact_shape",
    )

    assert package.recommendation == "blocked_by_security_eval"
    assert package.regression_gate_result["passed"] is False
    assert "security_eval_failed" in package.regression_gate_result["blocked_reasons"]
    assert package.security_eval_result is not None
    assert package.security_eval_result["passed"] is False


def test_passing_eval_still_requires_operator_approval():
    artifacts = _weak_evidence_artifacts()
    package = build_promotion_package(
        _memory_rule_candidate(),
        baseline_artifacts=artifacts,
        candidate_artifacts=artifacts,
    )

    assert package.regression_gate_result["passed"] is True
    assert package.unevaluated_required_suites == []
    assert package.requires_operator_approval is True
    assert package.approval_status == "pending"


def test_unknown_candidate_type_fails_safely():
    with pytest.raises(MissionPromotionError):
        normalize_promotion_candidate(
            {
                "candidate_id": "candidate-unknown",
                "candidate_type": "diagnostic_task",
                "summary": "Unsupported legacy candidate.",
                "source_artifact_ref": "mission_review.improvement_candidates",
            }
        )


def test_security_sensitive_candidate_type_mapping():
    for candidate_type in (
        MissionPromotionCandidateType.CODE_PATCH.value,
        MissionPromotionCandidateType.CAPABILITY_PATCH.value,
        MissionPromotionCandidateType.POLICY_PATCH.value,
    ):
        assert is_security_sensitive_candidate_type(candidate_type) is True
        assert (
            "approval_required_action"
            in required_eval_suites_for_candidate_type(candidate_type)
        )

    assert is_security_sensitive_candidate_type("verifier_improvement") is False


def test_post_mission_review_artifacts_do_not_consume_promotion_packages():
    artifacts = _weak_evidence_artifacts()

    assert "mission_review" in artifacts
    assert "promotion_package" not in artifacts
    assert "promotion_packages" not in artifacts
