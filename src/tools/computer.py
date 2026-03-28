"""Browser-first computer-use tools."""

from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.tools.browser import browser_click, browser_fill
from src.tools.current_tab import (
    current_tab_click,
    current_tab_extract_text,
    current_tab_fill,
    current_tab_info,
)
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_type,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)


def _tool_error(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = str(payload.get("error") or "").strip()
    if error:
        return error
    if payload.get("success") is False or payload.get("ok") is False:
        return "tool reported failure"
    return None


def _is_success(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if _tool_error(payload):
        return False
    if "success" in payload:
        return bool(payload.get("success"))
    if "ok" in payload:
        return bool(payload.get("ok"))
    return True


def _has_desktop_target(
    *,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
) -> bool:
    return any((window_id, role, title, identifier, value_contains))


def _observation_summary(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "preferred_surface": observation.get("preferred_surface"),
        "available_surfaces": observation.get("available_surfaces", []),
        **({"errors": observation.get("errors", {})} if observation.get("errors") else {}),
    }


def _action_payload(
    *,
    action: str,
    surface: str | None,
    strategy: str,
    observation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "action": action,
        "surface": surface,
        "strategy": strategy,
        "observation": _observation_summary(observation),
        "result": result,
        "success": _is_success(result),
    }
    error = _tool_error(result)
    if error:
        payload["error"] = error
    return payload


async def _resolve_observation(
    *,
    observation: dict[str, Any] | None,
    selector: Optional[str],
    app_name: Optional[str],
    window_id: Optional[str],
    role: Optional[str],
    title: Optional[str],
    identifier: Optional[str],
    value_contains: Optional[str],
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    if observation is not None:
        return observation

    return await computer_observe(
        include_current_tab=selector is not None,
        include_frontmost_app=True,
        include_windows=True,
        ax_app_name=app_name,
        ax_window_id=window_id,
        ax_role=role,
        ax_title=title,
        ax_identifier=identifier,
        ax_value_contains=value_contains,
        tool_context=tool_context,
    )


async def computer_observe(
    include_current_tab: bool = True,
    include_current_tab_text: bool = False,
    current_tab_selector: Optional[str] = None,
    include_frontmost_app: bool = True,
    include_windows: bool = True,
    include_screenshot: bool = False,
    include_ax_snapshot: bool = False,
    ax_app_name: Optional[str] = None,
    ax_window_id: Optional[str] = None,
    ax_role: Optional[str] = None,
    ax_title: Optional[str] = None,
    ax_identifier: Optional[str] = None,
    ax_value_contains: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Collect browser/desktop observations in a single browser-first bundle."""

    errors: dict[str, str] = {}
    result: dict[str, Any] = {
        "mode": "browser_first",
        "surface_order": ["current_tab", "desktop"],
        "available_surfaces": [],
    }

    if include_current_tab:
        current_tab = await current_tab_info(tool_context=tool_context)
        result["current_tab"] = current_tab
        error = _tool_error(current_tab)
        if error:
            errors["current_tab"] = error
        elif _is_success(current_tab):
            result["available_surfaces"].append("current_tab")

    if include_current_tab_text:
        current_tab_text = await current_tab_extract_text(
            selector=current_tab_selector,
            tool_context=tool_context,
        )
        result["current_tab_text"] = current_tab_text
        error = _tool_error(current_tab_text)
        if error:
            errors["current_tab_text"] = error

    if include_frontmost_app:
        frontmost_app = await desktop_view_frontmost_app(tool_context=tool_context)
        result["frontmost_app"] = frontmost_app
        error = _tool_error(frontmost_app)
        if error:
            errors["frontmost_app"] = error
        elif _is_success(frontmost_app) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_windows:
        windows = await desktop_view_windows(
            include_minimized=False,
            tool_context=tool_context,
        )
        result["windows"] = windows
        error = _tool_error(windows)
        if error:
            errors["windows"] = error
        elif _is_success(windows) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_screenshot:
        screenshot = await desktop_view_screenshot(tool_context=tool_context)
        result["screenshot"] = screenshot
        error = _tool_error(screenshot)
        if error:
            errors["screenshot"] = error
        elif _is_success(screenshot) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if any((ax_role, ax_title, ax_identifier, ax_value_contains, ax_window_id)):
        ax_find = await desktop_ax_find(
            app_name=ax_app_name,
            window_id=ax_window_id,
            role=ax_role,
            title=ax_title,
            identifier=ax_identifier,
            value_contains=ax_value_contains,
            tool_context=tool_context,
        )
        result["ax_find"] = ax_find
        error = _tool_error(ax_find)
        if error:
            errors["ax_find"] = error
        elif _is_success(ax_find) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if include_ax_snapshot:
        ax_snapshot = await desktop_ax_snapshot(
            app_name=ax_app_name,
            window_id=ax_window_id,
            tool_context=tool_context,
        )
        result["ax_snapshot"] = ax_snapshot
        error = _tool_error(ax_snapshot)
        if error:
            errors["ax_snapshot"] = error
        elif _is_success(ax_snapshot) and "desktop" not in result["available_surfaces"]:
            result["available_surfaces"].append("desktop")

    if errors:
        result["errors"] = errors

    result["success"] = bool(result["available_surfaces"])
    result["preferred_surface"] = (
        "current_tab"
        if "current_tab" in result["available_surfaces"]
        else "desktop"
        if "desktop" in result["available_surfaces"]
        else None
    )
    return result


async def computer_click(
    selector: Optional[str] = None,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    allow_managed_browser: bool = True,
    observation: dict[str, Any] | None = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Click the best available browser/desktop surface using browser-first fallback."""

    has_desktop_target = _has_desktop_target(
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if not selector and not has_desktop_target:
        return {
            "action": "click",
            "success": False,
            "error": "computer_click requires a CSS selector or desktop target fields",
        }

    observation = await _resolve_observation(
        observation=observation,
        selector=selector,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        tool_context=tool_context,
    )

    if selector and "current_tab" in observation.get("available_surfaces", []):
        result = await current_tab_click(selector, tool_context=tool_context)
        return _action_payload(
            action="click",
            surface="current_tab",
            strategy="current_tab_selector",
            observation=observation,
            result=result,
        )

    if has_desktop_target and "desktop" in observation.get("available_surfaces", []):
        result = await desktop_control_click(
            app_name=app_name,
            window_id=window_id,
            role=role,
            title=title,
            identifier=identifier,
            value_contains=value_contains,
            index=index,
            tool_context=tool_context,
        )
        return _action_payload(
            action="click",
            surface="desktop",
            strategy="desktop_selector",
            observation=observation,
            result=result,
        )

    if selector and allow_managed_browser:
        result = await browser_click(selector, tool_context=tool_context)
        return _action_payload(
            action="click",
            surface="browser",
            strategy="managed_browser_selector",
            observation=observation,
            result=result,
        )

    return _action_payload(
        action="click",
        surface=None,
        strategy="no_available_surface",
        observation=observation,
        result={
            "error": "No browser or desktop surface could satisfy computer_click",
            "success": False,
        },
    )


async def computer_fill(
    selector: Optional[str] = None,
    text: str = "",
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    allow_managed_browser: bool = True,
    observation: dict[str, Any] | None = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Fill the best available browser/desktop surface using browser-first fallback."""

    has_desktop_target = _has_desktop_target(
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if not selector and not has_desktop_target:
        return {
            "action": "fill",
            "success": False,
            "error": "computer_fill requires a CSS selector or desktop target fields",
        }

    observation = await _resolve_observation(
        observation=observation,
        selector=selector,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        tool_context=tool_context,
    )

    if selector and "current_tab" in observation.get("available_surfaces", []):
        result = await current_tab_fill(selector, text, tool_context=tool_context)
        return _action_payload(
            action="fill",
            surface="current_tab",
            strategy="current_tab_selector",
            observation=observation,
            result=result,
        )

    if has_desktop_target and "desktop" in observation.get("available_surfaces", []):
        result = await desktop_control_type(
            text=text,
            app_name=app_name,
            window_id=window_id,
            role=role,
            title=title,
            identifier=identifier,
            value_contains=value_contains,
            index=index,
            tool_context=tool_context,
        )
        return _action_payload(
            action="fill",
            surface="desktop",
            strategy="desktop_selector",
            observation=observation,
            result=result,
        )

    if selector and allow_managed_browser:
        result = await browser_fill(selector, text, tool_context=tool_context)
        return _action_payload(
            action="fill",
            surface="browser",
            strategy="managed_browser_selector",
            observation=observation,
            result=result,
        )

    return _action_payload(
        action="fill",
        surface=None,
        strategy="no_available_surface",
        observation=observation,
        result={
            "error": "No browser or desktop surface could satisfy computer_fill",
            "success": False,
        },
    )
