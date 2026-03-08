"""Shared tool context helpers."""

from typing import Optional

from google.adk.agents.context import Context as ToolContext


def resolve_tool_context(tool_context: Optional[ToolContext]) -> dict[str, str]:
    """Extract agent_name and session_id from ADK ToolContext."""
    session = getattr(tool_context, "session", None)
    return {
        "agent_name": getattr(tool_context, "agent_name", None) or "unknown_agent",
        "session_id": getattr(session, "id", None) or "",
    }
