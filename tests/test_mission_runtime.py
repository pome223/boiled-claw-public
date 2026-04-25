from datetime import datetime, timezone

from src.runtime.mission_runtime import (
    build_memory_promotion_candidates,
    build_mission_memory_links,
    build_mission_review,
    build_mission_scorecard,
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
                },
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
    assert payload["final_status"] == "blocked"
    assert payload["failure_buckets"] == [{"failure_type": "weak_evidence", "count": 2}]
    assert payload["repeated_failure_patterns"] == [
        {"failure_type": "weak_evidence", "count": 2}
    ]
    assert payload["improvement_candidates"][0]["candidate_type"] == "verifier_improvement"
    memory_candidate = payload["memory_promotion_candidates"][0]
    assert memory_candidate["schema_version"] == "memory_promotion_candidate.v1"
    assert memory_candidate["type"] == "failure_pattern"
    assert memory_candidate["source_task_id"] == "task-123"
    assert memory_candidate["last_verified_at"] == "2026-01-01T00:00:00Z"
    assert memory_candidate["expires_at"] == "2026-01-01T01:00:00Z"
    assert memory_candidate["approval_required"] is True

    links = build_mission_memory_links(review)
    assert links["schema_version"] == "mission_memory_links.v1"
    assert links["memory_promotion_candidates"] == payload["memory_promotion_candidates"]


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
            "verifier_verdicts": [
                {"verdict": "fail", "failure_type": "tool_timeout"}
            ],
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
