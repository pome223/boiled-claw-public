from datetime import datetime, timedelta, timezone

from src.runtime.approved_promotions import (
    ApprovedImprovementMemoryArtifact,
    ApprovedPromotionStatus,
    ApprovedPromotionTarget,
    ApprovedSkillArtifact,
    CapabilityPatchArtifact,
    PolicyPatchArtifact,
)
from src.runtime.mission_reuse import (
    MISSION_REUSE_PLAN_SCHEMA_VERSION,
    build_mission_reuse_plan,
)
from src.runtime.mission_templates import build_mission_contract_from_template

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _contract():
    return build_mission_contract_from_template(
        "current_tab_research_to_report",
        {
            "topic": "browser agent Sheets evidence",
            "report_target": "local report",
        },
    )


def _base_payload(*, artifact_id: str, target: ApprovedPromotionTarget, content: str):
    return {
        "artifact_id": artifact_id,
        "source_package_id": f"{artifact_id}:package",
        "candidate_id": f"{artifact_id}:candidate",
        "candidate_type": "benchmark_case",
        "source_mission_task_id": "source-task",
        "source_refs": ["mission_review:source-task"],
        "approval_status": ApprovedPromotionStatus.APPROVED,
        "approved_by": "operator",
        "approved_at": NOW,
        "approval_ref": "approval:1",
        "invalidation_rule": "Invalidate when benchmark evidence is stale.",
        "approval_requirements": {
            "requires_operator_approval": True,
            "requires_benchmark_gate": True,
        },
        "benchmark_refs": ["mission_eval_result:template_contract_generation:baseline"],
        "security_refs": [],
        "content": content,
        "promotion_target": target,
    }


def _memory(artifact_id="memory-sheet"):
    return ApprovedImprovementMemoryArtifact(
        **_base_payload(
            artifact_id=artifact_id,
            target=ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
            content="Use destination-bound Sheets evidence when writing browser agent reports.",
        ),
        memory_kind="approved_improvement",
        failure_type="weak_evidence",
    )


def _skill(artifact_id="skill-report"):
    return ApprovedSkillArtifact(
        **_base_payload(
            artifact_id=artifact_id,
            target=ApprovedPromotionTarget.APPROVED_SKILL,
            content="Reusable report writing recipe for source-linked browser research.",
        ),
        skill_name="approved/source-linked-report",
        procedure=["collect sources", "write report", "verify evidence"],
        inputs=["mission_contract", "verifier_verdict"],
    )


def _capability(artifact_id="capability-sheet"):
    return CapabilityPatchArtifact(
        **_base_payload(
            artifact_id=artifact_id,
            target=ApprovedPromotionTarget.CAPABILITY_PATCH,
            content="Capability proposal for destination-bound sheet evidence capture.",
        ),
        capability_name="approved.destination_bound_sheet_evidence",
        action_schema={"requires_destination_bound_verification": True},
    )


def _policy(artifact_id="policy-evidence"):
    return PolicyPatchArtifact(
        **_base_payload(
            artifact_id=artifact_id,
            target=ApprovedPromotionTarget.POLICY_PATCH,
            content="Policy requires source-linked report evidence before completion.",
        ),
        policy_id="policy-source-linked-report",
        rules=["require source-linked report evidence"],
    )


def test_build_mission_reuse_plan_selects_approved_artifacts_by_type():
    plan = build_mission_reuse_plan(
        _contract(),
        {
            "approved_promotions": [
                _memory().model_dump(mode="json"),
                _skill().model_dump(mode="json"),
                _capability().model_dump(mode="json"),
                _policy().model_dump(mode="json"),
            ]
        },
        mission_task_id="mission-1",
        now=NOW,
    )
    payload = plan.model_dump(mode="json")

    assert payload["schema_version"] == MISSION_REUSE_PLAN_SCHEMA_VERSION
    assert payload["mission_task_id"] == "mission-1"
    assert payload["operator_visible"] is True
    assert payload["selected_memories"][0]["artifact_id"] == "memory-sheet"
    assert payload["selected_skills"][0]["artifact_id"] == "skill-report"
    assert payload["selected_capabilities"][0]["artifact_id"] == "capability-sheet"
    assert payload["selected_policies"][0]["artifact_id"] == "policy-evidence"
    assert payload["selection_reasons"]
    assert all(
        item["application_mode"] == "operator_visible_plan_only"
        for item in payload["selected_policies"]
    )
    assert payload["metadata"]["automatic_runtime_application"] is False


def test_reuse_plan_excludes_pending_memory_candidates_and_promotion_packages():
    plan = build_mission_reuse_plan(
        _contract(),
        {
            "memory_promotion_candidates": [
                {
                    "candidate_id": "pending-memory",
                    "approval_status": "pending",
                    "content": "Sheets evidence should be reused",
                }
            ],
            "promotion_packages": [
                {
                    "package_id": "pending-package",
                    "approval_status": "pending",
                    "candidate_id": "candidate-package",
                }
            ],
        },
        now=NOW,
    )

    assert not plan.selected_memories
    reasons = {item.reason for item in plan.excluded_candidates}
    assert reasons == {"not_approved_promotion_artifact"}
    assert {item.artifact_id for item in plan.excluded_candidates} == {
        "pending-memory",
        "pending-package",
    }


def test_reuse_plan_excludes_expired_rejected_and_invalidated_artifacts():
    expired = _memory("memory-expired").model_copy(
        update={"expires_at": NOW - timedelta(seconds=1)}
    )
    rejected = _skill("skill-rejected").model_copy(
        update={"approval_status": ApprovedPromotionStatus.REJECTED}
    )
    invalidated = _policy("policy-invalidated").model_copy(
        update={"metadata": {"invalidation": {"triggered": True}}}
    )

    plan = build_mission_reuse_plan(
        _contract(),
        [
            expired.model_dump(mode="json"),
            rejected.model_dump(mode="json"),
            invalidated.model_dump(mode="json"),
        ],
        now=NOW,
    )

    assert not plan.selected_memories
    assert not plan.selected_skills
    assert not plan.selected_policies
    by_id = {item.artifact_id: item.reason for item in plan.excluded_candidates}
    assert by_id["memory-expired"] == "expired"
    assert by_id["skill-rejected"] == "rejected"
    assert by_id["policy-invalidated"] == "invalidation_triggered"
    assert any(check.check == "expiry" for check in plan.expiry_checks)
    assert any(check.check == "invalidation_rule" for check in plan.policy_checks)


def test_reuse_plan_excludes_approved_artifact_without_contract_match():
    unrelated = ApprovedImprovementMemoryArtifact(
        **_base_payload(
            artifact_id="memory-unrelated",
            target=ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
            content="Invoice reconciliation for warehouse barcode scanning.",
        ),
        memory_kind="approved_improvement",
    )

    plan = build_mission_reuse_plan(
        _contract(),
        {"approved_promotions": [unrelated.model_dump(mode="json")]},
        now=NOW,
    )

    assert not plan.selected_memories
    assert plan.excluded_candidates[0].reason == "no_contract_match"


def test_reuse_plan_respects_max_per_type_and_orders_by_relevance():
    high = _memory("memory-high")
    low = ApprovedImprovementMemoryArtifact(
        **_base_payload(
            artifact_id="memory-low",
            target=ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
            content="Report evidence.",
        ),
        memory_kind="approved_improvement",
    )

    plan = build_mission_reuse_plan(
        _contract(),
        [low.model_dump(mode="json"), high.model_dump(mode="json")],
        now=NOW,
        max_per_type=1,
    )

    assert [item.artifact_id for item in plan.selected_memories] == ["memory-high"]
    assert plan.selection_reasons


def test_reuse_plan_clamps_negative_max_per_type_to_zero():
    plan = build_mission_reuse_plan(
        _contract(),
        [_memory("memory-high").model_dump(mode="json")],
        now=NOW,
        max_per_type=-1,
    )

    assert not plan.selected_memories
    assert plan.selection_reasons


def test_reuse_plan_keeps_capabilities_and_policies_operator_visible_only():
    plan = build_mission_reuse_plan(
        _contract(),
        [_capability().model_dump(mode="json"), _policy().model_dump(mode="json")],
        now=NOW,
    )

    assert plan.selected_capabilities
    assert plan.selected_policies
    application_checks = [
        check for check in plan.policy_checks if check.check == "application_mode"
    ]
    assert application_checks
    assert all(
        check.metadata["automatic_application"] is False for check in application_checks
    )
