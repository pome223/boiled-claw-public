from pathlib import Path


def test_control_ui_bundle_includes_mission_runtime_sections():
    bundle = Path("src/gateway/static/app.js").read_text(encoding="utf-8")

    assert "Mission Runtime" in bundle
    assert "Mission Task Graph" in bundle
    assert "Recovery Decisions" in bundle
    assert "Verifier Evidence" in bundle
    assert "Memory Candidate State" in bundle
    assert "waiting_for_approval" in bundle
    assert "candidate_only" in bundle
