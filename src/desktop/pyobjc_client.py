"""macOS-oriented desktop client implementation.

This client keeps the public interface transport-agnostic while using
platform-native APIs where possible. View-oriented primitives are implemented
first; control and AX capabilities remain intentionally unimplemented.
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
        del request
        return DesktopAxSnapshotResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        del request
        return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        del request
        return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        del request
        return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        del request
        return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)


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


__all__ = ["PyObjCDesktopClient"]
