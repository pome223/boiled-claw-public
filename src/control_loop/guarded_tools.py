"""
Guarded Tools — boiled-claw v2

ToolContext.state で approval:status を確認してから実行する
policy-aware tool ラッパー。

Executor agent にアタッチし、approved plan の範囲外の実行を防ぐ。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote_plus, urlsplit

from google.adk.tools import ToolContext

from src.config.settings import get_settings
from src.gateway.routing import targets_user_browser
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import (
    SPREADSHEET_KEYWORDS,
    prefers_isolated_browser_for_goal,
)
from src.security.audit import AuditEventType, get_audit_logger
from src.security.network import is_loopback_host
from src.tools.context import resolve_tool_context

_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}
_IMPLICIT_PLAN_CAPABILITIES = {
    "desktop.view.windows": {
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.view.frontmost_app": {
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.wait.window": {
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
# Keep current-tab planning coarse-grained for now, mirroring the existing
# browser.navigate umbrella capability across this tool family.
_CURRENT_TAB_CAPABILITY = "current_tab.navigate"
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
_CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT = 1
_CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT_MAX = 2
_CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS = 5
_CURRENT_BROWSER_NEW_TAB_VERIFY_DELAY_SECONDS = 0.4
_CURRENT_BROWSER_NEW_TAB_STEP_MARKERS = (
    "new tab",
    "another new tab",
    "新しいタブ",
    "新規タブ",
)
_CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS = (
    "boiled-claw Control UI",
    "boiled-claw",
)
_ADDRESS_BAR_FALLBACK_QUERY_MAX_CHARS = 120
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
_CURRENT_BROWSER_SAFE_SEARCH_HOST_MARKERS = (
    "google.com/search",
    "www.google.com/search",
    "google.co.jp/search",
    "www.google.co.jp/search",
)
_PLAYBACK_TASK_KEYWORDS = {
    "djay",
    "spotify",
    "apple music",
    "itunes",
    "music",
    "song",
    "track",
    "playlist",
    "playback",
    "audio",
    "media",
    "再生",
    "停止",
    "止めて",
    "一時停止",
    "曲",
    "楽曲",
    "音楽",
    "プレイリスト",
    "かけて",
    "流して",
}
_PLAYBACK_APP_NAME_HINTS = (
    ("djay", "djay Pro"),
    ("spotify", "Spotify"),
    ("apple music", "Music"),
    ("itunes", "Music"),
    ("music", "Music"),
)


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
    plan = _approved_plan(tool_context)
    if plan is None:
        raise PermissionError("No approved plan in session state.")
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
    if (
        isinstance(goal, str)
        and targets_user_browser(goal)
        and not prefers_isolated_browser_for_goal(goal)
    ):
        return True
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return False
    required = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    return "current_tab.navigate" in required


def _approved_plan(tool_context: ToolContext | None) -> dict[str, Any] | None:
    if tool_context is None:
        return None
    raw_plan = tool_context.state.get(StateKeys.PLAN_APPROVED)
    if raw_plan is None:
        return None
    try:
        return raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PermissionError("Approved plan is not valid JSON.") from exc


def _plan_allows_capability(
    tool_context: ToolContext | None,
    capability_name: str,
) -> bool:
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return False
    required = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    implied_by = _IMPLICIT_PLAN_CAPABILITIES.get(capability_name, set())
    return capability_name in required or bool(required & implied_by)


def _current_browser_new_tab_count(tool_context: ToolContext | None) -> int:
    if tool_context is None:
        return 0
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _current_browser_new_tab_limit(tool_context: ToolContext | None) -> int:
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT

    explicit_new_tab_steps = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        chunks: list[str] = []
        for key in ("title", "description"):
            value = step.get(key)
            if isinstance(value, str):
                chunks.append(value.lower())
        haystack = " ".join(chunks)
        if any(marker in haystack for marker in _CURRENT_BROWSER_NEW_TAB_STEP_MARKERS):
            explicit_new_tab_steps += 1

    if explicit_new_tab_steps <= 0:
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT
    return max(
        _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT,
        min(explicit_new_tab_steps, _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT_MAX),
    )


def _current_browser_opened_tab_ids(tool_context: ToolContext | None) -> set[int]:
    if tool_context is None:
        return set()
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS, [])
    if not isinstance(raw, list):
        return set()
    tab_ids: set[int] = set()
    for item in raw:
        try:
            tab_ids.add(int(item))
        except (TypeError, ValueError):
            continue
    return tab_ids


def _current_browser_opened_tab_order(tool_context: ToolContext | None) -> list[int]:
    if tool_context is None:
        return []
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS, [])
    if not isinstance(raw, list):
        return []
    ordered: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            normalized = int(item)
        except (TypeError, ValueError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _current_browser_preferred_tab_id(tool_context: ToolContext | None) -> int | None:
    ordered = _current_browser_opened_tab_order(tool_context)
    if not ordered:
        return None
    return ordered[-1]


def _remember_current_browser_opened_tab(
    tool_context: ToolContext | None,
    tab_id: Any,
) -> None:
    if tool_context is None:
        return
    try:
        normalized = int(tab_id)
    except (TypeError, ValueError):
        return
    current = [item for item in _current_browser_opened_tab_order(tool_context) if item != normalized]
    current.append(normalized)
    tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] = current
    tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = normalized


async def _wait_for_current_browser_tab_verification(
    tool_context: ToolContext | None,
    previous_tab_id: Any | None = None,
) -> dict[str, Any]:
    from src.tools.current_tab import current_tab_info

    try:
        expected_previous_tab_id = int(previous_tab_id)
    except (TypeError, ValueError):
        expected_previous_tab_id = None

    last_info: dict[str, Any] = {}
    for attempt in range(_CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS):
        info = await current_tab_info(tool_context=tool_context)
        if isinstance(info, dict):
            last_info = info
            if info.get("success"):
                try:
                    current_tab_id = int(info.get("tab_id"))
                except (TypeError, ValueError):
                    current_tab_id = None
                if (
                    expected_previous_tab_id is None
                    or current_tab_id is None
                    or current_tab_id != expected_previous_tab_id
                ):
                    return info
            else:
                error_text = str(info.get("error") or "").lower()
                if "disconnected" not in error_text:
                    return info
        if attempt + 1 < _CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS:
            await asyncio.sleep(_CURRENT_BROWSER_NEW_TAB_VERIFY_DELAY_SECONDS)
    return last_info


def _is_desktop_playback_task(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    plan = _approved_plan(tool_context) or {}
    chunks: list[str] = []
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    if isinstance(goal, str):
        chunks.append(goal)
    for value in (
        plan.get("goal"),
        plan.get("plan_id"),
    ):
        if isinstance(value, str):
            chunks.append(value)
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for key in ("title", "description"):
            value = step.get(key)
            if isinstance(value, str):
                chunks.append(value)
        expected = step.get("expected_outputs", [])
        if isinstance(expected, list):
            chunks.extend(str(item) for item in expected)
    haystack = " ".join(chunks).lower()
    return any(keyword in haystack for keyword in _PLAYBACK_TASK_KEYWORDS)


def _playback_app_name_hint(
    tool_context: ToolContext | None,
    app_name: str | None,
) -> str | None:
    if isinstance(app_name, str) and app_name.strip():
        return app_name.strip()
    plan = _approved_plan(tool_context) or {}
    chunks: list[str] = []
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "") if tool_context is not None else ""
    if isinstance(goal, str):
        chunks.append(goal)
    for value in (plan.get("goal"), plan.get("plan_id")):
        if isinstance(value, str):
            chunks.append(value)
    haystack = " ".join(chunks).lower()
    for marker, resolved_name in _PLAYBACK_APP_NAME_HINTS:
        if marker in haystack:
            return resolved_name
    return None


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


def _is_current_browser_spreadsheet_task(tool_context: ToolContext | None) -> bool:
    goal = _current_browser_goal_text(tool_context).lower()
    return any(keyword in goal for keyword in SPREADSHEET_KEYWORDS)


def _remember_current_browser_spreadsheet_target(
    tool_context: ToolContext | None,
    target: Any,
) -> None:
    if tool_context is None or not _is_current_browser_spreadsheet_task(tool_context):
        return
    if not isinstance(target, dict):
        return
    normalized: dict[str, Any] = {}
    for key in ("app_name", "window_id", "role", "title", "identifier"):
        value = str(target.get(key) or "").strip()
        if value:
            normalized[key] = value
    if normalized:
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET] = normalized


def _current_browser_spreadsheet_target(
    tool_context: ToolContext | None,
) -> dict[str, Any] | None:
    if tool_context is None:
        return None
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET)
    if not isinstance(raw, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in ("app_name", "window_id", "role", "title", "identifier"):
        value = str(raw.get(key) or "").strip()
        if value:
            normalized[key] = value
    return normalized or None


def _is_safe_current_browser_destination(
    *,
    tool_context: ToolContext | None,
    url: str,
    title: str,
) -> bool:
    lowered_url = url.lower()
    lowered_title = title.lower()
    if _is_current_browser_spreadsheet_task(tool_context):
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "sheets.new":
            return True
        if host != "docs.google.com":
            return False
        if path.startswith("/spreadsheets/create"):
            return True
        return path.startswith("/spreadsheets/d/") or (
            path.startswith("/spreadsheets/u/") and "/d/" in path
        )
    if _is_current_browser_search_task(tool_context):
        return (
            any(marker in lowered_url for marker in _CURRENT_BROWSER_SAFE_SEARCH_HOST_MARKERS)
            or lowered_url.rstrip("/") in {"https://www.google.com", "https://google.com", "https://www.google.co.jp", "https://google.co.jp"}
            or "google" in lowered_title
        )
    return True


async def _assert_safe_current_browser_target(
    tool_context: ToolContext | None,
) -> None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return
    if not (
        _is_current_browser_search_task(tool_context)
        or _is_current_browser_spreadsheet_task(tool_context)
    ):
        return

    from src.tools.current_tab import current_tab_info

    await _activate_current_browser_task_tab(tool_context)

    info = await current_tab_info(tool_context=tool_context)
    if not isinstance(info, dict) or not info.get("success"):
        raise PermissionError(
            "Current-browser text/click actions require current_tab.info to verify the "
            "destination tab before interacting with page forms."
        )
    tab_id = info.get("tab_id")
    opened_tab_ids = _current_browser_opened_tab_ids(tool_context)
    try:
        normalized_tab_id = int(tab_id)
    except (TypeError, ValueError):
        normalized_tab_id = None
    if normalized_tab_id is None or normalized_tab_id not in opened_tab_ids:
        raise PermissionError(
            "Blocked current-browser interaction because the active tab was not "
            "opened by this task."
        )
    url = str(info.get("url") or "").strip()
    title = str(info.get("title") or "").strip()
    if not _is_safe_current_browser_destination(
        tool_context=tool_context,
        url=url,
        title=title,
    ):
        raise PermissionError(
            "Blocked current-browser interaction because the active tab does not match "
            f"the expected search/spreadsheet destination. url={url or '-'} title={title or '-'}"
        )


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
    # Cap the fallback so long arbitrary text does not get rewritten into a
    # search URL when the address-bar focus signal was likely lost.
    if not focused and len(stripped) > _ADDRESS_BAR_FALLBACK_QUERY_MAX_CHARS:
        return text
    return f"https://www.google.com/search?q={quote_plus(stripped)}"


def _matches_gateway_control_ui_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").strip()
    path = parsed.path.rstrip("/") or "/"
    if not host or path != "/chat":
        return False

    settings = get_settings()
    expected_host = (settings.gateway_host or "").strip().lower()
    expected_port = int(settings.gateway_port)
    actual_host = host.lower()
    actual_port = parsed.port
    if actual_port is None:
        actual_port = 443 if parsed.scheme == "https" else 80

    if actual_host == expected_host and actual_port == expected_port:
        return True
    if is_loopback_host(host) and is_loopback_host(settings.gateway_host):
        return actual_port == expected_port
    return False


def _is_loopback_control_ui_chat_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").strip()
    path = parsed.path.rstrip("/") or "/"
    return bool(host) and path == "/chat" and is_loopback_host(host)


def _audit_current_browser_navigation_redirect(
    *,
    tool_context: ToolContext | None,
    url: str,
    result: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    get_audit_logger().log(
        event_type=AuditEventType.BROWSER_NAVIGATE,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="redirect_to_current_tab",
        resource=url,
        result=result,
        metadata={
            "requested_tool": "browser.navigate",
            "effective_tool": "current_tab.navigate",
            "reason": "current_browser_task",
            **(metadata or {}),
        },
    )


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


def _is_current_browser_control_ui_tab(url: str, title: str) -> bool:
    lowered_title = title.lower()
    if _matches_gateway_control_ui_url(url) or _is_loopback_control_ui_chat_url(url):
        return True
    return any(
        hint.lower() in lowered_title
        for hint in _CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS
    )


async def _activate_current_browser_task_tab(
    tool_context: ToolContext | None,
) -> dict[str, Any] | None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return None
    target_tab_id = _current_browser_preferred_tab_id(tool_context)
    if target_tab_id is None:
        return None

    from src.tools.current_tab import current_tab_activate, current_tab_info

    current_info = await current_tab_info(tool_context=tool_context)
    if isinstance(current_info, dict) and current_info.get("success"):
        try:
            current_tab_id = int(current_info.get("tab_id"))
        except (TypeError, ValueError):
            current_tab_id = None
        if current_tab_id == target_tab_id:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = target_tab_id
            return current_info
        current_url = str(current_info.get("url") or "").strip()
        current_title = str(current_info.get("title") or "").strip()
        if current_tab_id in _current_browser_opened_tab_ids(tool_context):
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = current_tab_id
            return current_info
        if not _is_current_browser_control_ui_tab(current_url, current_title):
            return current_info

    activated = await current_tab_activate(target_tab_id, tool_context=tool_context)
    if isinstance(activated, dict) and activated.get("success"):
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = target_tab_id
        return activated
    return activated if isinstance(activated, dict) else None


async def _should_open_current_browser_task_tab(
    tool_context: ToolContext | None,
) -> bool:
    if tool_context is None:
        return False
    if not _plan_allows_capability(tool_context, _CURRENT_TAB_CAPABILITY):
        return False

    from src.tools.current_tab import current_tab_info

    current_info = await current_tab_info(tool_context=tool_context)
    if not isinstance(current_info, dict) or not current_info.get("success"):
        return False

    try:
        current_tab_id = int(current_info.get("tab_id"))
    except (TypeError, ValueError):
        current_tab_id = None

    if current_tab_id is not None and current_tab_id in _current_browser_opened_tab_ids(
        tool_context
    ):
        return False

    current_url = str(current_info.get("url") or "").strip()
    current_title = str(current_info.get("title") or "").strip()
    if not _is_current_browser_control_ui_tab(current_url, current_title):
        return False

    new_tab_count = _current_browser_new_tab_count(tool_context)
    new_tab_limit = _current_browser_new_tab_limit(tool_context)
    if new_tab_count >= new_tab_limit:
        quantity = "one" if new_tab_limit == 1 else str(new_tab_limit)
        noun = "tab is" if new_tab_limit == 1 else "tabs are"
        raise PermissionError(
            f"Only {quantity} preserved-browser {noun} allowed for this task."
        )
    return True


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
    if _is_current_browser_task(tool_context) and _plan_allows_capability(
        tool_context, _CURRENT_TAB_CAPABILITY
    ):
        try:
            result = await guarded_current_tab_navigate(url, tool_context=tool_context)
        except PermissionError as exc:
            _audit_current_browser_navigation_redirect(
                tool_context=tool_context,
                url=url,
                result="blocked",
                metadata={"error": str(exc)},
            )
            raise
        _audit_current_browser_navigation_redirect(
            tool_context=tool_context,
            url=url,
            result="redirected" if result.get("success") else "failed",
            metadata={
                "current_tab_success": bool(result.get("success")),
                "tab_id": result.get("tab_id"),
                "window_id": result.get("window_id"),
                **({"error": result.get("error")} if result.get("error") else {}),
            },
        )
        return result

    _check_approval(tool_context, "browser.navigate")
    _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_navigate
    return await browser_navigate(url)


async def guarded_current_tab_info(
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)

    from src.tools.current_tab import current_tab_info
    return await current_tab_info(tool_context=tool_context)


async def guarded_current_tab_navigate(
    url: str,
    timeout_ms: int = 15000,
    tool_context: ToolContext | None = None,
) -> dict:
    open_new_tab = False
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        await _activate_current_browser_task_tab(tool_context)
        open_new_tab = await _should_open_current_browser_task_tab(tool_context)

    from src.tools.current_tab import current_tab_navigate
    result = await current_tab_navigate(
        url,
        timeout_ms=timeout_ms,
        new_tab=open_new_tab,
        tool_context=tool_context,
    )
    if tool_context is not None and isinstance(result, dict) and result.get("success"):
        if open_new_tab:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] = (
                _current_browser_new_tab_count(tool_context) + 1
            )
        _remember_current_browser_opened_tab(tool_context, result.get("tab_id"))
    return result


async def guarded_current_tab_extract_text(
    selector: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        await _activate_current_browser_task_tab(tool_context)

    from src.tools.current_tab import current_tab_extract_text
    return await current_tab_extract_text(selector=selector, tool_context=tool_context)


async def guarded_current_tab_click(
    selector: str,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "current_tab.click requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)

    from src.tools.current_tab import current_tab_click
    return await current_tab_click(selector, tool_context=tool_context)


async def guarded_current_tab_fill(
    selector: str,
    text: str,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "current_tab.fill requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)

    from src.tools.current_tab import current_tab_fill
    return await current_tab_fill(selector, text, tool_context=tool_context)


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
    result = await desktop_ax_find(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if isinstance(result, dict) and result.get("matched"):
        _remember_current_browser_spreadsheet_target(tool_context, result.get("target"))
    return result


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
    result = await desktop_wait_element(
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
    if isinstance(result, dict) and result.get("matched"):
        _remember_current_browser_spreadsheet_target(tool_context, result.get("target"))
    return result


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
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.click requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.click")

    from src.tools.desktop import desktop_control_click
    result = await desktop_control_click(
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
    if isinstance(result, dict) and result.get("success"):
        _remember_current_browser_spreadsheet_target(tool_context, result.get("target"))
    return result


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
        if _is_current_browser_spreadsheet_task(tool_context):
            remembered_target = _current_browser_spreadsheet_target(tool_context)
            if remembered_target and not any((app_name, window_id, role, title, identifier, value_contains)):
                app_name = remembered_target.get("app_name") or app_name
                window_id = remembered_target.get("window_id") or window_id
                role = remembered_target.get("role") or role
                title = remembered_target.get("title") or title
                identifier = remembered_target.get("identifier") or identifier
        if (
            _is_current_browser_task(tool_context)
            and not any((app_name, window_id, role, title, identifier, value_contains))
        ):
            text = _rewrite_current_browser_address_bar_text(text, tool_context)
        elif _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.type requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.type")

    from src.tools.desktop import desktop_control_type
    result = await desktop_control_type(
        text=text,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if isinstance(result, dict) and result.get("success"):
        _remember_current_browser_spreadsheet_target(tool_context, result.get("target"))
    return result


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
        try:
            _check_capability_in_plan(tool_context, "desktop.control.launch_app")
        except PermissionError:
            if (
                _is_desktop_playback_task(tool_context)
                and _plan_allows_capability(tool_context, "desktop.control.focus_window")
            ):
                from src.tools.desktop import desktop_control_focus_window

                candidate_app = _playback_app_name_hint(tool_context, app_name)
                if candidate_app:
                    return await desktop_control_focus_window(app_name=candidate_app)
            raise

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
    verify_new_tab_after_hotkey = False
    previous_tab_id: int | None = None
    new_tab_count = 0
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
            if normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS:
                new_tab_count = _current_browser_new_tab_count(tool_context)
                new_tab_limit = _current_browser_new_tab_limit(tool_context)
                if new_tab_count >= new_tab_limit:
                    quantity = "one" if new_tab_limit == 1 else str(new_tab_limit)
                    noun = "hotkey is" if new_tab_limit == 1 else "hotkeys are"
                    raise PermissionError(
                        f"Only {quantity} new-tab {noun} allowed for a current-browser "
                        "task. Reuse the existing browser tab/window state for "
                        "retries instead of opening more tabs than the approved "
                        "plan requires."
                    )
                verify_new_tab_after_hotkey = True
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

        if verify_new_tab_after_hotkey:
            from src.tools.current_tab import current_tab_info

            previous_info = await current_tab_info(tool_context=tool_context)
            if isinstance(previous_info, dict) and previous_info.get("success"):
                try:
                    previous_tab_id = int(previous_info.get("tab_id"))
                except (TypeError, ValueError):
                    previous_tab_id = None

    from src.tools.desktop import desktop_control_hotkey
    result = await desktop_control_hotkey(keys=effective_keys)

    if verify_new_tab_after_hotkey and isinstance(result, dict) and (
        result.get("success") or result.get("ok")
    ):
        info = await _wait_for_current_browser_tab_verification(
            tool_context,
            previous_tab_id=previous_tab_id,
        )
        if not isinstance(info, dict) or not info.get("success"):
            raise PermissionError(
                "Failed to verify the newly opened browser tab. Refusing to "
                "continue interacting with an unverified tab."
            )
        try:
            verified_tab_id = int(info.get("tab_id"))
        except (TypeError, ValueError):
            verified_tab_id = None
        if previous_tab_id is not None and verified_tab_id == previous_tab_id:
            raise PermissionError(
                "Failed to confirm that the newly opened browser tab became active. "
                "Refusing to continue interacting with an unverified tab."
            )
        if tool_context is not None:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] = (
                new_tab_count + 1
            )
        _remember_current_browser_opened_tab(tool_context, info.get("tab_id"))

    return result


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
