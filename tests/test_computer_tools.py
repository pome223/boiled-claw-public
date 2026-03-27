import pytest

from src.tools import computer


@pytest.mark.asyncio
async def test_computer_observe_prefers_current_tab_and_collects_desktop_context(monkeypatch):
    async def _current_tab_info(*, tool_context=None):
        return {
            "tab_id": 7,
            "window_id": 70,
            "url": "https://example.com",
            "title": "Example",
            "success": True,
        }

    async def _current_tab_extract_text(*, selector=None, tool_context=None):
        return {
            "selector": selector or "body",
            "text": "hello world",
            "length": 11,
            "success": True,
        }

    async def _frontmost_app(*, tool_context=None):
        return {"app_name": "Google Chrome", "pid": 123, "ok": True}

    async def _windows(*, include_minimized=False, tool_context=None):
        return {"windows": [{"window_id": "1", "title": "Example"}], "ok": True}

    async def _screenshot(*, tool_context=None):
        return {"path": "/tmp/shot.png", "width": 100, "height": 50, "success": True}

    async def _ax_find(**kwargs):
        return {"matched": True, "target": {"title": "Search"}, "ok": True}

    async def _ax_snapshot(*, app_name=None, window_id=None, tool_context=None):
        return {"tree": {"role": "window"}, "ok": True}

    monkeypatch.setattr(computer, "current_tab_info", _current_tab_info)
    monkeypatch.setattr(computer, "current_tab_extract_text", _current_tab_extract_text)
    monkeypatch.setattr(computer, "desktop_view_frontmost_app", _frontmost_app)
    monkeypatch.setattr(computer, "desktop_view_windows", _windows)
    monkeypatch.setattr(computer, "desktop_view_screenshot", _screenshot)
    monkeypatch.setattr(computer, "desktop_ax_find", _ax_find)
    monkeypatch.setattr(computer, "desktop_ax_snapshot", _ax_snapshot)

    result = await computer.computer_observe(
        include_current_tab_text=True,
        current_tab_selector="#main",
        include_screenshot=True,
        include_ax_snapshot=True,
        ax_role="button",
        ax_title="Search",
    )

    assert result["success"] is True
    assert result["preferred_surface"] == "current_tab"
    assert result["available_surfaces"] == ["current_tab", "desktop"]
    assert result["current_tab"]["title"] == "Example"
    assert result["current_tab_text"]["selector"] == "#main"
    assert result["frontmost_app"]["app_name"] == "Google Chrome"
    assert result["windows"]["windows"][0]["title"] == "Example"
    assert result["screenshot"]["path"] == "/tmp/shot.png"
    assert result["ax_find"]["matched"] is True
    assert result["ax_snapshot"]["tree"]["role"] == "window"
    assert "errors" not in result


@pytest.mark.asyncio
async def test_computer_observe_allows_partial_success(monkeypatch):
    async def _current_tab_info(*, tool_context=None):
        return {
            "error": "Current Tab extension is not connected",
            "success": False,
        }

    async def _frontmost_app(*, tool_context=None):
        return {"app_name": "Safari", "pid": 77, "ok": True}

    async def _windows(*, include_minimized=False, tool_context=None):
        return {"error": "desktop query failed", "ok": False}

    monkeypatch.setattr(computer, "current_tab_info", _current_tab_info)
    monkeypatch.setattr(computer, "desktop_view_frontmost_app", _frontmost_app)
    monkeypatch.setattr(computer, "desktop_view_windows", _windows)

    result = await computer.computer_observe()

    assert result["success"] is True
    assert result["preferred_surface"] == "desktop"
    assert result["available_surfaces"] == ["desktop"]
    assert result["errors"]["current_tab"] == "Current Tab extension is not connected"
    assert result["errors"]["windows"] == "desktop query failed"


@pytest.mark.asyncio
async def test_computer_observe_returns_failure_when_no_surface_is_available(monkeypatch):
    async def _current_tab_info(*, tool_context=None):
        return {"error": "host bridge unavailable", "success": False}

    async def _frontmost_app(*, tool_context=None):
        return {"error": "desktop bridge unavailable", "ok": False}

    async def _windows(*, include_minimized=False, tool_context=None):
        return {"error": "desktop query failed", "ok": False}

    monkeypatch.setattr(computer, "current_tab_info", _current_tab_info)
    monkeypatch.setattr(computer, "desktop_view_frontmost_app", _frontmost_app)
    monkeypatch.setattr(computer, "desktop_view_windows", _windows)

    result = await computer.computer_observe()

    assert result["success"] is False
    assert result["preferred_surface"] is None
    assert result["available_surfaces"] == []
    assert result["errors"]["current_tab"] == "host bridge unavailable"
    assert result["errors"]["frontmost_app"] == "desktop bridge unavailable"
