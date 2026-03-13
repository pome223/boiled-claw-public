"""
Guarded Tools — boiled-claw v2

ToolContext.state で approval:status を確認してから実行する
policy-aware tool ラッパー。

Executor agent にアタッチし、approved plan の範囲外の実行を防ぐ。
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from src.runtime.state_keys import StateKeys

_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}


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
    if capability_name not in required:
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


async def guarded_desktop_view_windows(
    include_minimized: bool = False,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.view.windows")
        _check_capability_in_plan(tool_context, "desktop.view.windows")

    from src.tools.desktop import desktop_view_windows
    return await desktop_view_windows(include_minimized=include_minimized)


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
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.hotkey requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.hotkey")

    from src.tools.desktop import desktop_control_hotkey
    return await desktop_control_hotkey(keys=keys)


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
