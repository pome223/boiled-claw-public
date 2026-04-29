"""Substring assertions for the read-only limited live action gate panel."""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _limited_live_renderer_source() -> str:
    bundle = _bundle()
    start = bundle.index("function renderLimitedLiveActionGate(")
    end = bundle.index("function renderTaskDetail(", start)
    return bundle[start:end]


def test_limited_live_action_gate_panel_section_and_schema_labels_present():
    bundle = _bundle()

    assert "Limited Live Action Gate" in bundle
    assert "Operator Review Package" in bundle
    assert "limited_live_action_gate.v1" in bundle
    assert "limited_live_action_approval_package.v1" in bundle
    assert "function renderLimitedLiveActionGate(" in bundle
    assert "renderLimitedLiveActionGate(task)" in bundle


def test_limited_live_action_gate_panel_reads_task_artifacts_only():
    source = _limited_live_renderer_source()

    assert "artifacts.limited_live_action_gate" in source
    assert "artifacts.limited_live_action_approval_package" in source
    assert "durable.limited_live_action_gate" in source
    assert "durable.limited_live_action_approval_package" in source


def test_limited_live_action_gate_panel_surfaces_review_package_fields():
    source = _limited_live_renderer_source()

    assert "status" in source
    assert "passed" in source
    assert "missing_preconditions" in source
    assert "blocked_reasons" in source
    assert "warning_reasons" in source
    assert "autonomy_gate_result_refs" in source
    assert "hil_telemetry_review_refs" in source
    assert "emergency_stop_evidence_refs" in source
    assert "rollback_plan_refs" in source
    assert "action_allowlist_refs" in source
    assert "responsibility_ack_refs" in source
    assert "audit_refs" in source
    assert "required_evidence_refs" in source
    assert "required_operator_role" in source
    assert "responsibility_summary" in source


def test_limited_live_action_gate_panel_surfaces_safety_boundary_flags():
    source = _limited_live_renderer_source()

    assert "proposal_categories_only" in source
    assert "action_allowlist_scope" in source
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


def test_limited_live_action_gate_panel_does_not_introduce_action_surface():
    bundle = _bundle()
    forbidden = [
        'data-action="limited-live',
        'data-action="approve-live-action',
        'data-action="approve-limited-live',
        'data-action="execute-limited-live',
        'data-action="dispatch-limited-live',
        'data-action="send-command',
        'data-action="ros-dispatch',
        'data-action="mavlink-dispatch',
        'data-action="actuator-',
        ">Approve Live Action<",
        ">Execute Live Action<",
        ">Dispatch Live Action<",
        ">Send Command<",
        ">Dispatch ROS<",
        ">Dispatch MAVLink<",
        ">Run Actuator<",
    ]

    for needle in forbidden:
        assert needle not in bundle, f"forbidden limited-live action surface present: {needle}"
