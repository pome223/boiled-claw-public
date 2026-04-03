import pytest
from fastapi import HTTPException

from src.gateway.task_replay import (
    build_partial_replay_seed,
    build_task_compare_payload,
    parse_task_replay_request,
    step_compare_rows,
    task_result_snapshot,
)
from src.runtime.replay_schema import StepComparePayload, TaskResultSnapshot
from src.runtime.state_keys import StateKeys


def _control_loop_task_with_result(result: dict):
    return {
        "task_id": "task_demo",
        "kind": "control_loop",
        "status": "failed",
        "owner_session_id": "sess-1",
        "owner_user_id": "alice",
        "artifacts": {
            "result": result,
        },
    }


def test_parse_task_replay_request_normalizes_from_step():
    request = parse_task_replay_request(
        {"from_step": "  verify_visual_state  ", "ignored": True}
    )

    assert request.from_step == "verify_visual_state"


def test_task_result_snapshot_validates_contract_and_filters_invalid_step_entries():
    snapshot = task_result_snapshot(
        _control_loop_task_with_result(
            {
                "success": False,
                "final_text": "verification failed",
                "verification_report_id": "eval-1",
                "verification_report": {
                    "status": "fail",
                    "overall_score": 0.25,
                    "criterion_results": [
                        {"name": "music_playing", "passed": False},
                        {"name": "app_visible", "passed": True},
                    ],
                },
                "artifact_refs": ["before.png", "after.png"],
                "step_trace": [
                    {
                        "step_id": "verify_visual_state",
                        "title": "Verify visual state",
                        "description": "",
                        "step_type": "plan",
                        "status": "failed",
                        "output_summary": "evidence thin",
                        "failed_criteria": ["music_playing"],
                    },
                    {"invalid": True},
                ],
                "tail_replay_from_step_id": "verify_visual_state",
            }
        )
    )

    TaskResultSnapshot.model_validate(snapshot)
    assert snapshot["failed_criteria"] == ["music_playing"]
    assert snapshot["passed_criteria_count"] == 1
    assert snapshot["criteria_count"] == 2
    assert snapshot["screenshot_refs"] == ["before.png", "after.png"]
    assert len(snapshot["step_trace"]) == 1


def test_step_compare_rows_marks_changed_status_and_summary():
    rows = step_compare_rows(
        [
            {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "description": "",
                "step_type": "plan",
                "status": "failed",
                "output_summary": "evidence thin",
                "failed_criteria": ["music_playing"],
            }
        ],
        [
            {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "description": "",
                "step_type": "plan",
                "status": "succeeded",
                "output_summary": "fresh screenshots captured",
                "failed_criteria": [],
            }
        ],
    )

    payload = StepComparePayload(total=len(rows), changed=1, rows=rows)

    assert payload.changed == 1
    assert payload.rows[0].changed is True
    assert payload.rows[0].left.status == "failed"
    assert payload.rows[0].right.status == "succeeded"


def test_build_partial_replay_seed_returns_validated_replay_context():
    seed = build_partial_replay_seed(
        _control_loop_task_with_result(
            {
                "approved_plan": {
                    "plan_id": "plan-1",
                    "risk_level": "medium",
                    "steps": [
                        {"step_id": "launch_djay", "title": "Launch Djay"},
                        {"step_id": "verify_visual_state", "title": "Verify visual state"},
                    ],
                },
                "verification_status": "partial_pass",
                "step_trace": [
                    {
                        "step_id": "verify_visual_state",
                        "title": "Verify visual state",
                        "description": "",
                        "step_type": "plan",
                        "status": "failed",
                        "output_summary": "evidence thin",
                        "failed_criteria": ["music_playing"],
                    }
                ],
                "tail_replay_from_step_id": "verify_visual_state",
            }
        ),
        from_step="verify_visual_state",
    )

    assert seed[StateKeys.PLAN_APPROVED]["plan_id"] == "plan-1"
    assert seed[StateKeys.REPLAY_FROM_STEP] == "verify_visual_state"
    assert seed[StateKeys.REPLAY_CONTEXT]["mode"] == "tail"
    assert seed[StateKeys.REPLAY_CONTEXT]["step_trace"][0]["step_id"] == "verify_visual_state"


def test_build_partial_replay_seed_rejects_unknown_step():
    with pytest.raises(HTTPException) as exc_info:
        build_partial_replay_seed(
            _control_loop_task_with_result(
                {
                    "approved_plan": {
                        "plan_id": "plan-1",
                        "risk_level": "medium",
                        "steps": [{"step_id": "launch_djay", "title": "Launch Djay"}],
                    }
                }
            ),
            from_step="verify_visual_state",
        )

    assert exc_info.value.status_code == 400
    assert "unknown replay step" in str(exc_info.value.detail)


def test_build_task_compare_payload_exposes_step_compare_contract():
    left_task = _control_loop_task_with_result(
        {
            "success": False,
            "verification_report": {"status": "fail", "overall_score": 0.3},
            "step_trace": [
                {
                    "step_id": "verify_visual_state",
                    "title": "Verify visual state",
                    "description": "",
                    "step_type": "plan",
                    "status": "failed",
                    "output_summary": "evidence thin",
                    "failed_criteria": ["music_playing"],
                }
            ],
        }
    )
    right_task = _control_loop_task_with_result(
        {
            "success": True,
            "verification_report": {"status": "pass", "overall_score": 0.95},
            "step_trace": [
                {
                    "step_id": "verify_visual_state",
                    "title": "Verify visual state",
                    "description": "",
                    "step_type": "plan",
                    "status": "succeeded",
                    "output_summary": "fresh screenshots captured",
                    "failed_criteria": [],
                }
            ],
        }
    )

    payload = build_task_compare_payload(
        left_task,
        right_task,
        build_task_timeline_payload=lambda task, *, page, page_size: {
            "pagination": {"total": 1},
            "entries": [{"kind": "task", "task_id": task["task_id"]}],
        },
    )

    assert payload["step_compare"]["changed"] == 1
    assert payload["step_compare"]["rows"][0]["step_id"] == "verify_visual_state"
    assert payload["left"]["timeline"]["entries"][0]["task_id"] == "task_demo"
