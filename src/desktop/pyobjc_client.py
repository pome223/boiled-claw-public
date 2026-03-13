"""macOS-oriented desktop client implementation.

This client keeps the public interface transport-agnostic while using
platform-native APIs where possible. View-oriented primitives land first, then
the minimum viable control primitives needed for desktop automation.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from src.desktop.client import (
    DESKTOP_NOT_IMPLEMENTED,
    DesktopClient,
    desktop_capabilities,
)
from src.desktop.models import (
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTypeRequest,
    DesktopWindowBounds,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)


class PyObjCDesktopClient(DesktopClient):
    """Desktop client backed by macOS APIs and local OS tooling."""

    def __init__(
        self,
        *,
        appkit_module: Any | None = None,
        quartz_module: Any | None = None,
        screenshot_runner: Any | None = None,
    ) -> None:
        self._appkit = appkit_module
        self._quartz = quartz_module
        self._screenshot_runner = screenshot_runner or _default_screenshot_runner

    async def capabilities(self) -> CapabilityListResult:
        implemented = set()
        if self._quartz is not None:
            implemented.add("desktop.view.windows")
        if _supports_ax_snapshot(self._quartz, self._appkit):
            implemented.add("desktop.ax.snapshot")
        if _supports_mouse_click(self._quartz):
            implemented.add("desktop.control.click")
            implemented.add("desktop.control.drag")
        if _supports_text_input(self._quartz):
            implemented.add("desktop.control.type")
        if _supports_hotkey(self._quartz):
            implemented.add("desktop.control.hotkey")
        if self._appkit is not None:
            implemented.add("desktop.view.frontmost_app")
        if shutil.which("screencapture"):
            implemented.add("desktop.view.screenshot")
        return desktop_capabilities(implemented)

    async def screenshot(
        self, request: DesktopScreenshotRequest
    ) -> DesktopScreenshotResult:
        if not shutil.which("screencapture"):
            return DesktopScreenshotResult(
                ok=False,
                path=request.path,
                error=DESKTOP_NOT_IMPLEMENTED,
            )

        output_path = request.path or _make_temp_screenshot_path()
        try:
            self._screenshot_runner(output_path)
        except Exception as exc:
            return DesktopScreenshotResult(
                ok=False,
                path=output_path,
                error=f"screenshot failed: {exc}",
            )

        width, height = _display_size(self._quartz)
        return DesktopScreenshotResult(
            ok=True,
            path=output_path,
            width=width,
            height=height,
        )

    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        if self._quartz is None:
            return DesktopWindowsResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        option = getattr(self._quartz, "kCGWindowListOptionAll", 0)
        if not request.include_minimized:
            option = getattr(self._quartz, "kCGWindowListOptionOnScreenOnly", option)

        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            option,
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        descriptors: list[DesktopWindowDescriptor] = []
        for item in raw_windows or []:
            layer = int(item.get("kCGWindowLayer", item.get("CGWindowLayer", 0)) or 0)
            if layer != 0:
                continue

            app_name = str(
                item.get("kCGWindowOwnerName", item.get("CGWindowOwnerName", "")) or ""
            )
            if not app_name:
                continue

            window_id = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            bounds = item.get("kCGWindowBounds", item.get("CGWindowBounds", {})) or {}
            title = str(item.get("kCGWindowName", item.get("CGWindowName", "")) or "")
            descriptors.append(
                DesktopWindowDescriptor(
                    window_id=str(window_id),
                    app_name=app_name,
                    title=title,
                    bounds=DesktopWindowBounds(
                        x=int(bounds.get("X", 0) or 0),
                        y=int(bounds.get("Y", 0) or 0),
                        width=int(bounds.get("Width", 0) or 0),
                        height=int(bounds.get("Height", 0) or 0),
                    ),
                )
            )

        return DesktopWindowsResult(ok=True, windows=descriptors)

    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        del request
        if self._appkit is None:
            return DesktopFrontmostAppResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        workspace = self._appkit.NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app is None:
            return DesktopFrontmostAppResult(
                ok=False,
                error="frontmost application not available",
            )

        name_getter = getattr(app, "localizedName", None)
        pid_getter = getattr(app, "processIdentifier", None)
        app_name = name_getter() if callable(name_getter) else ""
        pid = pid_getter() if callable(pid_getter) else None
        return DesktopFrontmostAppResult(
            ok=True,
            app_name=str(app_name or ""),
            pid=int(pid) if pid is not None else None,
        )

    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        if not _supports_ax_snapshot(self._quartz, self._appkit):
            return DesktopAxSnapshotResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        resolved = self._resolve_target_application(request.app_name)
        if resolved is None:
            return DesktopAxSnapshotResult(
                ok=False,
                error=f"desktop app not found: {request.app_name or 'frontmost'}",
            )
        app_name, pid = resolved
        ax_root = self._quartz.AXUIElementCreateApplication(pid)
        if request.window_id:
            window = self._resolve_window_element(ax_root, request.window_id)
            if window is not None:
                ax_root = window

        tree = {
            "app_name": app_name,
            "pid": pid,
            "root": self._serialize_ax_element(ax_root, depth=0, max_depth=3),
        }
        return DesktopAxSnapshotResult(ok=True, tree=tree)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        if not _supports_mouse_click(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        event_down_type, event_up_type, mouse_button = _mouse_event_spec(
            self._quartz,
            request.button,
        )
        point = (request.x, request.y)
        for _ in range(request.click_count):
            down = self._quartz.CGEventCreateMouseEvent(
                None,
                event_down_type,
                point,
                mouse_button,
            )
            up = self._quartz.CGEventCreateMouseEvent(
                None,
                event_up_type,
                point,
                mouse_button,
            )
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        if not _supports_text_input(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        for character in request.text:
            down = self._quartz.CGEventCreateKeyboardEvent(None, 0, True)
            up = self._quartz.CGEventCreateKeyboardEvent(None, 0, False)
            self._quartz.CGEventKeyboardSetUnicodeString(down, len(character), character)
            self._quartz.CGEventKeyboardSetUnicodeString(up, len(character), character)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        if not _supports_hotkey(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        keycode, flags = _hotkey_spec(self._quartz, request.keys)
        if keycode is None:
            return DesktopControlResult(ok=False, error="unsupported hotkey")

        down = self._quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        up = self._quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        _set_event_flags(self._quartz, down, flags)
        _set_event_flags(self._quartz, up, flags)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        if not _supports_mouse_click(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        start = (request.start_x, request.start_y)
        end = (request.end_x, request.end_y)
        down = self._quartz.CGEventCreateMouseEvent(
            None,
            self._quartz.kCGEventLeftMouseDown,
            start,
            self._quartz.kCGMouseButtonLeft,
        )
        dragged = self._quartz.CGEventCreateMouseEvent(
            None,
            getattr(self._quartz, "kCGEventLeftMouseDragged", self._quartz.kCGEventLeftMouseDown),
            end,
            self._quartz.kCGMouseButtonLeft,
        )
        up = self._quartz.CGEventCreateMouseEvent(
            None,
            self._quartz.kCGEventLeftMouseUp,
            end,
            self._quartz.kCGMouseButtonLeft,
        )
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, dragged)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    def _resolve_target_application(
        self,
        requested_app_name: str | None,
    ) -> tuple[str, int] | None:
        if self._appkit is None:
            return None

        workspace = self._appkit.NSWorkspace.sharedWorkspace()
        if requested_app_name:
            matcher = requested_app_name.strip().lower()
            for app in workspace.runningApplications() or []:
                name_getter = getattr(app, "localizedName", None)
                pid_getter = getattr(app, "processIdentifier", None)
                app_name = name_getter() if callable(name_getter) else ""
                pid = pid_getter() if callable(pid_getter) else None
                if str(app_name or "").strip().lower() == matcher and pid is not None:
                    return str(app_name), int(pid)

        frontmost = workspace.frontmostApplication()
        if frontmost is None:
            return None
        name_getter = getattr(frontmost, "localizedName", None)
        pid_getter = getattr(frontmost, "processIdentifier", None)
        app_name = name_getter() if callable(name_getter) else ""
        pid = pid_getter() if callable(pid_getter) else None
        if pid is None:
            return None
        return (str(app_name or ""), int(pid))

    def _resolve_window_element(self, ax_root: Any, window_id: str) -> Any | None:
        if self._quartz is None:
            return None
        target_title = self._window_title_for_id(window_id)
        if not target_title:
            return None
        windows = _ax_value(self._quartz, ax_root, self._quartz.kAXWindowsAttribute) or []
        for window in windows:
            title = _ax_value(self._quartz, window, self._quartz.kAXTitleAttribute)
            if str(title or "") == target_title:
                return window
        return None

    def _window_title_for_id(self, window_id: str) -> str | None:
        if self._quartz is None:
            return None
        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            getattr(self._quartz, "kCGWindowListOptionAll", 0),
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        for item in raw_windows or []:
            candidate = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            if str(candidate) == str(window_id):
                title = item.get("kCGWindowName", item.get("CGWindowName", ""))
                return str(title or "")
        return None

    def _serialize_ax_element(self, element: Any, *, depth: int, max_depth: int) -> dict[str, Any]:
        if self._quartz is None:
            return {}

        node = {
            "role": _ax_value(self._quartz, element, self._quartz.kAXRoleAttribute) or "",
            "title": _ax_value(self._quartz, element, self._quartz.kAXTitleAttribute) or "",
            "description": _ax_value(
                self._quartz,
                element,
                self._quartz.kAXDescriptionAttribute,
            ) or "",
            "identifier": _ax_value(
                self._quartz,
                element,
                getattr(self._quartz, "kAXIdentifierAttribute", "AXIdentifier"),
            ) or "",
            "value": _stringify_ax_value(
                _ax_value(self._quartz, element, self._quartz.kAXValueAttribute)
            ),
            "children": [],
        }
        if depth >= max_depth:
            return node

        children = _ax_value(self._quartz, element, self._quartz.kAXChildrenAttribute) or []
        if not isinstance(children, (list, tuple)):
            children = [children]
        node["children"] = [
            self._serialize_ax_element(child, depth=depth + 1, max_depth=max_depth)
            for child in children[:25]
        ]
        return node


def _default_screenshot_runner(path: str) -> None:
    subprocess.run(
        ["screencapture", "-x", path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_temp_screenshot_path() -> str:
    fd, path = tempfile.mkstemp(prefix="boiled-claw-desktop-", suffix=".png")
    Path(path).unlink(missing_ok=True)
    return path


def _display_size(quartz_module: Any | None) -> tuple[int, int]:
    if quartz_module is None:
        return (0, 0)
    try:
        display_id = quartz_module.CGMainDisplayID()
        width = int(quartz_module.CGDisplayPixelsWide(display_id))
        height = int(quartz_module.CGDisplayPixelsHigh(display_id))
        return (width, height)
    except Exception:
        return (0, 0)


def _supports_ax_snapshot(quartz_module: Any | None, appkit_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and appkit_module is not None
        and hasattr(quartz_module, "AXUIElementCreateApplication")
        and hasattr(quartz_module, "AXUIElementCopyAttributeValue")
    )


def _supports_mouse_click(quartz_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and hasattr(quartz_module, "CGEventCreateMouseEvent")
        and hasattr(quartz_module, "CGEventPost")
        and hasattr(quartz_module, "kCGHIDEventTap")
    )


def _supports_text_input(quartz_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and hasattr(quartz_module, "CGEventCreateKeyboardEvent")
        and hasattr(quartz_module, "CGEventKeyboardSetUnicodeString")
        and hasattr(quartz_module, "CGEventPost")
        and hasattr(quartz_module, "kCGHIDEventTap")
    )


def _supports_hotkey(quartz_module: Any | None) -> bool:
    return _supports_text_input(quartz_module) and hasattr(quartz_module, "CGEventSetFlags")


def _mouse_event_spec(quartz_module: Any, button: str) -> tuple[int, int, int]:
    button_name = button.lower()
    if button_name == "right":
        return (
            quartz_module.kCGEventRightMouseDown,
            quartz_module.kCGEventRightMouseUp,
            quartz_module.kCGMouseButtonRight,
        )
    if button_name == "middle":
        return (
            quartz_module.kCGEventOtherMouseDown,
            quartz_module.kCGEventOtherMouseUp,
            quartz_module.kCGMouseButtonCenter,
        )
    return (
        quartz_module.kCGEventLeftMouseDown,
        quartz_module.kCGEventLeftMouseUp,
        quartz_module.kCGMouseButtonLeft,
    )


def _ax_value(quartz_module: Any, element: Any, attribute: str) -> Any:
    fn = quartz_module.AXUIElementCopyAttributeValue
    try:
        raw = fn(element, attribute, None)
    except TypeError:
        raw = fn(element, attribute)

    if isinstance(raw, tuple):
        if len(raw) == 2:
            error_code, value = raw
            if error_code not in (0, None):
                return None
            return value
        if len(raw) == 1:
            return raw[0]
    return raw


def _stringify_ax_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_stringify_ax_value(item) for item in value[:10]]
    if isinstance(value, dict):
        return {str(key): _stringify_ax_value(val) for key, val in value.items()}
    return str(value)


def _set_event_flags(quartz_module: Any, event: Any, flags: int) -> None:
    setter = getattr(quartz_module, "CGEventSetFlags", None)
    if callable(setter):
        setter(event, flags)
    elif isinstance(event, dict):
        event["flags"] = flags


def _hotkey_spec(quartz_module: Any, keys: list[str]) -> tuple[int | None, int]:
    modifiers = {
        "command": getattr(quartz_module, "kCGEventFlagMaskCommand", 0),
        "cmd": getattr(quartz_module, "kCGEventFlagMaskCommand", 0),
        "shift": getattr(quartz_module, "kCGEventFlagMaskShift", 0),
        "option": getattr(quartz_module, "kCGEventFlagMaskAlternate", 0),
        "alt": getattr(quartz_module, "kCGEventFlagMaskAlternate", 0),
        "control": getattr(quartz_module, "kCGEventFlagMaskControl", 0),
        "ctrl": getattr(quartz_module, "kCGEventFlagMaskControl", 0),
        "fn": getattr(quartz_module, "kCGEventFlagMaskSecondaryFn", 0),
    }
    keycodes = {
        "a": 0,
        "b": 11,
        "c": 8,
        "d": 2,
        "e": 14,
        "f": 3,
        "g": 5,
        "h": 4,
        "i": 34,
        "j": 38,
        "k": 40,
        "l": 37,
        "m": 46,
        "n": 45,
        "o": 31,
        "p": 35,
        "q": 12,
        "r": 15,
        "s": 1,
        "t": 17,
        "u": 32,
        "v": 9,
        "w": 13,
        "x": 7,
        "y": 16,
        "z": 6,
        "0": 29,
        "1": 18,
        "2": 19,
        "3": 20,
        "4": 21,
        "5": 23,
        "6": 22,
        "7": 26,
        "8": 28,
        "9": 25,
        "space": 49,
        "return": 36,
        "enter": 36,
        "tab": 48,
        "escape": 53,
        "esc": 53,
        "left": 123,
        "right": 124,
        "down": 125,
        "up": 126,
    }

    flags = 0
    primary_key: str | None = None
    for key in keys:
        normalized = str(key).strip().lower()
        if normalized in modifiers:
            flags |= modifiers[normalized]
        else:
            primary_key = normalized
    if primary_key is None:
        return None, flags
    return keycodes.get(primary_key), flags


__all__ = ["PyObjCDesktopClient"]
