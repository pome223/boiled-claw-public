"""Substring assertions verifying the read-only autonomy artifacts UI.

Mirrors the pattern from ``test_control_ui_mission_panel.py``: load the
bundled ``app.js`` / ``styles.css`` and assert that the autonomy panels are
wired in, while making sure no execution / approval / promotion / runtime
reuse action surface has been added.

These checks are intentionally substring-level because the static bundle is
plain JS / CSS — they are fast and protect the read-only contract of #169.
"""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _styles() -> str:
    return Path("src/gateway/static/styles.css").read_text(encoding="utf-8")


def test_autonomy_panel_section_titles_present():
    bundle = _bundle()
    assert "Autonomy Episode" in bundle
    assert "Autonomy Scorecard" in bundle
    assert "Autonomy Episode Review" in bundle
    assert "Autonomy Gate Result" in bundle
    assert "Autonomy Gate Comparison" in bundle


def test_autonomy_panel_renders_safety_artifacts():
    bundle = _bundle()
    # The render entrypoint and its wiring into the detail panel must be present.
    assert "function renderAutonomyArtifacts(" in bundle
    assert "renderAutonomyArtifacts(task)" in bundle


def test_autonomy_panel_reads_artifacts_directly():
    bundle = _bundle()
    # Read directly off task.artifacts.<name>; no digging into durable_execution
    # or nested reports for the autonomy artifacts.
    assert "artifacts.autonomous_episode" in bundle
    assert "artifacts.autonomy_scorecard" in bundle
    assert "artifacts.autonomy_episode_review" in bundle
    assert "artifacts.autonomy_gate_result" in bundle
    assert "artifacts.autonomy_gate_comparison_result" in bundle


def test_autonomy_panel_surfaces_gate_reasons_and_schemas():
    bundle = _bundle()
    assert "blocked_reasons" in bundle
    assert "warning_reasons" in bundle
    assert "metric_deltas" in bundle
    assert "autonomy_gate_result.v1" in bundle
    assert "autonomy_gate_comparison_result.v1" in bundle


def test_autonomy_panel_emits_safety_boundary_badges():
    bundle = _bundle()
    assert "operator_approval_required" in bundle
    assert "operator_approval_performed" in bundle
    assert "stronger_execution_allowed" in bundle
    assert "live_execution_allowed" in bundle
    assert "physical_execution_invoked" in bundle


def test_autonomy_panel_styles_defined():
    styles = _styles()
    assert ".autonomy-section" in styles
    assert ".autonomy-reason-blocked" in styles
    assert ".autonomy-reason-warning" in styles
    assert ".autonomy-safety-badges" in styles
    assert ".autonomy-metric-deltas" in styles


def test_autonomy_panel_does_not_introduce_action_surface():
    # Read-only contract: no buttons, no data-actions, no execution / approval /
    # promotion / runtime reuse triggers may be emitted from the autonomy UI.
    bundle = _bundle()
    forbidden = [
        'data-action="autonomy-',
        'data-action="approve-autonomy',
        'data-action="execute-autonomy',
        'data-action="dispatch-autonomy',
        'data-action="start-autonomy',
        'data-action="run-autonomy',
        'data-action="promote-autonomy',
        'data-action="reuse-autonomy',
        "run_toy_grid_world_autonomous_episode",
    ]
    for needle in forbidden:
        assert needle not in bundle, f"forbidden action surface present: {needle}"


def test_autonomy_panel_metric_deltas_table_includes_direction_and_severity():
    bundle = _bundle()
    # Header columns
    assert "<th>direction</th>" in bundle
    assert "<th>severity</th>" in bundle
    # Severity row classes used to color-code blocking / warning / info rows
    assert "autonomy-metric-severity-blocking" in bundle
    assert "autonomy-metric-severity-warning" in bundle
    assert "autonomy-metric-severity-info" in bundle


def test_autonomy_panel_severity_class_styles_defined():
    styles = _styles()
    assert ".autonomy-metric-severity-blocking" in styles
    assert ".autonomy-metric-severity-warning" in styles
    assert ".autonomy-metric-severity-info" in styles


def test_autonomy_panel_does_not_emit_live_or_physical_dispatch_calls():
    # Defensive: even within helper text, must not appear to imply a path to
    # live or physical execution from the UI.
    bundle = _bundle()
    forbidden = [
        'data-action="autonomy-live',
        'data-action="autonomy-physical',
        'data-action="enable-live',
        'data-action="enable-physical',
    ]
    for needle in forbidden:
        assert needle not in bundle, f"forbidden execution surface present: {needle}"
