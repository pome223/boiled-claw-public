from datetime import datetime, timezone

from src.runtime.mission_runtime import (
    approval_ready_memory_promotion_candidates,
    approve_memory_promotion_candidate,
    build_memory_promotion_candidate_artifacts,
    build_memory_promotion_candidates,
    build_mission_memory_links,
    build_mission_review,
    build_mission_scorecard,
    build_post_mission_review_artifacts,
    is_memory_promotion_candidate_approval_ready,
    is_memory_promotion_candidate_approved,
    normalize_memory_promotion_candidate,
    reject_memory_promotion_candidate,
)


def _durable_execution() -> dict:
    return {
        "mission_contract": {
            "schema_version": "mission_contract.v2",
            "contract_id": "mission-scorecard-test",
            "objective": "Keep state healthy",
            "memory_policy": {
                "promote_only": ["failure_pattern"],
                "never_promote": ["raw_transcript", "secret"],
                "require_operator_approval": True,
                "candidate_ttl_seconds": 3600,
            },
            "improvement_policy": {
                "require_benchmark_pass": True,
                "require_human_promotion": True,
            },
        },
        "task_graph": {
            "nodes": [
                {
                    "node_id": "node-a",
                    "status": "done",
                    "retry_count": 1,
                    "scheduler_queue": "completed",
                },
                {
                    "node_id": "node-b",
                    "status": "blocked",
                    "scheduler_queue": "blocked",
                },
            ]
        },
        "job_runs": [
            {
                "run_id": "run-1",
                "verifier_verdict": {
                    "verdict": "fail",
                    "failure_type": "weak_evidence",
                    "evidence_refs": ["verification_report:vr-1"],
                },
                "replay_reference": {"child_task_id": "child-1"},
            },
            {
                "run_id": "run-2",
                "verifier_verdict": {
                    "verdict": "fail",
                    "failure_type": "weak_evidence",
                },
            },
            {
                "run_id": "run-3",
                "verifier_verdict": {"verdict": "pass"},
            },
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "replay_references": [{"child_task_id": "child-1"}],
            }
        ],
        "recovery_decisions": [
            {
                "failure_type": "weak_evidence",
                "selected_step": "verify_state",
                "outcome": "paused",
                "budget_exhausted": False,
                "source_refs": ["task:child-1", "verification_report:vr-1"],
            },
            {
                "failure_type": "weak_evidence",
                "selected_step": "pause_or_block",
                "outcome": "blocked",
                "budget_exhausted": True,
                "source_refs": ["task:child-2"],
            },
        ],
        "escalations": [{"approval_request_id": "approval-1"}],
    }


def test_build_mission_scorecard_summarizes_durable_execution():
    scorecard = build_mission_scorecard(
        _durable_execution(),
        final_status="blocked",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = scorecard.model_dump(mode="json")
    assert payload["schema_version"] == "mission_scorecard.v1"
    assert payload["objective_progress"] == "blocked"
    assert payload["verification_pass_rate"] == 1 / 3
    assert payload["approval_wait_count"] == 1
    assert payload["repeated_failure_count"] == 1
    assert payload["blocked_count"] == 1
    assert payload["improvement_candidate_count"] == 1
    assert payload["memory_promotion_candidate_count"] == 1
    assert payload["last_verifier_verdict"] == "pass"


def test_build_mission_review_emits_expiring_memory_candidates():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    review = build_mission_review(
        _durable_execution(),
        final_status="blocked",
        source_task_id="task-123",
        created_at=now,
    )
    payload = review.model_dump(mode="json")

    assert payload["schema_version"] == "mission_review.v1"
    assert payload["mission_task_id"] == "task-123"
    assert payload["final_status"] == "blocked"
    assert payload["failure_buckets"] == [{"failure_type": "weak_evidence", "count": 2}]
    assert payload["repeated_failure_patterns"] == [
        {"failure_type": "weak_evidence", "count": 2}
    ]
    assert (
        payload["improvement_candidates"][0]["candidate_type"] == "verifier_improvement"
    )
    assert payload["improvement_candidates"][0]["approval_status"] == "candidate_only"
    assert payload["recovery_effectiveness"]["recovery_decision_count"] == 2
    assert payload["recovery_effectiveness"]["selected_step_counts"] == {
        "pause_or_block": 1,
        "verify_state": 1,
    }
    assert payload["recovery_effectiveness"]["recovery_outcome_counts"] == {
        "blocked": 1,
        "paused": 1,
    }
    assert payload["recovery_effectiveness"]["budget_exhausted_count"] == 1
    assert payload["evidence_quality"]["quality"] == "weak"
    assert payload["evidence_quality"]["weak_evidence_count"] == 2
    assert "task:task-123" in payload["source_refs"]
    assert "child_task:child-1" in payload["source_refs"]
    assert "verification_report:vr-1" in payload["source_refs"]
    assert "checkpoint:checkpoint-1" in payload["source_refs"]
    memory_candidate = payload["memory_promotion_candidates"][0]
    assert memory_candidate["schema_version"] == "memory_promotion_candidate.v1"
    assert memory_candidate["type"] == "failure_pattern"
    assert memory_candidate["approval_status"] == "candidate_only"
    assert memory_candidate["source_task_id"] == "task-123"
    assert memory_candidate["last_verified_at"] == "2026-01-01T00:00:00Z"
    assert memory_candidate["expires_at"] == "2026-01-01T01:00:00Z"
    assert memory_candidate["approval_required"] is True

    links = build_mission_memory_links(review)
    assert links["schema_version"] == "mission_memory_links.v1"
    assert (
        links["memory_promotion_candidates"] == payload["memory_promotion_candidates"]
    )

    artifacts = build_memory_promotion_candidate_artifacts(review, now=now)
    assert artifacts[0]["approval_status"] == "pending"
    assert artifacts[0]["approved_by"] == ""
    assert artifacts[0]["approved_at"] is None
    assert artifacts[0]["rejected_reason"] == ""
    assert "task:task-123" in artifacts[0]["source_refs"]
    assert "mission_review:task-123" in artifacts[0]["source_refs"]
    assert "durable_execution.verifier_verdicts" in artifacts[0]["source_refs"]


def test_build_post_mission_review_artifacts_for_completed_and_failed_missions():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    completed = build_post_mission_review_artifacts(
        {
            "mission_contract": {"contract_id": "mission-completed"},
            "task_graph": {"nodes": [{"node_id": "node-a", "status": "done"}]},
            "job_runs": [
                {
                    "run_id": "run-1",
                    "verifier_verdict": {
                        "verdict": "pass",
                        "evidence_refs": ["evidence:pass"],
                    },
                }
            ],
        },
        final_status="completed",
        source_task_id="task-completed",
        child_task_ids=["child-completed"],
        created_at=now,
    )

    assert completed["mission_review"]["final_status"] == "completed"
    assert completed["mission_review"]["mission_task_id"] == "task-completed"
    assert completed["mission_review"]["failure_buckets"] == []
    assert completed["mission_review"]["evidence_quality"]["quality"] == "strong"
    assert "child_task:child-completed" in completed["mission_review"]["source_refs"]

    failed = build_post_mission_review_artifacts(
        {
            "mission_contract": {"contract_id": "mission-failed"},
            "task_graph": {"nodes": [{"node_id": "node-a", "status": "failed"}]},
            "verifier_verdicts": [{"verdict": "fail", "failure_type": "tool_timeout"}],
        },
        final_status="failed",
        source_task_id="task-failed",
        created_at=now,
    )

    assert failed["mission_review"]["final_status"] == "failed"
    assert failed["mission_review"]["failure_buckets"] == [
        {"failure_type": "tool_timeout", "count": 1}
    ]
    assert failed["mission_review"]["improvement_candidates"][0]["approval_status"] == (
        "candidate_only"
    )


def test_build_post_mission_review_artifacts_emits_pending_memory_artifact():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifacts = build_post_mission_review_artifacts(
        _durable_execution(),
        final_status="blocked",
        source_task_id="task-123",
        created_at=now,
    )

    review_candidate = artifacts["mission_review"]["memory_promotion_candidates"][0]
    top_level_candidate = artifacts["memory_promotion_candidates"][0]

    assert review_candidate["approval_status"] == "candidate_only"
    assert top_level_candidate["approval_status"] == "pending"
    assert top_level_candidate["candidate_id"] == review_candidate["candidate_id"]
    assert top_level_candidate["source_task_id"] == "task-123"
    assert (
        artifacts["mission_memory_links"]["memory_promotion_candidates"]
        == artifacts["memory_promotion_candidates"]
    )


def test_build_mission_review_handles_legacy_durable_execution_without_recovery_decisions():
    review = build_mission_review(
        {
            "mission_contract": {"contract_id": "mission-legacy"},
            "task_graph": {"nodes": [{"node_id": "node-a", "status": "blocked"}]},
            "job_runs": [
                {
                    "run_id": "run-legacy",
                    "verifier_verdict": {
                        "verdict": "fail",
                        "failure_type": "weak_evidence",
                    },
                }
            ],
        },
        final_status="blocked",
        source_task_id="task-legacy",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = review.model_dump(mode="json")
    assert payload["failure_buckets"] == [{"failure_type": "weak_evidence", "count": 1}]
    assert payload["recovery_effectiveness"]["recovery_decision_count"] == 0
    assert payload["recovery_effectiveness"]["selected_step_counts"] == {}
    assert payload["evidence_quality"]["failed_without_evidence_count"] == 1
    assert (
        payload["memory_promotion_candidates"][0]["approval_status"] == "candidate_only"
    )


def test_memory_promotion_candidate_approval_and_rejection_helpers():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = build_memory_promotion_candidates(
        _durable_execution(),
        source_task_id="task-123",
        now=now,
    )[0].model_dump(mode="json")

    pending = normalize_memory_promotion_candidate(
        candidate,
        default_status="pending",
        now=now,
    )
    assert pending.approval_status == "pending"
    assert is_memory_promotion_candidate_approval_ready(candidate, now=now)
    assert not is_memory_promotion_candidate_approved(candidate, now=now)

    approved = approve_memory_promotion_candidate(
        candidate,
        approved_by="operator",
        approved_at=now,
    )
    approved_payload = approved.model_dump(mode="json")
    assert approved_payload["approval_status"] == "approved"
    assert approved_payload["approved_by"] == "operator"
    assert approved_payload["approved_at"] == "2026-01-01T00:00:00Z"
    assert approved_payload["rejected_reason"] == ""
    assert is_memory_promotion_candidate_approved(approved_payload, now=now)

    rejected = reject_memory_promotion_candidate(
        candidate,
        rejected_reason="too environment-specific",
        now=now,
    ).model_dump(mode="json")
    assert rejected["approval_status"] == "rejected"
    assert rejected["rejected_reason"] == "too environment-specific"
    assert not is_memory_promotion_candidate_approved(rejected, now=now)


def test_expired_memory_promotion_candidate_is_not_approval_ready():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expired_payload = {
        "candidate_id": "candidate-expired",
        "type": "failure_pattern",
        "content": "Expired candidate",
        "source_task_id": "task-expired",
        "source_artifact_ref": "mission_review.memory_promotion_candidates",
        "confidence": 0.7,
        "last_verified_at": "2025-12-01T00:00:00Z",
        "expires_at": "2025-12-31T00:00:00Z",
        "promotion_reason": "old evidence",
        "invalidation_rule": "expired by time",
        "approval_required": True,
        "approval_status": "pending",
    }

    expired = normalize_memory_promotion_candidate(
        expired_payload,
        default_status="pending",
        now=now,
    )
    assert expired.approval_status == "expired"
    assert not is_memory_promotion_candidate_approval_ready(expired_payload, now=now)
    assert (
        approval_ready_memory_promotion_candidates(
            [expired_payload],
            now=now,
        )
        == []
    )


def test_approved_but_expired_memory_candidate_is_not_approved_for_use():
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)
    approved_expired = {
        "candidate_id": "candidate-approved-expired",
        "type": "failure_pattern",
        "content": "Expired approved candidate",
        "source_task_id": "task-approved-expired",
        "source_artifact_ref": "mission_review.memory_promotion_candidates",
        "confidence": 0.8,
        "last_verified_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
        "promotion_reason": "previously approved evidence",
        "invalidation_rule": "expired by time",
        "approval_required": True,
        "approval_status": "approved",
        "approved_by": "operator",
        "approved_at": "2026-01-01T00:00:00Z",
    }

    normalized = normalize_memory_promotion_candidate(
        approved_expired,
        now=now,
    )

    assert normalized.approval_status == "approved"
    assert not is_memory_promotion_candidate_approved(
        approved_expired,
        now=now,
    )
    assert is_memory_promotion_candidate_approved(
        approved_expired,
        now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )


def test_legacy_candidate_only_memory_candidate_remains_readable():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy = {
        "candidate_id": "legacy-candidate",
        "type": "failure_pattern",
        "content": "Legacy candidate-only shape",
        "source_artifact_ref": "durable_execution.verifier_verdicts",
        "confidence": 0.5,
        "last_verified_at": "2026-01-01T00:00:00Z",
        "promotion_reason": "legacy review",
        "approval_status": "candidate_only",
    }

    normalized = normalize_memory_promotion_candidate(
        legacy,
        source_task_id="task-legacy",
        now=now,
    )

    payload = normalized.model_dump(mode="json")
    assert payload["approval_status"] == "candidate_only"
    assert payload["approved_by"] == ""
    assert payload["approved_at"] is None
    assert payload["rejected_reason"] == ""
    assert payload["expires_at"] is None
    assert payload["metadata"]["no_expiry_reason"] == (
        "legacy candidate did not include expires_at"
    )
    assert "task:task-legacy" in payload["source_refs"]
    assert "mission_review:task-legacy" in payload["source_refs"]


def test_runtime_scorecard_does_not_consume_memory_candidate_artifacts():
    scorecard = build_mission_scorecard(
        {
            "mission_contract": {"contract_id": "mission-memory-artifact-only"},
            "task_graph": {"nodes": [{"node_id": "node-a", "status": "done"}]},
            "job_runs": [],
            "memory_promotion_candidates": [
                {
                    "candidate_id": "approved-but-not-reused",
                    "type": "failure_pattern",
                    "content": "Approved candidate is artifact-only for #134.",
                    "source_task_id": "task-memory",
                    "source_artifact_ref": "mission_review.memory_promotion_candidates",
                    "confidence": 0.9,
                    "last_verified_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-02-01T00:00:00Z",
                    "promotion_reason": "test",
                    "invalidation_rule": "test",
                    "approval_status": "approved",
                    "approved_by": "operator",
                    "approved_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
        final_status="completed",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = scorecard.model_dump(mode="json")
    assert payload["memory_promotion_candidate_count"] == 0
    assert payload["metadata"]["failure_type_counts"] == {}


def test_build_mission_scorecard_defaults_when_no_verdicts_are_recorded():
    scorecard = build_mission_scorecard(
        {
            "mission_contract": {"contract_id": "mission-empty"},
            "task_graph": {"nodes": []},
            "job_runs": [],
        },
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = scorecard.model_dump(mode="json")
    assert payload["verification_pass_rate"] == 0.0
    assert payload["recovery_success_rate"] == 0.0
    assert payload["last_verifier_verdict"] is None
    assert payload["metadata"]["total_verifier_verdicts"] == 0
    assert payload["metadata"]["retry_node_count"] == 0
    assert payload["metadata"]["recovered_retry_node_count"] == 0


def test_build_memory_promotion_candidates_returns_empty_without_policy_or_verdicts():
    assert build_memory_promotion_candidates({}) == []


def test_build_mission_scorecard_distinguishes_no_retries_from_recovery_failure():
    scorecard = build_mission_scorecard(
        {
            "mission_contract": {"contract_id": "mission-no-retry-failure"},
            "task_graph": {
                "nodes": [
                    {
                        "node_id": "failed-node",
                        "status": "failed",
                        "retry_count": 0,
                    }
                ]
            },
            "verifier_verdicts": [{"verdict": "fail", "failure_type": "tool_timeout"}],
        },
        final_status="failed",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = scorecard.model_dump(mode="json")
    assert payload["recovery_success_rate"] == 0.0
    assert payload["metadata"]["retry_node_count"] == 0
    assert payload["metadata"]["recovered_retry_node_count"] == 0
    assert payload["metadata"]["failure_type_counts"] == {"tool_timeout": 1}


def test_build_mission_scorecard_uses_job_and_node_verdict_fallbacks():
    scorecard = build_mission_scorecard(
        {
            "mission_contract": {"contract_id": "mission-verdict-fallback"},
            "task_graph": {
                "nodes": [
                    {
                        "node_id": "node-with-verdict",
                        "status": "done",
                        "verifier_verdict": {"verdict": "pass"},
                    }
                ]
            },
            "job_runs": [
                {
                    "run_id": "run-with-verdict",
                    "verifier_verdict": {
                        "verdict": "fail",
                        "failure_type": "weak_evidence",
                    },
                }
            ],
        },
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    payload = scorecard.model_dump(mode="json")
    assert payload["verification_pass_rate"] == 0.5
    assert payload["metadata"]["total_verifier_verdicts"] == 2
    assert payload["metadata"]["failure_type_counts"] == {"weak_evidence": 1}
