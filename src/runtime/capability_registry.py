"""Thin runtime substrate for resources and capabilities."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.context import Context as ToolContext

from src.browser.current_tab_bridge import current_tab_bridge_enabled
from src.bridges.desktop_bridge_client import get_desktop_client
from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_schema import HostFileListRequest
from src.config.settings import get_settings
from src.security.policy import get_security_policy
from src.skills.base import BaseSkill, get_skill_registry
from src.skills.runtime import ensure_skills_loaded
from src.tools.browser import (
    PLAYWRIGHT_AVAILABLE,
    browser_click,
    browser_extract_text,
    browser_fill,
    browser_navigate,
    browser_press,
    browser_screenshot,
)
from src.tools.context import resolve_tool_context
from src.tools.control_ui_chat import control_ui_chat_send_message
from src.tools.current_tab import (
    current_tab_click,
    current_tab_extract_text,
    current_tab_fill,
    current_tab_info,
    current_tab_navigate,
)
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_drag,
    desktop_control_focus_window,
    desktop_control_hotkey,
    desktop_control_launch_app,
    desktop_control_scroll,
    desktop_control_type,
    desktop_runtime_clear_stop,
    desktop_runtime_status,
    desktop_runtime_stop,
    desktop_wait_element,
    desktop_wait_window,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)
from src.tools.file_manager import read_file, write_file
from src.tools.shell import run_shell_guarded

RuntimeInvoker = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RuntimeCapabilitySpec:
    name: str
    provider: str
    description: str
    risk: str
    requires_approval: bool
    transport: str
    bridge_capability: Optional[str]
    invoker: RuntimeInvoker


def _runtime_context(tool_context: Optional[ToolContext]) -> dict[str, str]:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    return {
        "session_id": ctx.get("session_id") or "runtime-session",
        "user_id": ctx.get("user_id") or "runtime-user",
        "agent_name": ctx.get("agent_name") or "runtime_registry",
    }


async def _skill_list_capability() -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    items = []
    for meta in registry.list_skills():
        items.append(
            {
                "name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "author": meta.author,
                "tags": meta.tags,
            }
        )
    return {"count": len(items), "skills": items}


async def _skill_execute_capability(
    name: str,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    skill = registry.get_skill(name)
    if not skill:
        return {"ok": False, "message": f"Skill not found: {name}"}

    payload = params or {}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "params must decode to object"}

    is_valid, reason = await skill.validate_input(**payload)
    if not is_valid:
        return {"ok": False, "message": reason or "Invalid input"}

    result = await skill.execute(**payload)
    return {"ok": True, "skill": name, "result": result}


async def _file_list_capability(
    path: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    settings = get_settings()
    if settings.host_bridge_enabled:
        client = get_host_bridge_client()
        if client is None:
            return {"success": False, "path": path, "error": "Host Bridge is not enabled."}

        ctx = _runtime_context(tool_context)
        request = HostFileListRequest(
            request_id=f"runtime-file-list-{uuid.uuid4().hex[:12]}",
            session_id=ctx["session_id"],
            user_id=ctx["user_id"],
            agent_name=ctx["agent_name"],
            path=path,
        )
        try:
            result = await client.list_files(request)
        except Exception as exc:  # pragma: no cover
            return {"success": False, "path": path, "error": str(exc)}

        return {
            "success": result.ok,
            "path": result.path or path,
            "entries": [entry.model_dump() for entry in result.entries],
            "count": len(result.entries),
            **({"error": result.error} if result.error else {}),
        }

    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "read")
    if not allowed:
        return {"success": False, "path": path, "error": f"Access denied: {reason}"}

    try:
        dir_path = Path(path).expanduser().resolve()
        entries = []
        for entry in sorted(dir_path.iterdir(), key=lambda item: item.name.lower()):
            try:
                stat = entry.stat()
                size = stat.st_size if entry.is_file() else 0
            except OSError:
                size = 0
            entries.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": size,
                }
            )
        return {
            "success": True,
            "path": str(dir_path),
            "entries": entries,
            "count": len(entries),
        }
    except FileNotFoundError:
        return {"success": False, "path": path, "error": f"Directory not found: {path}"}
    except NotADirectoryError:
        return {"success": False, "path": path, "error": f"Not a directory: {path}"}
    except PermissionError:
        return {"success": False, "path": path, "error": f"Permission denied: {path}"}
    except Exception as exc:  # pragma: no cover
        return {"success": False, "path": path, "error": str(exc)}


_CAPABILITY_SPECS: dict[str, RuntimeCapabilitySpec] = {
    spec.name: spec
    for spec in [
        RuntimeCapabilitySpec(
            name="skill.list",
            provider="skills",
            description="List loaded repository skills.",
            risk="low",
            requires_approval=False,
            transport="runtime",
            bridge_capability=None,
            invoker=_skill_list_capability,
        ),
        RuntimeCapabilitySpec(
            name="skill.execute",
            provider="skills",
            description="Inspect or execute a loaded repository skill.",
            risk="low",
            requires_approval=False,
            transport="runtime",
            bridge_capability=None,
            invoker=_skill_execute_capability,
        ),
        RuntimeCapabilitySpec(
            name="shell.run",
            provider="host",
            description="Run a guarded shell command through the configured host runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.shell.run",
            invoker=run_shell_guarded,
        ),
        RuntimeCapabilitySpec(
            name="file.read",
            provider="host",
            description="Read a guarded file through the configured host runtime.",
            risk="low",
            requires_approval=False,
            transport="host_bridge_or_local",
            bridge_capability="host.file.read",
            invoker=read_file,
        ),
        RuntimeCapabilitySpec(
            name="file.write",
            provider="host",
            description="Write a guarded file through the configured host runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.file.write",
            invoker=write_file,
        ),
        RuntimeCapabilitySpec(
            name="file.list",
            provider="host",
            description="List a guarded directory through the configured host runtime.",
            risk="low",
            requires_approval=False,
            transport="host_bridge_or_local",
            bridge_capability="host.file.list",
            invoker=_file_list_capability,
        ),
        RuntimeCapabilitySpec(
            name="browser.navigate",
            provider="browser",
            description="Navigate a browser page through the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.navigate",
            invoker=browser_navigate,
        ),
        RuntimeCapabilitySpec(
            name="browser.click",
            provider="browser",
            description="Click a selector in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.click",
            invoker=browser_click,
        ),
        RuntimeCapabilitySpec(
            name="browser.fill",
            provider="browser",
            description="Fill a selector in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.fill",
            invoker=browser_fill,
        ),
        RuntimeCapabilitySpec(
            name="browser.press",
            provider="browser",
            description="Press a key in the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.press",
            invoker=browser_press,
        ),
        RuntimeCapabilitySpec(
            name="browser.extract_text",
            provider="browser",
            description="Extract text from the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.extract_text",
            invoker=browser_extract_text,
        ),
        RuntimeCapabilitySpec(
            name="browser.screenshot",
            provider="browser",
            description="Capture a browser screenshot through the configured browser runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.screenshot",
            invoker=browser_screenshot,
        ),
        RuntimeCapabilitySpec(
            name="control_ui_chat.send_message",
            provider="browser",
            description="Send a message through the boiled-claw Control UI chat runtime.",
            risk="medium",
            requires_approval=True,
            transport="host_bridge_or_local",
            bridge_capability="host.control_ui_chat.send_message",
            invoker=control_ui_chat_send_message,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.info",
            provider="current_tab",
            description="Inspect the active Chrome tab through the current-tab relay.",
            risk="low",
            requires_approval=False,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.info",
            invoker=current_tab_info,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.navigate",
            provider="current_tab",
            description="Navigate the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.navigate",
            invoker=current_tab_navigate,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.click",
            provider="current_tab",
            description="Click a selector inside the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.click",
            invoker=current_tab_click,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.fill",
            provider="current_tab",
            description="Fill a selector inside the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.fill",
            invoker=current_tab_fill,
        ),
        RuntimeCapabilitySpec(
            name="current_tab.extract_text",
            provider="current_tab",
            description="Extract text from the active Chrome tab through the current-tab relay.",
            risk="medium",
            requires_approval=True,
            transport="current_tab_relay",
            bridge_capability="host.current_tab.extract_text",
            invoker=current_tab_extract_text,
        ),
        RuntimeCapabilitySpec(
            name="desktop.view.windows",
            provider="desktop",
            description="List visible windows from the desktop runtime.",
            risk="low",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.windows",
            invoker=desktop_view_windows,
        ),
        RuntimeCapabilitySpec(
            name="desktop.wait.window",
            provider="desktop",
            description="Wait for a matching window in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.wait.window",
            invoker=desktop_wait_window,
        ),
        RuntimeCapabilitySpec(
            name="desktop.view.frontmost_app",
            provider="desktop",
            description="Inspect the frontmost app in the desktop runtime.",
            risk="low",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.frontmost_app",
            invoker=desktop_view_frontmost_app,
        ),
        RuntimeCapabilitySpec(
            name="desktop.view.screenshot",
            provider="desktop",
            description="Capture a screenshot from the desktop runtime.",
            risk="medium",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.view.screenshot",
            invoker=desktop_view_screenshot,
        ),
        RuntimeCapabilitySpec(
            name="desktop.ax.find",
            provider="desktop",
            description="Resolve a matching accessibility element in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.ax.find",
            invoker=desktop_ax_find,
        ),
        RuntimeCapabilitySpec(
            name="desktop.wait.element",
            provider="desktop",
            description="Wait for a matching accessibility element in the desktop runtime.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.wait.element",
            invoker=desktop_wait_element,
        ),
        RuntimeCapabilitySpec(
            name="desktop.ax.snapshot",
            provider="desktop",
            description="Capture an accessibility snapshot from the desktop runtime.",
            risk="medium",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.ax.snapshot",
            invoker=desktop_ax_snapshot,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.status",
            provider="desktop",
            description="Inspect desktop emergency-stop state.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.status",
            invoker=desktop_runtime_status,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.stop",
            provider="desktop",
            description="Trigger desktop emergency stop.",
            risk="low",
            requires_approval=False,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.stop",
            invoker=desktop_runtime_stop,
        ),
        RuntimeCapabilitySpec(
            name="desktop.runtime.clear_stop",
            provider="desktop",
            description="Clear desktop emergency stop and re-enable control.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.runtime.clear_stop",
            invoker=desktop_runtime_clear_stop,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.click",
            provider="desktop",
            description="Click on the desktop or a matched accessibility element.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.click",
            invoker=desktop_control_click,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.type",
            provider="desktop",
            description="Type into the desktop or a matched accessibility element.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.type",
            invoker=desktop_control_type,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.launch_app",
            provider="desktop",
            description="Launch an app through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.launch_app",
            invoker=desktop_control_launch_app,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.focus_window",
            provider="desktop",
            description="Focus a window through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.focus_window",
            invoker=desktop_control_focus_window,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.hotkey",
            provider="desktop",
            description="Send a hotkey through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.hotkey",
            invoker=desktop_control_hotkey,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.scroll",
            provider="desktop",
            description="Scroll through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.scroll",
            invoker=desktop_control_scroll,
        ),
        RuntimeCapabilitySpec(
            name="desktop.control.drag",
            provider="desktop",
            description="Drag the pointer through the desktop runtime.",
            risk="high",
            requires_approval=True,
            transport="desktop_runtime",
            bridge_capability="desktop.control.drag",
            invoker=desktop_control_drag,
        ),
    ]
}

_HOST_BRIDGE_CAPABILITY_MAP = {
    "host.shell.run": "shell.run",
    "host.file.read": "file.read",
    "host.file.write": "file.write",
    "host.file.list": "file.list",
    "host.browser.navigate": "browser.navigate",
    "host.browser.click": "browser.click",
    "host.browser.fill": "browser.fill",
    "host.browser.press": "browser.press",
    "host.browser.extract_text": "browser.extract_text",
    "host.browser.screenshot": "browser.screenshot",
    "host.control_ui_chat.send_message": "control_ui_chat.send_message",
    "host.current_tab.info": "current_tab.info",
    "host.current_tab.navigate": "current_tab.navigate",
    "host.current_tab.click": "current_tab.click",
    "host.current_tab.fill": "current_tab.fill",
    "host.current_tab.extract_text": "current_tab.extract_text",
}


def _skill_resource(skill: BaseSkill) -> dict[str, Any]:
    meta = skill.get_metadata()
    skill_file = getattr(skill, "skill_file", None)
    return {
        "id": f"skill:{meta.name}",
        "kind": "skill",
        "provider": "skills",
        "title": meta.name,
        "description": meta.description,
        "version": meta.version,
        "author": meta.author,
        "tags": meta.tags,
        **({"path": str(skill_file)} if skill_file else {}),
    }


def _bridge_resources() -> list[dict[str, Any]]:
    settings = get_settings()
    return [
        {
            "id": "bridge:host",
            "kind": "bridge",
            "provider": "host",
            "title": "Host runtime",
            "description": "Guarded shell, file, browser, and Control UI surfaces backed by Host Bridge or local fallbacks.",
            "enabled": bool(settings.host_bridge_enabled),
        },
        {
            "id": "bridge:current_tab",
            "kind": "bridge",
            "provider": "current_tab",
            "title": "Current Tab relay",
            "description": "Chrome extension relay for the currently visible tab.",
            "enabled": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        },
        {
            "id": "bridge:desktop",
            "kind": "bridge",
            "provider": "desktop",
            "title": "Desktop runtime",
            "description": "Desktop capability plane for view, accessibility, and control actions.",
            "enabled": True,
        },
    ]


async def _implemented_overrides(refresh: bool) -> dict[str, bool]:
    settings = get_settings()
    implemented: dict[str, bool] = {
        "skill.list": True,
        "skill.execute": True,
        "shell.run": True,
        "file.read": True,
        "file.write": True,
        "file.list": True,
        "browser.navigate": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.click": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.fill": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.press": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.extract_text": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "browser.screenshot": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "control_ui_chat.send_message": bool(settings.host_bridge_enabled or PLAYWRIGHT_AVAILABLE),
        "current_tab.info": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.navigate": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.click": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.fill": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
        "current_tab.extract_text": bool(settings.host_bridge_enabled and current_tab_bridge_enabled()),
    }
    for name in _CAPABILITY_SPECS:
        if name.startswith("desktop."):
            implemented[name] = True

    if settings.host_bridge_enabled and refresh:
        host_names = {
            name
            for name, spec in _CAPABILITY_SPECS.items()
            if spec.bridge_capability and spec.bridge_capability.startswith("host.")
        }
        try:
            client = get_host_bridge_client()
            if client is None:
                raise RuntimeError("Host Bridge is not enabled.")
            result = await client.list_capabilities()
            for descriptor in result.capabilities:
                canonical_name = _HOST_BRIDGE_CAPABILITY_MAP.get(descriptor.name)
                if canonical_name:
                    implemented[canonical_name] = descriptor.implemented
        except Exception:
            for name in host_names:
                implemented[name] = False

    if refresh or not settings.desktop_bridge_enabled:
        desktop_names = {name for name in _CAPABILITY_SPECS if name.startswith("desktop.")}
        try:
            result = await get_desktop_client().capabilities()
            for descriptor in result.capabilities:
                if descriptor.name in desktop_names:
                    implemented[descriptor.name] = descriptor.implemented
        except Exception:
            if refresh:
                for name in desktop_names:
                    implemented[name] = False

    return implemented


async def list_runtime_resources() -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()
    resources = _bridge_resources()
    for meta in sorted(registry.list_skills(), key=lambda item: item.name):
        skill = registry.get_skill(meta.name)
        if skill is not None:
            resources.append(_skill_resource(skill))
    return {"count": len(resources), "resources": resources}


async def read_runtime_resource(resource_id: str, refresh: bool = False) -> dict[str, Any]:
    await ensure_skills_loaded()
    registry = get_skill_registry()

    if resource_id.startswith("skill:"):
        name = resource_id.split(":", 1)[1]
        skill = registry.get_skill(name)
        if skill is None:
            return {"ok": False, "message": f"Resource not found: {resource_id}"}
        payload = _skill_resource(skill)
        payload["content"] = getattr(skill, "content", "")
        return {"ok": True, "resource": payload}

    capabilities = await list_runtime_capabilities(refresh=refresh)
    capability_items = capabilities["capabilities"]

    if resource_id == "bridge:host":
        settings = get_settings()
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "host",
                "title": "Host runtime",
                "description": "Guarded shell, file, browser, and Control UI surfaces backed by Host Bridge or local fallbacks.",
                "enabled": bool(settings.host_bridge_enabled),
                "transport": "host_bridge" if settings.host_bridge_enabled else "local_fallback",
                "capabilities": [
                    item
                    for item in capability_items
                    if item["provider"] in {"host", "browser"}
                ],
            },
        }

    if resource_id == "bridge:current_tab":
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "current_tab",
                "title": "Current Tab relay",
                "description": "Chrome extension relay for the currently visible tab.",
                "enabled": bool(get_settings().host_bridge_enabled and current_tab_bridge_enabled()),
                "transport": "current_tab_extension_relay",
                "capabilities": [
                    item for item in capability_items if item["provider"] == "current_tab"
                ],
            },
        }

    if resource_id == "bridge:desktop":
        return {
            "ok": True,
            "resource": {
                "id": resource_id,
                "kind": "bridge",
                "provider": "desktop",
                "title": "Desktop runtime",
                "description": "Desktop capability plane for view, accessibility, and control actions.",
                "enabled": True,
                "transport": "desktop_bridge" if get_settings().desktop_bridge_enabled else "local_runtime",
                "capabilities": [
                    item for item in capability_items if item["provider"] == "desktop"
                ],
            },
        }

    return {"ok": False, "message": f"Resource not found: {resource_id}"}


async def list_runtime_capabilities(refresh: bool = False) -> dict[str, Any]:
    await ensure_skills_loaded()
    implemented = await _implemented_overrides(refresh=refresh)
    capabilities = []
    for name in sorted(_CAPABILITY_SPECS):
        spec = _CAPABILITY_SPECS[name]
        capabilities.append(
            {
                "name": spec.name,
                "provider": spec.provider,
                "description": spec.description,
                "risk": spec.risk,
                "requires_approval": spec.requires_approval,
                "transport": spec.transport,
                "bridge_capability": spec.bridge_capability,
                "implemented": implemented.get(spec.name, True),
            }
        )
    return {"count": len(capabilities), "refresh": refresh, "capabilities": capabilities}


async def invoke_runtime_capability(
    name: str,
    params: Optional[dict[str, Any]] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    spec = _CAPABILITY_SPECS.get(name)
    if spec is None:
        return {"success": False, "capability": name, "error": f"Unknown capability: {name}"}

    if spec.requires_approval and tool_context is None:
        return {
            "success": False,
            "capability": name,
            "error": (
                f"Capability {name} requires tool_context-backed approval flow and "
                "cannot be invoked without an ADK tool context."
            ),
        }

    kwargs = dict(params or {})
    try:
        if "tool_context" in inspect.signature(spec.invoker).parameters:
            kwargs["tool_context"] = tool_context
        result = await spec.invoker(**kwargs)
    except TypeError as exc:
        return {
            "success": False,
            "capability": name,
            "error": f"Invalid parameters for capability {name}: {exc}",
        }
    except Exception as exc:  # pragma: no cover
        return {"success": False, "capability": name, "error": str(exc)}

    success = True
    if isinstance(result, dict):
        if "success" in result:
            success = bool(result["success"])
        elif "ok" in result:
            success = bool(result["ok"])
        elif "error" in result:
            success = False

    return {
        "success": success,
        "capability": name,
        "provider": spec.provider,
        "transport": spec.transport,
        "result": result if isinstance(result, dict) else {"value": result},
    }


__all__ = [
    "RuntimeCapabilitySpec",
    "invoke_runtime_capability",
    "list_runtime_capabilities",
    "list_runtime_resources",
    "read_runtime_resource",
]
