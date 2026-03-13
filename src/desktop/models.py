"""Core desktop runtime models.

These models describe desktop capabilities independently from any transport.
Bridge adapters may re-export or serialize them, but the runtime should not
depend on MCP-specific modules.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.bridges.host_bridge_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
)


class DesktopRequestBase(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None


class DesktopControlResult(BaseModel):
    ok: bool
    error: Optional[str] = None


class DesktopScreenshotRequest(DesktopRequestBase):
    path: Optional[str] = None


class DesktopScreenshotResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    width: int = 0
    height: int = 0
    error: Optional[str] = None


class DesktopWindowBounds(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


class DesktopWindowDescriptor(BaseModel):
    window_id: str
    app_name: str
    title: str = ""
    bounds: DesktopWindowBounds = Field(default_factory=DesktopWindowBounds)


class DesktopWindowsRequest(DesktopRequestBase):
    include_minimized: bool = False


class DesktopWindowsResult(BaseModel):
    ok: bool
    windows: list[DesktopWindowDescriptor] = Field(default_factory=list)
    error: Optional[str] = None


class DesktopFrontmostAppRequest(DesktopRequestBase):
    pass


class DesktopFrontmostAppResult(BaseModel):
    ok: bool
    app_name: str = ""
    pid: Optional[int] = None
    error: Optional[str] = None


class DesktopAxSnapshotRequest(DesktopRequestBase):
    app_name: Optional[str] = None
    window_id: Optional[str] = None


class DesktopAxSnapshotResult(BaseModel):
    ok: bool
    tree: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class DesktopClickRequest(DesktopRequestBase):
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=4)


class DesktopTypeRequest(DesktopRequestBase):
    text: str = Field(min_length=1)


class DesktopHotkeyRequest(DesktopRequestBase):
    keys: list[str] = Field(min_length=1)


class DesktopDragRequest(DesktopRequestBase):
    start_x: int
    start_y: int
    end_x: int
    end_y: int


__all__ = [
    "BridgePingResult",
    "CapabilityDescriptor",
    "CapabilityListResult",
    "DesktopRequestBase",
    "DesktopControlResult",
    "DesktopScreenshotRequest",
    "DesktopScreenshotResult",
    "DesktopWindowBounds",
    "DesktopWindowDescriptor",
    "DesktopWindowsRequest",
    "DesktopWindowsResult",
    "DesktopFrontmostAppRequest",
    "DesktopFrontmostAppResult",
    "DesktopAxSnapshotRequest",
    "DesktopAxSnapshotResult",
    "DesktopClickRequest",
    "DesktopTypeRequest",
    "DesktopHotkeyRequest",
    "DesktopDragRequest",
]
