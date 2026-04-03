from src.control_loop.replay_trace import _build_step_trace, _parse_replay_context


def test_build_step_trace_skips_verification_entry_without_status():
    trace = _build_step_trace(
        plan={"steps": [{"step_id": "launch_djay", "title": "Launch Djay"}]},
        executor_outputs={"steps_executed": []},
        report={},
        replay_context=None,
    )

    assert [item["step_id"] for item in trace] == ["launch_djay"]


def test_parse_replay_context_validates_shape():
    parsed = _parse_replay_context(
        {
            "source_task_id": "task_old",
            "from_step": "verify_visual_state",
            "mode": "tail",
            "previous_failed_criteria": ["music_playing"],
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

    assert parsed is not None
    assert parsed["mode"] == "tail"
    assert parsed["step_trace"][0]["step_id"] == "verify_visual_state"
