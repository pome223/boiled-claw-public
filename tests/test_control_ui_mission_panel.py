from pathlib import Path


def test_control_ui_bundle_includes_mission_runtime_sections():
    bundle = Path("src/gateway/static/app.js").read_text(encoding="utf-8")
    styles = Path("src/gateway/static/styles.css").read_text(encoding="utf-8")

    assert "Mission Runtime" in bundle
    assert "Mission Task Graph" in bundle
    assert "Recovery Decisions" in bundle
    assert "Verifier Evidence" in bundle
    assert "Memory Candidate State" in bundle
    assert "Reuse Plan" in bundle
    assert "Selected Memories" in bundle
    assert "Selected Skills" in bundle
    assert "Selected Policies" in bundle
    assert "Selected Capabilities" in bundle
    assert "Excluded Candidates" in bundle
    assert "Expiry Checks" in bundle
    assert "Policy Checks" in bundle
    assert "Reuse Plan History" in bundle
    assert "automatic_runtime_application" in bundle
    assert "waiting_for_approval" in bundle
    assert "candidate_only" in bundle

    assert "Toy Grid Replay" in bundle
    assert "Initial State" in bundle
    assert "Final State" in bundle
    assert "Action Sequence" in bundle
    assert "Step Results" in bundle
    assert "Physical Replay Artifacts" in bundle
    assert "SVG Preview" in bundle
    assert "simulation_only" in bundle
    assert "live_execution_allowed" in bundle
    assert "physical_execution_invoked" in bundle
    assert "step_toy_grid_world" not in bundle
    assert 'data-action="toy-grid' not in bundle

    assert "safeToyGridSvgDataUrl" in bundle
    assert "encodeURIComponent" in bundle
    assert r"(?:^|[\s<])on[a-z0-9_-]+\s*=" in bundle
    assert "<script" in bundle
    assert "renderSafeToyGridSvgPreview" in bundle
    assert ".toy-grid-svg-frame" in styles
