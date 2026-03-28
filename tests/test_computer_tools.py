import pytest
from google.adk.tools import FunctionTool

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


@pytest.mark.asyncio
async def test_computer_click_prefers_current_tab_selector(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "current_tab",
            "available_surfaces": ["current_tab", "desktop"],
            "success": True,
        }

    async def _current_tab_click(selector, tool_context=None):
        return {"selector": selector, "success": True}

    async def _browser_click(selector, tool_context=None):
        raise AssertionError("browser_click should not be used when current_tab is available")

    async def _desktop_click(**kwargs):
        raise AssertionError("desktop_control_click should not be used for selector-only click")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "current_tab_click", _current_tab_click)
    monkeypatch.setattr(computer, "browser_click", _browser_click)
    monkeypatch.setattr(computer, "desktop_control_click", _desktop_click)

    result = await computer.computer_click(selector="#submit")

    assert result["success"] is True
    assert result["surface"] == "current_tab"
    assert result["strategy"] == "current_tab_selector"
    assert result["result"]["selector"] == "#submit"


@pytest.mark.asyncio
async def test_computer_click_falls_back_to_managed_browser(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "desktop",
            "available_surfaces": ["desktop"],
            "errors": {"current_tab": "relay unavailable"},
            "success": True,
        }

    async def _browser_click(selector, tool_context=None):
        return {"selector": selector, "success": True}

    async def _desktop_click(**kwargs):
        raise AssertionError("desktop_control_click should not be used without desktop target fields")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "browser_click", _browser_click)
    monkeypatch.setattr(computer, "desktop_control_click", _desktop_click)

    result = await computer.computer_click(selector="#submit")

    assert result["success"] is True
    assert result["surface"] == "browser"
    assert result["strategy"] == "managed_browser_selector"
    assert result["observation"]["errors"]["current_tab"] == "relay unavailable"


@pytest.mark.asyncio
async def test_computer_fill_uses_desktop_selector_when_available(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "desktop",
            "available_surfaces": ["desktop"],
            "success": True,
        }

    async def _desktop_type(**kwargs):
        return {
            "success": True,
            "target": {"role": kwargs["role"], "title": kwargs["title"]},
        }

    async def _browser_fill(selector, text, tool_context=None):
        raise AssertionError("browser_fill should not be used when desktop target is available")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "desktop_control_type", _desktop_type)
    monkeypatch.setattr(computer, "browser_fill", _browser_fill)

    result = await computer.computer_fill(
        text="hello",
        role="text_field",
        title="Search",
        app_name="Google Chrome",
    )

    assert result["success"] is True
    assert result["surface"] == "desktop"
    assert result["strategy"] == "desktop_selector"
    assert result["result"]["target"]["title"] == "Search"


@pytest.mark.asyncio
async def test_computer_fill_prefers_current_tab_selector(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "current_tab",
            "available_surfaces": ["current_tab", "desktop"],
            "success": True,
        }

    async def _current_tab_fill(selector, text, tool_context=None):
        return {"selector": selector, "text_length": len(text), "success": True}

    async def _desktop_type(**kwargs):
        raise AssertionError("desktop_control_type should not be used when current_tab is available")

    async def _browser_fill(selector, text, tool_context=None):
        raise AssertionError("browser_fill should not be used when current_tab is available")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "current_tab_fill", _current_tab_fill)
    monkeypatch.setattr(computer, "desktop_control_type", _desktop_type)
    monkeypatch.setattr(computer, "browser_fill", _browser_fill)

    result = await computer.computer_fill(selector="#search", text="hello")

    assert result["success"] is True
    assert result["surface"] == "current_tab"
    assert result["strategy"] == "current_tab_selector"
    assert result["result"]["text_length"] == 5


@pytest.mark.asyncio
async def test_computer_click_returns_no_surface_error_when_managed_browser_disabled(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "desktop",
            "available_surfaces": ["desktop"],
            "errors": {"current_tab": "relay unavailable"},
            "success": True,
        }

    async def _desktop_click(**kwargs):
        raise AssertionError("desktop_control_click should not be used without desktop target fields")

    async def _browser_click(selector, tool_context=None):
        raise AssertionError("browser_click should not be used when managed browser is disabled")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "desktop_control_click", _desktop_click)
    monkeypatch.setattr(computer, "browser_click", _browser_click)

    result = await computer.computer_click(
        selector="#submit",
        allow_managed_browser=False,
    )

    assert result["success"] is False
    assert result["surface"] is None
    assert result["strategy"] == "no_available_surface"
    assert result["error"] == "No browser or desktop surface could satisfy computer_click"


@pytest.mark.asyncio
async def test_computer_fill_uses_provided_observation_without_reobserve(monkeypatch):
    async def _observe(**kwargs):
        raise AssertionError("computer_observe should not be called when observed surfaces are provided")

    async def _current_tab_fill(selector, text, tool_context=None):
        return {"selector": selector, "text_length": len(text), "success": True}

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "current_tab_fill", _current_tab_fill)

    result = await computer.computer_fill(
        selector="#search",
        text="hello",
        observed_preferred_surface="current_tab",
        observed_available_surfaces=["current_tab"],
    )

    assert result["success"] is True
    assert result["surface"] == "current_tab"
    assert result["observation"]["preferred_surface"] == "current_tab"


@pytest.mark.asyncio
async def test_computer_click_prefers_desktop_target_over_managed_browser(monkeypatch):
    async def _observe(**kwargs):
        return {
            "preferred_surface": "desktop",
            "available_surfaces": ["desktop"],
            "success": True,
        }

    async def _desktop_click(**kwargs):
        return {
            "success": True,
            "target": {"role": kwargs["role"], "title": kwargs["title"]},
        }

    async def _browser_click(selector, tool_context=None):
        raise AssertionError("browser_click should not be used when desktop target is available")

    monkeypatch.setattr(computer, "computer_observe", _observe)
    monkeypatch.setattr(computer, "desktop_control_click", _desktop_click)
    monkeypatch.setattr(computer, "browser_click", _browser_click)

    result = await computer.computer_click(
        selector="#submit",
        role="button",
        title="Submit",
        app_name="Google Chrome",
    )

    assert result["success"] is True
    assert result["surface"] == "desktop"
    assert result["strategy"] == "desktop_selector"
    assert result["result"]["target"]["title"] == "Submit"


def test_computer_action_tool_schema_avoids_additional_properties():
    for tool in (computer.computer_click, computer.computer_fill):
        declaration = FunctionTool(tool)._get_declaration()
        properties = declaration.parameters.properties
        array_variant = next(
            variant
            for variant in properties["observed_available_surfaces"].any_of
            if getattr(variant.type, "value", variant.type) == "ARRAY"
        )

        assert properties["observed_available_surfaces"].additional_properties is None
        assert getattr(array_variant.items.type, "value", array_variant.items.type) == "STRING"
        assert properties["observed_preferred_surface"].additional_properties is None
