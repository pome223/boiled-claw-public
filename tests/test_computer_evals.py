import pytest

from src.computer_use import trajectory_store as trajectory_store_module
from src.computer_use.trajectory_store import ComputerTrajectoryStore
from src.tools import computer


@pytest.fixture
def trajectory_store(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    monkeypatch.setattr(computer, "get_computer_trajectory_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_computer_evaluate_reports_partial_pass_for_browser_surface(monkeypatch):
    async def _browser_extract_text(selector=None, tool_context=None):
        return {"selector": selector, "text": "saved successfully", "success": True}

    async def _current_tab_info(*, tool_context=None):
        return {
            "url": "https://example.com/stale",
            "title": "Stale Page",
            "success": True,
        }

    async def _current_tab_extract_text(*, selector=None, tool_context=None):
        raise AssertionError("current_tab_extract_text should not be used for browser evaluation")

    monkeypatch.setattr(computer, "browser_extract_text", _browser_extract_text)
    monkeypatch.setattr(computer, "current_tab_info", _current_tab_info)
    monkeypatch.setattr(computer, "current_tab_extract_text", _current_tab_extract_text)

    result = await computer.computer_evaluate(
        selector="#status",
        expected_text_contains="saved",
        expected_url_contains="/dashboard",
        surface="browser",
    )

    assert result["success"] is False
    assert result["status"] == "partial_pass"
    assert [check["name"] for check in result["checks"]] == ["text_contains", "url_contains"]
    assert result["checks"][0]["passed"] is True
    assert result["checks"][1]["passed"] is False
    assert result["snapshot"]["browser_text"]["text"] == "saved successfully"
    assert "current_tab_text" not in result["snapshot"]


@pytest.mark.asyncio
async def test_computer_evaluate_skips_when_no_expectations_are_given(monkeypatch):
    async def _unexpected_call(*args, **kwargs):
        raise AssertionError("verification probes should be skipped without expectations")

    monkeypatch.setattr(computer, "current_tab_info", _unexpected_call)
    monkeypatch.setattr(computer, "current_tab_extract_text", _unexpected_call)
    monkeypatch.setattr(computer, "browser_extract_text", _unexpected_call)
    monkeypatch.setattr(computer, "desktop_view_frontmost_app", _unexpected_call)
    monkeypatch.setattr(computer, "desktop_view_windows", _unexpected_call)

    result = await computer.computer_evaluate(surface="current_tab")

    assert result == {
        "status": "skipped",
        "checks": [],
        "success": True,
        "surface": "current_tab",
        "expected": {},
    }


@pytest.mark.asyncio
async def test_computer_click_records_success_trajectory_for_current_tab(monkeypatch, trajectory_store):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "current_tab",
            "available_surfaces": ["current_tab", "desktop"],
            "success": True,
        }

    async def _current_tab_click(selector, tool_context=None):
        return {"selector": selector, "success": True}

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "current_tab_click", _current_tab_click)

    result = await computer.computer_click(selector="#submit")

    assert result["success"] is True
    assert result["trajectory_id"] > 0

    recent = await computer.computer_trajectory_recent(status="success", limit=5)

    assert recent["count"] == 1
    assert recent["trajectories"][0]["status"] == "success"
    assert recent["trajectories"][0]["action"] == "click"
    assert recent["trajectories"][0]["final_surface"] == "current_tab"
    assert recent["trajectories"][0]["attempts"][0]["strategy"] == "current_tab_selector"
    assert recent["trajectories"][0]["request"]["selector"] == "#submit"
    assert recent["trajectories"][0]["observation"]["preferred_surface"] == "current_tab"
    assert trajectory_store.recent(status="success", limit=1)[0]["id"] == result["trajectory_id"]


@pytest.mark.asyncio
async def test_computer_trajectory_recent_orders_filters_and_limits(monkeypatch, trajectory_store):
    timestamps = iter((100.0, 200.0, 300.0))
    monkeypatch.setattr(trajectory_store_module.time, "time", lambda: next(timestamps))

    trajectory_store.record(
        action="click",
        status="success",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification=None,
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    trajectory_store.record(
        action="click",
        status="failed",
        final_surface="browser",
        attempts=[{"surface": "browser", "strategy": "managed_browser_selector"}],
        verification={"status": "fail", "success": False},
        request={"selector": "#retry"},
        observation={"preferred_surface": "desktop", "available_surfaces": ["desktop"]},
    )
    trajectory_store.record(
        action="fill",
        status="recovered",
        final_surface="desktop",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={"status": "pass", "success": True},
        request={"selector": "#query"},
        observation={"preferred_surface": "desktop", "available_surfaces": ["desktop"]},
    )

    limited = await computer.computer_trajectory_recent(limit=2)
    failed_only = await computer.computer_trajectory_recent(status="failed", limit=5)

    assert limited["count"] == 2
    assert [trajectory["status"] for trajectory in limited["trajectories"]] == ["recovered", "failed"]
    assert failed_only["count"] == 1
    assert failed_only["trajectories"][0]["status"] == "failed"
    assert failed_only["trajectories"][0]["final_surface"] == "browser"
