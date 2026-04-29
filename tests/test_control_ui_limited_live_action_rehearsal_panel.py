"""Substring assertions for the read-only limited live action rehearsal panel."""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _limited_live_rehearsal_renderer_source() -> str:
    bundle = _bundle()
    start = bundle.index("function renderLimitedLiveActionRehearsal(")
    end = bundle.index("function renderTaskDetail(", start)
    return bundle[start:end]


def test_limited_live_action_rehearsal_panel_section_and_schema_labels_present():
    bundle = _bundle()

    assert "Limited Live Action Rehearsal" in bundle
    assert "limited_live_action_rehearsal.v1" in bundle
    assert "function renderLimitedLiveActionRehearsal(" in bundle
    assert "renderLimitedLiveActionRehearsal(task)" in bundle


def test_limited_live_action_rehearsal_panel_reads_task_artifacts_only():
    source = _limited_live_rehearsal_renderer_source()

    assert "artifacts.limited_live_action_rehearsal" in source
    assert "durable.limited_live_action_rehearsal" in source
    assert "rehearsal.gate_snapshot" in source
    assert "rehearsal.approval_package_snapshot" in source


def test_limited_live_action_rehearsal_panel_surfaces_readiness_fields():
    source = _limited_live_rehearsal_renderer_source()

    assert "readiness_status" in source
    assert "missing_preconditions" in source
    assert "blocked_reasons" in source
    assert "warning_reasons" in source
    assert "evidence_refs" in source
    assert "audit_refs" in source
    assert "mission_contract_ref" in source
    assert "autonomy_gate_result_ref" in source
    assert "hil_telemetry_review_ref" in source
    assert "emergency_stop_evidence_ref" in source
    assert "rollback_plan_ref" in source
    assert "operator_responsibility_ack_ref" in source


def test_limited_live_action_rehearsal_panel_surfaces_safety_boundary_flags():
    source = _limited_live_rehearsal_renderer_source()

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


def test_limited_live_action_rehearsal_panel_does_not_introduce_action_surface():
    bundle = _bundle()
    forbidden = [
        'data-action="limited-live-rehearsal',
        'data-action="approve-live-action',
        'data-action="approve-limited-live',
        'data-action="approve-rehearsal',
        'data-action="execute-limited-live',
        'data-action="dispatch-limited-live',
        'data-action="send-command',
        'data-action="ros-dispatch',
        'data-action="mavlink-dispatch',
        'data-action="actuator-',
        ">Approve Live Action<",
        ">Approve Rehearsal<",
        ">Execute Live Action<",
        ">Dispatch Live Action<",
        ">Send Command<",
        ">Dispatch ROS<",
        ">Dispatch MAVLink<",
        ">Run Actuator<",
    ]

    for needle in forbidden:
        assert needle not in bundle, f"forbidden limited-live rehearsal surface present: {needle}"
