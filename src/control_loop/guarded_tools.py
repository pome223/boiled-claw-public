"""
Guarded Tools — boiled-claw v2

ToolContext.state で approval:status を確認してから実行する
policy-aware tool ラッパー。

Executor agent にアタッチし、approved plan の範囲外の実行を防ぐ。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from google.adk.tools import ToolContext

from src.gateway.routing import targets_user_browser
from src.runtime.state_keys import StateKeys

_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}
_IMPLICIT_PLAN_CAPABILITIES = {
    "desktop.view.windows": {
        "desktop.control.launch_app",
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.view.frontmost_app": {
        "desktop.control.launch_app",
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.wait.window": {
        "desktop.control.launch_app",
        "desktop.control.focus_window",
    },
    "desktop.ax.find": {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.wait.element",
    },
    "desktop.wait.element": {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.ax.find",
    },
}
_CURRENT_BROWSER_ALLOWED_HOTKEYS = {
    ("control", "l"),
    ("enter",),
    ("l", "meta"),
}
_CURRENT_BROWSER_NEW_TAB_HOTKEYS = {
    ("control", "t"),
    ("meta", "t"),
}
_HOTKEY_ALIASES = {
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "arrowup": "up",
    "cmd": "meta",
    "command": "meta",
    "control": "control",
    "ctrl": "control",
    "return": "enter",
}
_CURRENT_BROWSER_HOTKEY_REWRITES = {
    ("control", "e"): ["control", "l"],
    ("control", "k"): ["control", "l"],
    ("e", "meta"): ["meta", "l"],
    ("k", "meta"): ["meta", "l"],
}
_KNOWN_BROWSER_APPS = {
    "Google Chrome",
    "Chromium",
    "Safari",
    "Arc",
    "Firefox",
    "Brave Browser",
    "Microsoft Edge",
}
_PRESERVE_CONTROL_UI_MARKER = "preserve that tab and open a new tab in the same browser window"
_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY = "temp:current_browser_address_bar_focused"
_CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS = (
    "boiled-claw Control UI",
    "boiled-claw",
)
_CURRENT_BROWSER_SEARCH_KEYWORDS = {
    "search",
    "weather",
    "pollen",
    "latest",
    "latest news",
    "forecast",
    "research",
    "調べ",
    "検索",
    "花粉",
    "天気",
    "最新",
}


def _check_approval(tool_context: ToolContext, capability: str) -> None:
    """
    approval:status を確認し、未承認なら PermissionError を上げる。
    """
    status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
    if status not in _APPROVED_STATUSES:
        raise PermissionError(
            f"Tool '{capability}' blocked: plan approval status is '{status}'. "
            "Requires policy_approved, human_approved, or auto_approved."
        )


def _check_capability_in_plan(
    tool_context: ToolContext, capability_name: str
) -> None:
    """
    plan:approved に capability が含まれているか確認する。
    """
    import json

    raw_plan = tool_context.state.get(StateKeys.PLAN_APPROVED)
    if raw_plan is None:
        raise PermissionError("No approved plan in session state.")

    try:
        plan = raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PermissionError("Approved plan is not valid JSON.") from exc
    required = {
        cap.get("name", "") for cap in plan.get("required_capabilities", [])
    }
    implied_by = _IMPLICIT_PLAN_CAPABILITIES.get(capability_name, set())
    if capability_name not in required and not (required & implied_by):
        raise PermissionError(
            f"Capability '{capability_name}' is not in the approved plan."
        )


def _memory_entry_to_result(entry: Any) -> dict[str, Any]:
    content = getattr(entry, "content", None)
    parts = getattr(content, "parts", None) or []
    text = "\n".join(
        part.text for part in parts if getattr(part, "text", None)
    ).strip()
    return {
        "content": text,
        "author": getattr(entry, "author", None),
        "timestamp": getattr(entry, "timestamp", None),
    }


def _is_current_browser_task(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    return isinstance(goal, str) and targets_user_browser(goal)


def _normalize_hotkeys(keys: list[str]) -> tuple[str, ...]:
    normalized = []
    for key in keys:
        value = _HOTKEY_ALIASES.get(key.strip().lower(), key.strip().lower())
        normalized.append(value)
    return tuple(sorted(item for item in normalized if item))


def _rewrite_current_browser_hotkeys(keys: list[str]) -> list[str]:
    normalized = _normalize_hotkeys(keys)
    return list(_CURRENT_BROWSER_HOTKEY_REWRITES.get(normalized, keys))


def _allows_current_browser_new_tab(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    constraints = tool_context.state.get(StateKeys.TASK_CONSTRAINTS, [])
    if not isinstance(constraints, list):
        return False
    return any(
        _PRESERVE_CONTROL_UI_MARKER in str(item).lower()
        for item in constraints
    )


def _current_browser_goal_text(tool_context: ToolContext | None) -> str:
    if tool_context is None:
        return ""
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    return goal if isinstance(goal, str) else ""


def _is_current_browser_search_task(tool_context: ToolContext | None) -> bool:
    goal = _current_browser_goal_text(tool_context).lower()
    return any(keyword in goal for keyword in _CURRENT_BROWSER_SEARCH_KEYWORDS)


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("http://", "https://")) or "://" in lowered


def _rewrite_current_browser_address_bar_text(
    text: str,
    tool_context: ToolContext | None,
) -> str:
    if tool_context is None:
        return text
    focused = bool(tool_context.state.get(_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY))
    tool_context.state[_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY] = False
    if not _is_current_browser_search_task(tool_context):
        return text
    stripped = text.strip()
    if not stripped or _looks_like_url(stripped):
        return text
    # ToolContext.state mutations are not guaranteed to survive every model/tool
    # boundary, so treat selector-less text entry in current-browser search tasks
    # as an address-bar query even if the transient "focused" flag was dropped.
    if not focused and len(stripped) > 120:
        return text
    return f"https://www.google.com/search?q={quote_plus(stripped)}"


async def _focus_control_ui_browser_window(
    *,
    app_name: str,
) -> dict[str, Any] | None:
    from src.tools.desktop import desktop_control_focus_window

    for title_hint in _CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS:
        result = await desktop_control_focus_window(
            app_name=app_name,
            title=title_hint,
        )
        if result.get("success") or result.get("ok"):
            return result
    return None


# ── Guarded tool implementations ──────────────────────────────────────────


async def guarded_web_search(
    query: str,
    tool_context: ToolContext,
) -> dict:
    """web.search capability が承認済みの場合のみ Web 検索を実行する。"""
    _check_approval(tool_context, "web.search")
    _check_capability_in_plan(tool_context, "web.search")

    from src.tools.web_search import web_search
    return await web_search(query)


async def guarded_read_file(
    path: str,
    tool_context: ToolContext,
) -> dict:
    """file.read capability が承認済みの場合のみファイルを読む。"""
    _check_approval(tool_context, "file.read")
    _check_capability_in_plan(tool_context, "file.read")

    from src.tools.file_manager import read_file
    return await read_file(path)


async def guarded_write_file(
    path: str,
    content: str,
    tool_context: ToolContext,
) -> dict:
    """
    file.write capability が承認済みの場合のみファイルを書く。
    HIGH リスク: human_approved が必要。
    """
    status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
    if status != "human_approved":
        raise PermissionError(
            "file.write requires human_approved status. "
            f"Current status: '{status}'"
        )
    _check_capability_in_plan(tool_context, "file.write")

    from src.tools.file_manager import write_file
    return await write_file(path, content)


async def guarded_memory_read(
    query: str | None = None,
    tags: str | None = None,
    limit: int = 10,
    tool_context: ToolContext | None = None,
) -> dict:
    """memory.read capability が承認済みの場合のみメモリを検索する。"""
    if tool_context is not None:
        _check_approval(tool_context, "memory.read")
        _check_capability_in_plan(tool_context, "memory.read")

        if query and not tags:
            try:
                response = await tool_context.search_memory(query)
            except ValueError:
                response = None
            else:
                memories = response.memories[: max(1, limit)]
                return {
                    "results": [_memory_entry_to_result(entry) for entry in memories],
                    "count": len(memories),
                    "query": query,
                    "tags": None,
                    "source": "adk_memory",
                    "success": True,
                }

    from src.tools.memory import memory_search
    return await memory_search(query=query, tags=tags, limit=limit)


async def guarded_browser_navigate(
    url: str,
    tool_context: ToolContext,
) -> dict:
    """browser.navigate capability が承認済みの場合のみブラウザを操作する。"""
    _check_approval(tool_context, "browser.navigate")
    _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_navigate
    return await browser_navigate(url)


async def guarded_browser_extract_text(
    selector: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでテキスト抽出を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_extract_text
    return await browser_extract_text(selector)


async def guarded_browser_click(
    selector: str,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでクリックを許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_click
    return await browser_click(selector, timeout=timeout)


async def guarded_browser_fill(
    selector: str,
    text: str,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとで入力を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_fill
    return await browser_fill(selector, text, timeout=timeout)


async def guarded_browser_press(
    key: str,
    selector: str | None = None,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでキー送信を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_press
    return await browser_press(key, selector=selector, timeout=timeout)


async def guarded_desktop_view_windows(
    include_minimized: bool = False,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.view.windows")
        _check_capability_in_plan(tool_context, "desktop.view.windows")

    from src.tools.desktop import desktop_view_windows
    return await desktop_view_windows(include_minimized=include_minimized)


async def guarded_desktop_wait_window(
    app_name: str | None = None,
    window_id: str | None = None,
    title: str | None = None,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.wait.window")
        _check_capability_in_plan(tool_context, "desktop.wait.window")

    from src.tools.desktop import desktop_wait_window
    return await desktop_wait_window(
        app_name=app_name,
        window_id=window_id,
        title=title,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


async def guarded_desktop_view_frontmost_app(
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.view.frontmost_app")
        _check_capability_in_plan(tool_context, "desktop.view.frontmost_app")

    from src.tools.desktop import desktop_view_frontmost_app
    return await desktop_view_frontmost_app()


async def guarded_desktop_view_screenshot(
    path: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.view.screenshot requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.view.screenshot")

    from src.tools.desktop import desktop_view_screenshot
    return await desktop_view_screenshot(path=path)


async def guarded_desktop_ax_find(
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.ax.find")
        _check_capability_in_plan(tool_context, "desktop.ax.find")

    from src.tools.desktop import desktop_ax_find
    return await desktop_ax_find(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )


async def guarded_desktop_wait_element(
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.wait.element")
        _check_capability_in_plan(tool_context, "desktop.wait.element")

    from src.tools.desktop import desktop_wait_element
    return await desktop_wait_element(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


async def guarded_desktop_ax_snapshot(
    app_name: str | None = None,
    window_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.ax.snapshot requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.ax.snapshot")

    from src.tools.desktop import desktop_ax_snapshot
    return await desktop_ax_snapshot(app_name=app_name, window_id=window_id)


async def guarded_desktop_control_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    click_count: int = 1,
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.click requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.click")

    from src.tools.desktop import desktop_control_click
    return await desktop_control_click(
        x=x,
        y=y,
        button=button,
        click_count=click_count,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )


async def guarded_desktop_control_type(
    text: str,
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if (
            _is_current_browser_task(tool_context)
            and not any((app_name, window_id, role, title, identifier, value_contains))
        ):
            text = _rewrite_current_browser_address_bar_text(text, tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.type requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.type")

    from src.tools.desktop import desktop_control_type
    return await desktop_control_type(
        text=text,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )


async def guarded_desktop_control_launch_app(
    app_name: str | None = None,
    bundle_id: str | None = None,
    wait_for_focus: bool = True,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            from src.tools.desktop import (
                desktop_control_focus_window,
                desktop_view_windows,
            )

            candidate_app = app_name.strip() if isinstance(app_name, str) else ""
            if candidate_app and candidate_app not in _KNOWN_BROWSER_APPS:
                raise PermissionError(
                    "desktop.control.launch_app is not allowed for current-browser tasks. "
                    "Use the existing frontmost browser window instead."
                )

            if not candidate_app:
                windows = await desktop_view_windows(include_minimized=False)
                for window in windows.get("windows", []):
                    window_app = str(window.get("app_name", "")).strip()
                    if window_app in _KNOWN_BROWSER_APPS:
                        candidate_app = window_app
                        break

            if candidate_app:
                if _allows_current_browser_new_tab(tool_context):
                    focused_control_ui = await _focus_control_ui_browser_window(
                        app_name=candidate_app
                    )
                    if focused_control_ui is not None:
                        return focused_control_ui
                return await desktop_control_focus_window(app_name=candidate_app)

            raise PermissionError(
                "desktop.control.launch_app is not allowed for current-browser tasks. "
                "No existing browser window could be identified."
            )
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.launch_app requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.launch_app")

    from src.tools.desktop import desktop_control_launch_app
    return await desktop_control_launch_app(
        app_name=app_name,
        bundle_id=bundle_id,
        wait_for_focus=wait_for_focus,
    )


async def guarded_desktop_control_focus_window(
    app_name: str | None = None,
    window_id: str | None = None,
    title: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.focus_window requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.focus_window")
        if (
            _is_current_browser_task(tool_context)
            and _allows_current_browser_new_tab(tool_context)
            and not window_id
            and not title
            and isinstance(app_name, str)
            and app_name.strip() in _KNOWN_BROWSER_APPS
        ):
            focused_control_ui = await _focus_control_ui_browser_window(
                app_name=app_name.strip()
            )
            if focused_control_ui is not None:
                return focused_control_ui

    from src.tools.desktop import desktop_control_focus_window
    return await desktop_control_focus_window(
        app_name=app_name,
        window_id=window_id,
        title=title,
    )


async def guarded_desktop_control_hotkey(
    keys: list[str],
    tool_context: ToolContext | None = None,
) -> dict:
    effective_keys = keys
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            effective_keys = _rewrite_current_browser_hotkeys(keys)
            normalized_keys = _normalize_hotkeys(effective_keys)
            allow_new_tab = _allows_current_browser_new_tab(tool_context)
            if (
                normalized_keys not in _CURRENT_BROWSER_ALLOWED_HOTKEYS
                and not (
                    allow_new_tab
                    and normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS
                )
            ):
                raise PermissionError(
                    "Only focus-address-bar or submit hotkeys are allowed for "
                    f"current-browser tasks. attempted={normalized_keys}"
                )
            if (
                normalized_keys in _CURRENT_BROWSER_ALLOWED_HOTKEYS
                or normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS
            ):
                tool_context.state[_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY] = (
                    normalized_keys != ("enter",)
                )
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.hotkey requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.hotkey")

    from src.tools.desktop import desktop_control_hotkey
    return await desktop_control_hotkey(keys=effective_keys)


async def guarded_desktop_control_scroll(
    delta_x: int = 0,
    delta_y: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.scroll requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.scroll")

    from src.tools.desktop import desktop_control_scroll
    return await desktop_control_scroll(delta_x=delta_x, delta_y=delta_y)


async def guarded_desktop_control_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.drag requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.drag")

    from src.tools.desktop import desktop_control_drag
    return await desktop_control_drag(
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
    )
