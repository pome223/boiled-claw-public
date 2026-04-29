"""Substring assertions for the read-only 10th-stage readiness panel."""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _tenth_stage_renderer_source() -> str:
    bundle = _bundle()
    start = bundle.index("function renderTenthStageReadinessCheck(")
    end = bundle.index("function renderTaskDetail(", start)
    return bundle[start:end]


def test_tenth_stage_readiness_panel_section_and_schema_labels_present():
    bundle = _bundle()

    assert "10th-Stage Readiness Check" in bundle
    assert "tenth_stage_readiness_check.v1" in bundle
    assert "function renderTenthStageReadinessCheck(" in bundle
    assert "renderTenthStageReadinessCheck(task)" in bundle


def test_tenth_stage_readiness_panel_reads_task_artifacts_only():
    source = _tenth_stage_renderer_source()

    assert "artifacts.tenth_stage_readiness_check" in source
    assert "durable.tenth_stage_readiness_check" in source


def test_tenth_stage_readiness_panel_surfaces_readiness_fields():
    source = _tenth_stage_renderer_source()

    assert "readiness_status" in source
    assert "live_action_status" in source
    assert "missing_preconditions" in source
    assert "blocked_reasons" in source
    assert "live_action_blocked_reasons" in source
    assert "warning_reasons" in source
    assert "evidence_refs" in source
    assert "audit_refs" in source
    assert "limited_live_action_rehearsal_ref" in source
    assert "mission_contract_ref" in source
    assert "adopting_organization_ref" in source
    assert "hardware_owner_ref" in source
    assert "certified_or_autopilot_controller_ref" in source
    assert "emergency_stop_process_ref" in source


def test_tenth_stage_readiness_panel_surfaces_safety_boundary_flags():
    source = _tenth_stage_renderer_source()

    assert "organization_review_required" in source
    assert "operator_approval_required" in source
    assert "operator_approval_performed" in source
    assert "stronger_execution_allowed" in source
    assert "live_execution_allowed" in source
    assert "physical_execution_invoked" in source
    assert "command_payload_allowed" in source
    assert "dispatch_implementation_present" in source
    assert "ros_dispatch_allowed" in source
    assert "mavlink_dispatch_allowed" in source
    assert "actuator_execution_allowed" in source
    assert "rule_based" in source
    assert "llm_judge_used" in source


def test_tenth_stage_readiness_panel_does_not_introduce_action_surface():
    bundle = _bundle()
    forbidden = [
        'data-action="tenth-stage',
        'data-action="approve-tenth-stage',
        'data-action="approve-live-action',
        'data-action="approve-limited-live',
        'data-action="execute-tenth-stage',
        'data-action="execute-limited-live',
        'data-action="dispatch-tenth-stage',
        'data-action="dispatch-limited-live',
        'data-action="send-command',
        'data-action="ros-dispatch',
        'data-action="mavlink-dispatch',
        'data-action="actuator-',
        ">Approve 10th Stage<",
        ">Approve Live Action<",
        ">Execute Live Action<",
        ">Dispatch Live Action<",
        ">Send Command<",
        ">Dispatch ROS<",
        ">Dispatch MAVLink<",
        ">Run Actuator<",
    ]

    for needle in forbidden:
        assert needle not in bundle, f"forbidden 10th-stage action surface present: {needle}"
