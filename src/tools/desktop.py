"""Desktop automation tools."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.bridges.desktop_bridge_client import get_desktop_client
from src.bridges.desktop_exec import execute_desktop_call
from src.config.settings import get_settings
from src.desktop import (
    DesktopAxFindRequest,
    DesktopAxSnapshotRequest,
    DesktopClickRequest,
    DesktopElementSelector,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopHotkeyRequest,
    DesktopLaunchAppRequest,
    DesktopScreenshotRequest,
    DesktopTypeRequest,
    DesktopDragRequest,
    DesktopWindowsRequest,
)
from src.security.audit import AuditEventType, get_audit_logger
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context


async def _check_desktop_policy(
    tool_name: str,
    args: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> tuple[Optional[str], Optional[str]]:
    if tool_context is None:
        return None, None

    ctx = resolve_tool_context(tool_context)
    engine = get_tool_policy_engine()
    action, reason = engine.evaluate(ctx["agent_name"], tool_name)
    if action == "allow":
        return None, None
    if action == "deny":
        return f"Tool blocked by policy: {reason}", None

    approved, response_reason, approval_token = await engine.request_approval_with_id(
        tool_name=tool_name,
        agent_name=ctx["agent_name"],
        args=args,
        session_id=ctx["session_id"],
        reason=reason,
    )
    if approved:
        return None, approval_token
    detail = response_reason or reason or "user rejected"
    return f"Tool approval denied: {detail}", approval_token


def _audit_desktop_event(
    *,
    event_type: AuditEventType,
    action: str,
    resource: str,
    result: str,
    metadata: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> None:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    get_audit_logger().log(
        event_type=event_type,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action=action,
        resource=resource,
        result=result,
        metadata=metadata,
    )


def _executor_metadata() -> dict[str, Any]:
    settings = get_settings()
    return {
        "executor": "desktop_bridge" if settings.desktop_bridge_enabled else "local_desktop",
    }


def _selector_from_fields(
    *,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
) -> DesktopElementSelector | None:
    if not any((window_id, role, title, identifier, value_contains)):
        return None
    return DesktopElementSelector(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )


async def desktop_view_windows(
    include_minimized: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopWindowsRequest(
        request_id=f"desktop-windows-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=None,
        include_minimized=include_minimized,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.view.windows",
        args={"include_minimized": include_minimized},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.windows(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_VIEW,
            action="windows",
            resource="desktop",
            result=(result.error if result else payload["error"]) or "error",
            metadata={**_executor_metadata(), "request_id": request.request_id},
            tool_context=tool_context,
        )
        return {"error": (result.error if result else payload["error"]) or "desktop query failed"}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_VIEW,
        action="windows",
        resource="desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "count": len(result.windows),
        },
        tool_context=tool_context,
    )
    return {"windows": [window.model_dump() for window in result.windows]}


async def desktop_view_frontmost_app(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopFrontmostAppRequest(
        request_id=f"desktop-frontmost-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=None,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.view.frontmost_app",
        args={},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.frontmost_app(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_VIEW,
            action="frontmost_app",
            resource="desktop",
            result=(result.error if result else payload["error"]) or "error",
            metadata={**_executor_metadata(), "request_id": request.request_id},
            tool_context=tool_context,
        )
        return {"error": (result.error if result else payload["error"]) or "desktop query failed"}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_VIEW,
        action="frontmost_app",
        resource=result.app_name or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "pid": result.pid,
        },
        tool_context=tool_context,
    )
    return {"app_name": result.app_name, "pid": result.pid}


async def desktop_view_screenshot(
    path: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_view_screenshot",
        {"path": path},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopScreenshotRequest(
        request_id=f"desktop-shot-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        path=path,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.view.screenshot",
        args={"path": path},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.screenshot(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error, "path": path},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop screenshot failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_VIEW,
            action="screenshot",
            resource=path or "desktop",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
            },
            tool_context=tool_context,
        )
        return {"error": error, "path": path}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_VIEW,
        action="screenshot",
        resource=result.path or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
            "width": result.width,
            "height": result.height,
        },
        tool_context=tool_context,
    )
    return {
        "path": result.path,
        "width": result.width,
        "height": result.height,
        "success": True,
    }


async def desktop_ax_snapshot(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_ax_snapshot",
        {"app_name": app_name, "window_id": window_id},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopAxSnapshotRequest(
        request_id=f"desktop-ax-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        app_name=app_name,
        window_id=window_id,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.ax.snapshot",
        args={"app_name": app_name, "window_id": window_id},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.ax_snapshot(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop ax snapshot failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_VIEW,
            action="ax_snapshot",
            resource=app_name or "desktop",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
                "window_id": window_id,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_VIEW,
        action="ax_snapshot",
        resource=app_name or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
            "window_id": window_id,
        },
        tool_context=tool_context,
    )
    return {"tree": result.tree}


async def desktop_ax_find(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if selector is None:
        return {"error": "desktop ax find requires a selector target"}
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopAxFindRequest(
        request_id=f"desktop-find-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=None,
        target=selector,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.ax.find",
        args={"selector": selector.model_dump(exclude_none=True) if selector else None},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.ax_find(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop ax find failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_VIEW,
            action="ax_find",
            resource=app_name or window_id or identifier or title or "desktop",
            result=error,
            metadata={**_executor_metadata(), "request_id": request.request_id},
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_VIEW,
        action="ax_find",
        resource=app_name or window_id or identifier or title or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "matched": result.matched,
        },
        tool_context=tool_context,
    )
    return {
        "matched": result.matched,
        "target": result.target.model_dump() if result.target else None,
    }


async def desktop_control_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    click_count: int = 1,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if selector is None and (x is None or y is None):
        return {"error": "desktop click requires coordinates or a selector target"}
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_click",
        {
            "x": x,
            "y": y,
            "button": button,
            "click_count": click_count,
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopClickRequest(
        request_id=f"desktop-click-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        x=x,
        y=y,
        button=button,
        click_count=click_count,
        target=selector,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.click",
        args={
            "x": x,
            "y": y,
            "button": button,
            "click_count": click_count,
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        get_client=get_desktop_client,
        invoke=lambda client, req: client.click(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop click failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_CONTROL,
            action="click",
            resource=f"{x},{y}",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
                "button": button,
                "click_count": click_count,
                "selector": selector.model_dump(exclude_none=True) if selector else None,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="click",
        resource=f"{x},{y}",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
            "button": button,
            "click_count": click_count,
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        tool_context=tool_context,
    )
    return {
        "success": True,
        "target": result.target.model_dump() if result.target else None,
    }


async def desktop_control_type(
    text: str,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_type",
        {
            "text": text,
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopTypeRequest(
        request_id=f"desktop-type-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        text=text,
        target=selector,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.type",
        args={
            "text": text,
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        get_client=get_desktop_client,
        invoke=lambda client, req: client.type_text(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop type failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_CONTROL,
            action="type",
            resource="desktop",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
                "length": len(text),
                "selector": selector.model_dump(exclude_none=True) if selector else None,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="type",
        resource="desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
            "length": len(text),
            "selector": selector.model_dump(exclude_none=True) if selector else None,
        },
        tool_context=tool_context,
    )
    return {
        "success": True,
        "target": result.target.model_dump() if result.target else None,
    }


async def desktop_control_launch_app(
    app_name: Optional[str] = None,
    bundle_id: Optional[str] = None,
    wait_for_focus: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_launch_app",
        {
            "app_name": app_name,
            "bundle_id": bundle_id,
            "wait_for_focus": wait_for_focus,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopLaunchAppRequest(
        request_id=f"desktop-launch-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        app_name=app_name,
        bundle_id=bundle_id,
        wait_for_focus=wait_for_focus,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.launch_app",
        args={
            "app_name": app_name,
            "bundle_id": bundle_id,
            "wait_for_focus": wait_for_focus,
        },
        get_client=get_desktop_client,
        invoke=lambda client, req: client.launch_app(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop launch failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_CONTROL,
            action="launch_app",
            resource=app_name or bundle_id or "desktop",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
                "wait_for_focus": wait_for_focus,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="launch_app",
        resource=app_name or bundle_id or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
            "wait_for_focus": wait_for_focus,
        },
        tool_context=tool_context,
    )
    return {
        "success": True,
        "target": result.target.model_dump() if result.target else None,
    }


async def desktop_control_focus_window(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    title: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_focus_window",
        {"app_name": app_name, "window_id": window_id, "title": title},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopFocusWindowRequest(
        request_id=f"desktop-focus-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        app_name=app_name,
        window_id=window_id,
        title=title,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.focus_window",
        args={"app_name": app_name, "window_id": window_id, "title": title},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.focus_window(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop focus failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_CONTROL,
            action="focus_window",
            resource=title or window_id or app_name or "desktop",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="focus_window",
        resource=title or window_id or app_name or "desktop",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
        },
        tool_context=tool_context,
    )
    return {
        "success": True,
        "target": result.target.model_dump() if result.target else None,
    }


async def desktop_control_hotkey(
    keys: list[str],
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_hotkey",
        {"keys": keys},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopHotkeyRequest(
        request_id=f"desktop-hotkey-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        keys=keys,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.hotkey",
        args={"keys": keys},
        get_client=get_desktop_client,
        invoke=lambda client, req: client.hotkey(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        return {"error": (result.error if result else payload["error"]) or "desktop hotkey failed"}
    return {"success": True}


async def desktop_control_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_drag",
        {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = DesktopDragRequest(
        request_id=f"desktop-drag-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
    )
    result, payload = await execute_desktop_call(
        request=request,
        tool_name="desktop.control.drag",
        args={
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        },
        get_client=get_desktop_client,
        invoke=lambda client, req: client.drag(req),
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=_executor_metadata(),
    )
    if result is None or not result.ok:
        error = (result.error if result else payload["error"]) or "desktop drag failed"
        _audit_desktop_event(
            event_type=AuditEventType.DESKTOP_CONTROL,
            action="drag",
            resource=f"{start_x},{start_y}->{end_x},{end_y}",
            result=error,
            metadata={
                **_executor_metadata(),
                "request_id": request.request_id,
                "approval_token": approval_token,
            },
            tool_context=tool_context,
        )
        return {"error": error}

    _audit_desktop_event(
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="drag",
        resource=f"{start_x},{start_y}->{end_x},{end_y}",
        result="success",
        metadata={
            **_executor_metadata(),
            "request_id": request.request_id,
            "approval_token": approval_token,
        },
        tool_context=tool_context,
    )
    return {"success": True}
