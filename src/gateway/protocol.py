"""
Typed Gateway Protocol v1

Envelope format (all messages):
  {
    "v": 1,
    "event": "<event_name>",
    "request_id": "<optional correlation id>",
    "ts": <unix timestamp>,
    ...event-specific fields
  }

Client -> Server:
  chat.send           text, request_id?
  chat.inject         text, role?, request_id?
  chat.abort          request_id?
  chat.history        session_id?, limit?, before?
  presence.ping       (no data)
  tools.approval      request_id, approved (bool), reason?

Server -> Client:
  connected           session_id, user_id, protocol_version
  chat.done           text, request_id?, aborted
  chat.token          text, request_id?
  chat.history        entries[], session_id
  system.event        source, status, message, run_id?, agent_name?
  health.tick         active_sessions, ts
  cron.update         job_id, status, message
  tools.approval_request  request_id, tool_name, agent_name, args, reason
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

PROTOCOL_VERSION = 1

# --------------------------------------------------------------------------
# JSON Schema definitions for protocol validation
# --------------------------------------------------------------------------

EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    # --- Client -> Server ---
    "chat.send": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.send"},
            "v": {"type": "integer"},
            "text": {"type": "string", "minLength": 1},
            "request_id": {"type": "string"},
        },
    },
    "chat.inject": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.inject"},
            "v": {"type": "integer"},
            "text": {"type": "string", "minLength": 1},
            "role": {"type": "string", "enum": ["system", "context", "user"]},
            "request_id": {"type": "string"},
        },
    },
    "chat.abort": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "chat.abort"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
        },
    },
    "chat.history": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "chat.history"},
            "v": {"type": "integer"},
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "before": {"type": "number"},
            "request_id": {"type": "string"},
        },
    },
    "presence.ping": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "presence.ping"},
            "v": {"type": "integer"},
        },
    },
    "tools.approval": {
        "type": "object",
        "required": ["event", "request_id", "approved"],
        "properties": {
            "event": {"const": "tools.approval"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "approved": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
    # --- Server -> Client ---
    "connected": {
        "type": "object",
        "required": ["event", "session_id", "user_id"],
        "properties": {
            "event": {"const": "connected"},
            "v": {"type": "integer"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "protocol_version": {"type": "integer"},
            "ts": {"type": "number"},
        },
    },
    "chat.done": {
        "type": "object",
        "required": ["event", "text", "aborted"],
        "properties": {
            "event": {"const": "chat.done"},
            "v": {"type": "integer"},
            "text": {"type": "string"},
            "request_id": {"type": "string"},
            "aborted": {"type": "boolean"},
            "ts": {"type": "number"},
        },
    },
    "chat.token": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.token"},
            "v": {"type": "integer"},
            "text": {"type": "string"},
            "request_id": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "health.tick": {
        "type": "object",
        "required": ["event", "active_sessions", "ts"],
        "properties": {
            "event": {"const": "health.tick"},
            "v": {"type": "integer"},
            "active_sessions": {"type": "integer"},
            "ts": {"type": "number"},
        },
    },
    "system.event": {
        "type": "object",
        "required": ["event", "source", "status", "message"],
        "properties": {
            "event": {"const": "system.event"},
            "v": {"type": "integer"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "message": {"type": "string"},
            "run_id": {"type": "string"},
            "agent_name": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "cron.update": {
        "type": "object",
        "required": ["event", "job_id", "status"],
        "properties": {
            "event": {"const": "cron.update"},
            "v": {"type": "integer"},
            "job_id": {"type": "string"},
            "status": {"type": "string"},
            "message": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "tools.approval_request": {
        "type": "object",
        "required": ["event", "request_id", "tool_name", "agent_name"],
        "properties": {
            "event": {"const": "tools.approval_request"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "agent_name": {"type": "string"},
            "args": {"type": "object"},
            "reason": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
}


def make_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _base(event: str, request_id: Optional[str] = None) -> dict[str, Any]:
    d: dict[str, Any] = {"v": PROTOCOL_VERSION, "event": event, "ts": time.time()}
    if request_id:
        d["request_id"] = request_id
    return d


# --------------------------------------------------------------------------
# Server -> Client event builders
# --------------------------------------------------------------------------

def ev_connected(session_id: str, user_id: str) -> dict[str, Any]:
    d = _base("connected")
    d["session_id"] = session_id
    d["user_id"] = user_id
    d["protocol_version"] = PROTOCOL_VERSION
    return d


def ev_chat_done(
    text: str,
    request_id: Optional[str] = None,
    aborted: bool = False,
) -> dict[str, Any]:
    d = _base("chat.done", request_id)
    d["text"] = text
    d["aborted"] = aborted
    return d


def ev_chat_token(text: str, request_id: Optional[str] = None) -> dict[str, Any]:
    d = _base("chat.token", request_id)
    d["text"] = text
    return d


def ev_chat_history(
    entries: list[dict[str, Any]],
    session_id: str,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("chat.history", request_id)
    d["session_id"] = session_id
    d["entries"] = entries
    return d


def ev_system_event(
    source: str,
    status: str,
    message: str,
    run_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("system.event")
    d["source"] = source
    d["status"] = status
    d["message"] = message
    if run_id:
        d["run_id"] = run_id
    if agent_name:
        d["agent_name"] = agent_name
    return d


def ev_health_tick(active_sessions: int) -> dict[str, Any]:
    d = _base("health.tick")
    d["active_sessions"] = active_sessions
    return d


def ev_cron_update(job_id: str, status: str, message: str = "") -> dict[str, Any]:
    d = _base("cron.update")
    d["job_id"] = job_id
    d["status"] = status
    d["message"] = message
    return d


def ev_tools_approval_request(
    request_id: str,
    tool_name: str,
    agent_name: str,
    args: Optional[dict[str, Any]] = None,
    reason: str = "",
) -> dict[str, Any]:
    d = _base("tools.approval_request", request_id)
    d["tool_name"] = tool_name
    d["agent_name"] = agent_name
    d["args"] = args or {}
    d["reason"] = reason
    return d


# --------------------------------------------------------------------------
# Parsing / normalization
# --------------------------------------------------------------------------

def normalize_client_event(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize incoming client message to v1 envelope.

    Supports legacy format (type=message, message=...) as well as v1 format.
    """
    event = data.get("event") or data.get("type", "")

    # Legacy: type=message -> chat.send
    if event == "message":
        event = "chat.send"

    # Legacy: "message" field -> "text" field
    if "text" not in data and "message" in data:
        data["text"] = data["message"]

    # Legacy: type=ping -> presence.ping
    if event == "ping":
        event = "presence.ping"

    data["event"] = event
    if "v" not in data:
        data["v"] = PROTOCOL_VERSION

    return data


def get_schema(event_name: str) -> Optional[dict[str, Any]]:
    """Return JSON Schema for a given event name, or None."""
    return EVENT_SCHEMAS.get(event_name)
