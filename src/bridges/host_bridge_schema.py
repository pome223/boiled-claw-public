"""Host Bridge v1 schema definitions."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field
from src.bridges.common_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
)


class HostShellRunRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cwd: Optional[str] = None


class HostShellRunResult(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False
    error: Optional[str] = None


class HostFileReadRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)


class HostFileReadResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    content: str = ""
    size: int = 0
    error: Optional[str] = None


class HostFileWriteRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)
    content: str = ""


class HostFileWriteResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    size: int = 0
    error: Optional[str] = None


class HostFileListRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)


class HostFileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int


class HostFileListResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    entries: list[HostFileEntry] = Field(default_factory=list)
    error: Optional[str] = None


class HostBrowserNavigateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    url: str = Field(min_length=1)
    wait_for: str = Field(default="load", min_length=1)
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserNavigateResult(BaseModel):
    ok: bool
    url: Optional[str] = None
    title: str = ""
    status: Optional[int] = None
    error: Optional[str] = None


class HostBrowserClickRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserClickResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    error: Optional[str] = None


class HostBrowserFillRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)
    text: str = ""
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserFillResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    text_length: int = 0
    error: Optional[str] = None


class HostBrowserPressRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    key: str = Field(min_length=1)
    selector: Optional[str] = None
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserPressResult(BaseModel):
    ok: bool
    key: str = ""
    selector: Optional[str] = None
    error: Optional[str] = None


class HostBrowserScreenshotRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: Optional[str] = None
    full_page: bool = False


class HostBrowserScreenshotResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    full_page: bool = False
    error: Optional[str] = None


class HostBrowserExtractTextRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: Optional[str] = None


class HostBrowserExtractTextResult(BaseModel):
    ok: bool
    text: str = ""
    selector: str = "body"
    length: int = 0
    error: Optional[str] = None
