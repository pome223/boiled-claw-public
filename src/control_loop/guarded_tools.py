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

    plan = raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
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
