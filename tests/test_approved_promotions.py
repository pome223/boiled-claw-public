from datetime import datetime, timedelta, timezone

import pytest

from src.runtime.approved_promotions import (
    APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION,
    APPROVED_SKILL_SCHEMA_VERSION,
    CAPABILITY_PATCH_SCHEMA_VERSION,
    POLICY_PATCH_SCHEMA_VERSION,
    ApprovedPromotionError,
    ApprovedPromotionTarget,
    build_approved_promotion_artifact,
    default_approved_promotion_target,
    group_approved_promotion_artifacts_by_type,
    is_approved_promotion_artifact_usable,
    is_promotion_package_approval_ready,
    list_approved_promotion_artifacts,
    reject_approved_promotion_artifact,
)
from src.runtime.mission_promotion import MissionPromotionPackage

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _eval_result(suite_id: str, *, subject_id: str, passed: bool = True):
    return {
        "schema_version": "mission_eval_result.v1",
        "suite_id": suite_id,
        "subject_id": subject_id,
        "passed": passed,
        "metrics": {
            "artifact_shape_compatible": 1.0 if passed else 0.0,
            "security_eval_pass_rate": 1.0 if passed else 0.0,
        },
        "failures": [] if passed else ["eval_failed"],
        "artifact_refs": [f"artifact:{suite_id}"],
        "created_at": NOW.isoformat(),
    }


def _ready_package(
    *,
    candidate_type: str,
    package_id: str,
    required_suites: list[str] | None = None,
    security_eval: bool = False,
) -> MissionPromotionPackage:
    suites = required_suites or ["memory_candidate_approval_boundary"]
    return MissionPromotionPackage(
        package_id=package_id,
        candidate_id=f"{package_id}:candidate",
        candidate_type=candidate_type,
        source_mission_task_id="task-1",
        source_refs=["mission_review:task-1"],
        required_eval_suites=suites,
        evaluated_suite_ids=suites + (["approval_required_action"] if security_eval else []),
        unevaluated_required_suites=[],
        eval_suite_id=suites[0],
        baseline_eval_result=_eval_result(suites[0], subject_id="baseline"),
        candidate_eval_result=_eval_result(suites[0], subject_id="candidate"),
        regression_gate_result={
            "schema_version": "mission_regression_gate.v1",
            "baseline_result": "baseline",
            "candidate_result": "candidate",
            "passed": True,
            "blocked_reasons": [],
            "metric_deltas": {},
            "requires_operator_approval": True,
        },
        security_eval_result=(
            _eval_result("approval_required_action", subject_id="security")
            if security_eval
            else None
        ),
        recommendation="pending_operator_approval",
        approval_status="pending",
        requires_operator_approval=True,
        created_at=NOW,
        metadata={
            "source_candidate": {
                "summary": f"Promote {candidate_type}",
                "failure_type": "weak_evidence",
            }
        },
    )


def _approve(package: MissionPromotionPackage, **kwargs):
    return build_approved_promotion_artifact(
        package,
        approved_by="operator",
        approval_ref="approval:1",
        approved_at=NOW,
        **kwargs,
    )


def test_approved_improvement_memory_artifact_has_dedicated_schema():
    package = _ready_package(
        candidate_type="benchmark_case",
        package_id="pkg-memory",
    )

    artifact = _approve(
        package,
        expires_at=NOW + timedelta(days=30),
        invalidation_rule="Invalidate when the benchmark case is superseded.",
    )
    payload = artifact.model_dump(mode="json")

    assert payload["schema_version"] == APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION
    assert payload["promotion_target"] == "approved_improvement_memory"
    assert payload["memory_kind"] == "approved_improvement"
    assert payload["approval_status"] == "approved"
    assert payload["approved_by"] == "operator"
    assert payload["approval_ref"] == "approval:1"
    assert payload["failure_type"] == "weak_evidence"
    assert payload["benchmark_refs"]
    assert is_approved_promotion_artifact_usable(payload, now=NOW)


def test_verifier_and_memory_rule_default_to_memory_not_policy_patch():
    verifier_package = _ready_package(
        candidate_type="verifier_improvement",
        package_id="pkg-verifier-default",
        required_suites=["weak_evidence_probe", "mission_review_artifact_shape"],
    )
    memory_rule_package = _ready_package(
        candidate_type="memory_rule",
        package_id="pkg-memory-rule-default",
    )

    verifier = _approve(verifier_package)
    memory_rule = _approve(memory_rule_package)

    assert verifier.promotion_target == ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY
    assert memory_rule.promotion_target == ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY


def test_approved_skill_artifact_has_recipe_schema():
    package = _ready_package(
        candidate_type="recovery_strategy",
        package_id="pkg-skill",
        required_suites=["blocked_state_correctness", "budget_exhaustion_probe"],
    )

    artifact = _approve(package)
    payload = artifact.model_dump(mode="json")

    assert payload["schema_version"] == APPROVED_SKILL_SCHEMA_VERSION
    assert payload["promotion_target"] == "approved_skill"
    assert payload["skill_name"].startswith("approved/")
    assert "mission_contract" in payload["inputs"]
    assert payload["procedure"]
    assert payload["approval_requirements"]["requires_benchmark_gate"] is True


def test_capability_patch_artifact_requires_security_eval_metadata():
    package = _ready_package(
        candidate_type="capability_patch",
        package_id="pkg-capability",
        required_suites=["approval_required_action", "mission_review_artifact_shape"],
        security_eval=True,
    )

    artifact = _approve(package)
    payload = artifact.model_dump(mode="json")

    assert payload["schema_version"] == CAPABILITY_PATCH_SCHEMA_VERSION
    assert payload["promotion_target"] == "capability_patch"
    assert payload["registration_required"] is True
    assert payload["capability_name"].startswith("approved.")
    assert payload["approval_requirements"]["requires_security_eval"] is True
    assert payload["security_refs"]


def test_policy_patch_artifact_has_policy_schema():
    package = _ready_package(
        candidate_type="policy_patch",
        package_id="pkg-policy",
        required_suites=["approval_required_action", "mission_review_artifact_shape"],
        security_eval=True,
    )

    artifact = _approve(package)
    payload = artifact.model_dump(mode="json")

    assert payload["schema_version"] == POLICY_PATCH_SCHEMA_VERSION
    assert payload["promotion_target"] == "policy_patch"
    assert payload["policy_id"].startswith("policy-")
    assert payload["enforcement_scope"] == "mission_promotion"
    assert "operator approval is required before reuse" in payload["rules"]


def test_policy_patch_target_requires_security_eval_for_non_security_candidate_type():
    package = _ready_package(
        candidate_type="verifier_improvement",
        package_id="pkg-verifier-policy-no-security",
        required_suites=["weak_evidence_probe", "mission_review_artifact_shape"],
        security_eval=False,
    )

    assert is_promotion_package_approval_ready(package) is True
    assert (
        is_promotion_package_approval_ready(
            package,
            promotion_target=ApprovedPromotionTarget.POLICY_PATCH,
        )
        is False
    )
    with pytest.raises(ApprovedPromotionError, match="requires a passing security eval"):
        _approve(
            package,
            promotion_target=ApprovedPromotionTarget.POLICY_PATCH,
        )


def test_policy_patch_target_allows_non_security_candidate_when_security_eval_passed():
    package = _ready_package(
        candidate_type="verifier_improvement",
        package_id="pkg-verifier-policy-security",
        required_suites=["weak_evidence_probe", "mission_review_artifact_shape"],
        security_eval=True,
    )

    artifact = _approve(
        package,
        promotion_target=ApprovedPromotionTarget.POLICY_PATCH,
    )

    assert artifact.promotion_target == ApprovedPromotionTarget.POLICY_PATCH
    assert artifact.security_refs
    assert artifact.approval_requirements["requires_security_eval"] is True


def test_capability_patch_candidate_requires_security_eval_result():
    package = _ready_package(
        candidate_type="capability_patch",
        package_id="pkg-capability-no-security",
        required_suites=["approval_required_action", "mission_review_artifact_shape"],
        security_eval=False,
    )

    assert not is_promotion_package_approval_ready(package)
    with pytest.raises(ApprovedPromotionError, match="requires a passing security eval"):
        _approve(package)


def test_blocked_package_cannot_be_approved():
    package = _ready_package(
        candidate_type="memory_rule",
        package_id="pkg-blocked",
    ).model_copy(
        update={
            "recommendation": "blocked_by_regression_gate",
            "regression_gate_result": {
                "schema_version": "mission_regression_gate.v1",
                "passed": False,
                "blocked_reasons": ["candidate_eval_failed"],
            },
        }
    )

    assert not is_promotion_package_approval_ready(package)
    with pytest.raises(ApprovedPromotionError, match="not approval-ready"):
        _approve(package)


def test_operator_approval_ref_is_required_before_creating_artifact():
    package = _ready_package(
        candidate_type="benchmark_case",
        package_id="pkg-approval",
    )

    with pytest.raises(ApprovedPromotionError, match="approved_by is required"):
        build_approved_promotion_artifact(
            package,
            approved_by="",
            approval_ref="approval:1",
            approved_at=NOW,
        )
    with pytest.raises(ApprovedPromotionError, match="approval_ref is required"):
        build_approved_promotion_artifact(
            package,
            approved_by="operator",
            approval_ref="",
            approved_at=NOW,
        )


def test_code_patch_has_no_approved_runtime_target_in_this_layer():
    package = _ready_package(
        candidate_type="code_patch",
        package_id="pkg-code",
        required_suites=["approval_required_action", "mission_review_artifact_shape"],
        security_eval=True,
    )

    with pytest.raises(ApprovedPromotionError, match="no approved promotion target"):
        default_approved_promotion_target("code_patch")
    with pytest.raises(ApprovedPromotionError, match="no approved promotion target"):
        _approve(package)


def test_list_helpers_filter_by_type_and_ignore_expired_or_rejected_artifacts():
    memory = _approve(
        _ready_package(candidate_type="benchmark_case", package_id="pkg-memory-list"),
        expires_at=NOW + timedelta(days=1),
    )
    expired = memory.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    rejected = reject_approved_promotion_artifact(
        memory,
        rejected_reason="operator rejected after review",
    )
    skill = _approve(
        _ready_package(
            candidate_type="skill_recipe",
            package_id="pkg-skill-list",
            required_suites=["template_contract_generation", "mission_review_artifact_shape"],
        )
    )
    artifacts = {
        "approved_promotions": [
            memory.model_dump(mode="json"),
            expired.model_dump(mode="json"),
            rejected.model_dump(mode="json"),
            skill.model_dump(mode="json"),
        ]
    }

    assert not is_approved_promotion_artifact_usable(expired, now=NOW)
    assert not is_approved_promotion_artifact_usable(rejected, now=NOW)
    all_usable = list_approved_promotion_artifacts(artifacts, now=NOW)
    memory_only = list_approved_promotion_artifacts(
        artifacts,
        promotion_target=ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
        now=NOW,
    )
    grouped = group_approved_promotion_artifacts_by_type(artifacts, now=NOW)

    assert [item.artifact_id for item in all_usable] == [
        memory.artifact_id,
        skill.artifact_id,
    ]
    assert [item.artifact_id for item in memory_only] == [memory.artifact_id]
    assert grouped["approved_improvement_memory"][0]["artifact_id"] == memory.artifact_id
    assert grouped["approved_skill"][0]["artifact_id"] == skill.artifact_id
    assert grouped["capability_patch"] == []
    assert grouped["policy_patch"] == []


def test_approved_artifacts_are_not_runtime_reuse_plans():
    artifact = _approve(
        _ready_package(candidate_type="benchmark_case", package_id="pkg-inert"),
    ).model_dump(mode="json")
    artifacts = {"approved_promotions": [artifact]}

    assert list_approved_promotion_artifacts(artifacts, now=NOW)
    assert "reuse_plan" not in artifacts
    assert "selected_memories" not in artifacts
