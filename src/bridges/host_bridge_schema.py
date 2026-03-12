"""Host Bridge v1 schema definitions."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BridgePingResult(BaseModel):
    ok: bool = True
    service: str = "host-bridge"
    version: str = "v1"
    transport: str


class CapabilityDescriptor(BaseModel):
    name: str
    risk: Literal["low", "medium", "high"]
    requires_approval: bool
    description: str
    implemented: bool = True


class CapabilityListResult(BaseModel):
    capabilities: list[CapabilityDescriptor]


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
    success: bool = False
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
