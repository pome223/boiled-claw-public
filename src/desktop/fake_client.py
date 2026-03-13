"""Fake desktop client for contract tests and early integration work."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

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
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)


class FakeDesktopClient(DesktopClient):
    """In-memory desktop runtime used before a real companion exists."""

    def __init__(
        self,
        *,
        implemented: Collection[str] | None = None,
        windows: list[DesktopWindowDescriptor] | None = None,
        frontmost_app_name: str = "",
        frontmost_pid: int | None = None,
        screenshot_path: str | None = None,
        screenshot_width: int = 0,
        screenshot_height: int = 0,
        ax_tree: dict[str, Any] | None = None,
    ) -> None:
        self._implemented = set(implemented or ())
        self._windows = list(windows or [])
        self._frontmost_app_name = frontmost_app_name
        self._frontmost_pid = frontmost_pid
        self._screenshot_path = screenshot_path
        self._screenshot_width = screenshot_width
        self._screenshot_height = screenshot_height
        self._ax_tree = dict(ax_tree or {})

    async def capabilities(self) -> CapabilityListResult:
        return desktop_capabilities(self._implemented)

    async def screenshot(
        self, request: DesktopScreenshotRequest
    ) -> DesktopScreenshotResult:
        if "desktop.view.screenshot" not in self._implemented:
            return DesktopScreenshotResult(
                ok=False,
                path=request.path,
                error=DESKTOP_NOT_IMPLEMENTED,
            )
        return DesktopScreenshotResult(
            ok=True,
            path=request.path or self._screenshot_path,
            width=self._screenshot_width,
            height=self._screenshot_height,
        )

    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        del request
        if "desktop.view.windows" not in self._implemented:
            return DesktopWindowsResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopWindowsResult(ok=True, windows=self._windows)

    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        del request
        if "desktop.view.frontmost_app" not in self._implemented:
            return DesktopFrontmostAppResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopFrontmostAppResult(
            ok=True,
            app_name=self._frontmost_app_name,
            pid=self._frontmost_pid,
        )

    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        del request
        if "desktop.ax.snapshot" not in self._implemented:
            return DesktopAxSnapshotResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopAxSnapshotResult(ok=True, tree=self._ax_tree)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        del request
        return self._control_result("desktop.control.click")

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        del request
        return self._control_result("desktop.control.type")

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        del request
        return self._control_result("desktop.control.hotkey")

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        del request
        return self._control_result("desktop.control.drag")

    def _control_result(self, capability: str) -> DesktopControlResult:
        if capability not in self._implemented:
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopControlResult(ok=True)


__all__ = ["FakeDesktopClient"]
