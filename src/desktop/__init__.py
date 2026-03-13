"""Desktop runtime interfaces and fake implementations."""

from src.desktop.client import DESKTOP_NOT_IMPLEMENTED, DesktopClient, desktop_capabilities
from src.desktop.factory import build_default_desktop_client
from src.desktop.fake_client import FakeDesktopClient
from src.desktop.models import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopRequestBase,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTypeRequest,
    DesktopWindowBounds,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)
from src.desktop.pyobjc_client import PyObjCDesktopClient

__all__ = [
    "DESKTOP_NOT_IMPLEMENTED",
    "DesktopClient",
    "FakeDesktopClient",
    "PyObjCDesktopClient",
    "build_default_desktop_client",
    "desktop_capabilities",
    "BridgePingResult",
    "CapabilityDescriptor",
    "CapabilityListResult",
    "DesktopAxSnapshotRequest",
    "DesktopAxSnapshotResult",
    "DesktopClickRequest",
    "DesktopControlResult",
    "DesktopDragRequest",
    "DesktopFrontmostAppRequest",
    "DesktopFrontmostAppResult",
    "DesktopHotkeyRequest",
    "DesktopRequestBase",
    "DesktopScreenshotRequest",
    "DesktopScreenshotResult",
    "DesktopTypeRequest",
    "DesktopWindowBounds",
    "DesktopWindowDescriptor",
    "DesktopWindowsRequest",
    "DesktopWindowsResult",
]
