"""Substring assertions verifying the read-only HIL telemetry review UI.

Mirrors the pattern from ``test_control_ui_autonomy_panel.py`` and
``test_control_ui_hil_telemetry_panel.py``: load the bundled ``app.js`` /
``styles.css`` and assert that the HIL review surfaces are wired in, while
making sure no execution / approval / command / dispatch / actuator / ROS
action surface has been added.

These checks are intentionally substring-level because the static bundle is
plain JS / CSS — they are fast and protect the read-only contract.
"""

from pathlib import Path


def _bundle() -> str:
    return Path("src/gateway/static/app.js").read_text(encoding="utf-8")


def _styles() -> str:
    return Path("src/gateway/static/styles.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# positive: HIL review surfaces are wired in
# ---------------------------------------------------------------------------


def test_standalone_hil_review_panel_render_function_present():
    bundle = _bundle()
    assert "function renderHilTelemetryReview(" in bundle
    assert "renderHilTelemetryReview(task)" in bundle


def test_standalone_hil_review_panel_section_title_present():
    assert "HIL Telemetry Review" in _bundle()


def test_standalone_hil_review_panel_reads_task_artifacts_directly():
    bundle = _bundle()
    # Standalone single review and optional list form
    assert "artifacts.hil_telemetry_review" in bundle
    assert "artifacts.hil_telemetry_reviews" in bundle


def test_standalone_hil_review_panel_surfaces_schema_and_key_fields():
    bundle = _bundle()
    assert "hil_telemetry_review.v1" in bundle
    assert "review_id" in bundle
    assert "blocked_reasons" in bundle
    assert "warning_reasons" in bundle
    assert "freshness_seconds_max" in bundle
    assert "freshness_threshold_seconds" in bundle
    assert "rejected_command_like_payload_count" in bundle


def test_standalone_hil_review_panel_surfaces_known_buckets():
    bundle = _bundle()
    assert "hil_telemetry_stale" in bundle
    assert "hil_telemetry_missing" in bundle
    assert "hil_telemetry_malformed" in bundle
    assert "command_payload_rejected" in bundle


def test_standalone_hil_review_panel_emits_safety_boundary_badges():
    bundle = _bundle()
    assert "operator_approval_required" in bundle
    assert "live_execution_allowed" in bundle
    assert "physical_execution_invoked" in bundle
    assert "command_payload_allowed" in bundle


def test_autonomy_gate_panel_surfaces_hil_review_refs_and_snapshot_count():
    bundle = _bundle()
    # The Autonomy Gate Result section gained a card showing the gate's
    # hil_telemetry_review_refs and the number of attached snapshots.
    assert "hil_telemetry_review_refs" in bundle
    assert "hil_telemetry_review_snapshots" in bundle
    assert "autonomy-hil-refs-card" in bundle


def test_required_hil_review_missing_reason_appears_in_bundle():
    # The reason string the gate emits when required_hil_telemetry_review
    # is True with no review attached MUST be visible in the UI bundle so
    # operators can see this exact reason in the gate's blocked_reasons
    # list rendered by renderReasonList.
    assert "required_hil_telemetry_review_missing" in _bundle()


def test_hil_review_styles_defined():
    styles = _styles()
    assert ".hil-telemetry-review" in styles
    assert ".autonomy-hil-refs-card" in styles


# ---------------------------------------------------------------------------
# negative: no action surface added by this PR
# ---------------------------------------------------------------------------


def test_hil_review_panel_does_not_introduce_action_surface():
    bundle = _bundle()
    forbidden = [
        'data-action="hil-',
        'data-action="approve-hil',
        'data-action="execute-hil',
        'data-action="dispatch-hil',
        'data-action="run-hil',
        'data-action="promote-hil',
        'data-action="reuse-hil',
        'data-action="approve-review',
        'data-action="hil-review-',
        'data-action="send-command',
        'data-action="dispatch-command',
        'data-action="actuator-',
        'data-action="ros-dispatch',
        'data-action="ros2-dispatch',
    ]
    for needle in forbidden:
        assert needle not in bundle, f"forbidden action surface present: {needle}"


def test_hil_review_panel_does_not_emit_command_or_dispatch_buttons():
    bundle = _bundle()
    forbidden = [
        ">Send Command<",
        ">Dispatch Command<",
        ">Run Live<",
        ">Approve HIL Review<",
        ">Promote HIL Review<",
        ">Reuse HIL Review<",
        ">Run Actuator<",
        ">Dispatch ROS<",
    ]
    for needle in forbidden:
        assert needle not in bundle, f"forbidden control button present: {needle}"
