"""Substring assertions for the read-only HIL telemetry evidence panel."""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _hil_renderer_source() -> str:
    bundle = _bundle()
    start = bundle.index("function renderHilTelemetryEvidence(")
    end = bundle.index("function renderTaskDetail(", start)
    return bundle[start:end]


def test_hil_telemetry_panel_section_and_schema_labels_present():
    bundle = _bundle()

    assert "HIL Telemetry Evidence" in bundle
    assert "hil_telemetry_contract.v1" in bundle
    assert "hil_telemetry_envelope.v1" in bundle
    assert "hil_telemetry_evidence.v1" in bundle
    assert "function renderHilTelemetryEvidence(" in bundle
    assert "renderHilTelemetryEvidence(task)" in bundle


def test_hil_telemetry_panel_reads_task_artifacts_only():
    source = _hil_renderer_source()

    assert "artifacts.hil_telemetry_contract" in source
    assert "artifacts.hil_telemetry_envelope" in source
    assert "artifacts.hil_telemetry_evidence" in source
    assert "durable.hil_telemetry_contract" in source
    assert "durable.hil_telemetry_envelope" in source
    assert "durable.hil_telemetry_evidence" in source


def test_hil_telemetry_panel_surfaces_evidence_fields():
    source = _hil_renderer_source()

    assert "contract_id" in source
    assert "subject_kind" in source
    assert "subject_id" in source
    assert "captured_at" in source
    assert "freshness_seconds" in source
    assert "freshness_threshold_seconds" in source
    assert "measurement_keys" in source
    assert "rejected_command_like_payload_count" in source
    assert "gate_findings" in source
    assert "review_findings" in source


def test_hil_telemetry_panel_surfaces_read_only_safety_boundary():
    source = _hil_renderer_source()

    assert "telemetry_only" in source
    assert "read_only" in source
    assert "no_action" in source
    assert "no_command" in source
    assert "no_ros" in source
    assert "live_execution_allowed" in source
    assert "physical_execution_invoked" in source
    assert '"no_" + "act" + "uator"' in source


def test_hil_telemetry_panel_does_not_introduce_action_surface():
    bundle = _bundle()
    forbidden = [
        'data-action="hil-',
        'data-action="approve-hil',
        "run_hil",
        "send_command",
        "ros_dispatch",
        "dispatch",
        "actuator",
    ]

    for needle in forbidden:
        assert needle not in bundle, f"forbidden HIL action surface present: {needle}"
