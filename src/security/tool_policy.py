"""
Tool-level security with per-agent policies and approval forwarding.

Policy evaluation order:
  1. Check agent-specific rules (if any)
  2. Check default rules
  3. Apply fallback action (deny by default)

Actions:
  allow   - tool execution permitted
  deny    - tool execution blocked
  approve - requires user approval before execution
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

Action = Literal["allow", "deny", "approve"]


@dataclass
class ToolRule:
    """A single tool policy rule.

    tool_pattern: glob pattern matching tool names (e.g. "shell.*", "browser_*", "*")
    action: what to do when matched
    reason: human-readable explanation
    """
    tool_pattern: str
    action: Action
    reason: str = ""

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name, self.tool_pattern)


@dataclass
class AgentPolicy:
    """Policy for a specific agent."""
    agent_name: str
    rules: List[ToolRule] = field(default_factory=list)
    fallback: Action = "deny"

    def evaluate(self, tool_name: str) -> Tuple[Action, str]:
        for rule in self.rules:
            if rule.matches(tool_name):
                return rule.action, rule.reason or f"matched rule: {rule.tool_pattern}"
        return self.fallback, f"fallback policy for agent '{self.agent_name}'"


# Default rules: broad allow for safe tools, approve for dangerous ones
_DEFAULT_RULES: List[ToolRule] = [
    ToolRule("memory_*", "allow", "memory operations are safe"),
    ToolRule("web_search", "allow", "web search is safe"),
    ToolRule("skill_list", "allow", "listing skills is safe"),
    ToolRule("skill_execute", "approve", "skill execution needs approval"),
    ToolRule("run_shell", "approve", "shell commands need approval"),
    ToolRule("shell_*", "approve", "shell commands need approval"),
    ToolRule("write_file", "approve", "file writes need approval"),
    ToolRule("read_file", "allow", "file reads are safe"),
    ToolRule("browser_*", "approve", "browser automation needs approval"),
    ToolRule("stock_price", "allow", "stock price lookup is safe"),
    ToolRule("subagents_*", "approve", "subagent operations need approval"),
    ToolRule("sessions_*", "approve", "session operations need approval"),
]


class ToolPolicyEngine:
    """Evaluate tool execution permissions per agent."""

    def __init__(self) -> None:
        self._default_policy = AgentPolicy(
            agent_name="__default__",
            rules=list(_DEFAULT_RULES),
            fallback="deny",
        )
        self._agent_policies: Dict[str, AgentPolicy] = {}
        self._pending_approvals: Dict[str, _PendingApproval] = {}
        self._approval_waiters: Dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    @property
    def default_policy(self) -> AgentPolicy:
        return self._default_policy

    def register_agent_policy(self, policy: AgentPolicy) -> None:
        self._agent_policies[policy.agent_name] = policy

    def remove_agent_policy(self, agent_name: str) -> bool:
        return self._agent_policies.pop(agent_name, None) is not None

    def get_agent_policy(self, agent_name: str) -> Optional[AgentPolicy]:
        return self._agent_policies.get(agent_name)

    def set_notifier(
        self,
        notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> None:
        self._notifier = notifier

    def list_policies(self) -> Dict[str, Any]:
        return {
            "default": {
                "rules": [
                    {"pattern": r.tool_pattern, "action": r.action, "reason": r.reason}
                    for r in self._default_policy.rules
                ],
                "fallback": self._default_policy.fallback,
            },
            "agents": {
                name: {
                    "rules": [
                        {"pattern": r.tool_pattern, "action": r.action, "reason": r.reason}
                        for r in p.rules
                    ],
                    "fallback": p.fallback,
                }
                for name, p in self._agent_policies.items()
            },
        }

    def evaluate(self, agent_name: str, tool_name: str) -> Tuple[Action, str]:
        """Evaluate whether a tool call is allowed.

        Returns (action, reason) tuple.
        """
        # Check agent-specific policy first
        agent_policy = self._agent_policies.get(agent_name)
        if agent_policy:
            action, reason = agent_policy.evaluate(tool_name)
            if action != "deny" or agent_policy.fallback != "deny":
                return action, reason
            # If agent policy has deny fallback and no match, also check default
            for rule in agent_policy.rules:
                if rule.matches(tool_name):
                    return action, reason

        # Fall back to default policy
        return self._default_policy.evaluate(tool_name)

    # ------------------------------------------------------------------
    # Approval request tracking
    # ------------------------------------------------------------------

    def create_approval_request(
        self,
        request_id: str,
        tool_name: str,
        agent_name: str,
        args: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        reason: str = "",
    ) -> _PendingApproval:
        approval = _PendingApproval(
            request_id=request_id,
            tool_name=tool_name,
            agent_name=agent_name,
            args=args or {},
            session_id=session_id or "",
            reason=reason,
            created_at=time.time(),
        )
        self._pending_approvals[request_id] = approval
        return approval

    def resolve_approval(
        self,
        request_id: str,
        approved: bool,
        reason: str = "",
    ) -> Optional[_PendingApproval]:
        approval = self._pending_approvals.pop(request_id, None)
        if approval:
            approval.resolved = True
            approval.approved = approved
            approval.resolve_reason = reason
            approval.resolved_at = time.time()
            waiter = self._approval_waiters.pop(request_id, None)
            if waiter and not waiter.done():
                waiter.set_result((approved, reason))
        return approval

    def get_pending_approval(self, request_id: str) -> Optional[_PendingApproval]:
        return self._pending_approvals.get(request_id)

    def list_pending_approvals(
        self, session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        approvals = list(self._pending_approvals.values())
        if session_id:
            approvals = [a for a in approvals if a.session_id == session_id]
        return [a.to_dict() for a in approvals]

    def cleanup_expired(self, max_age: float = 300.0) -> int:
        """Remove approval requests older than max_age seconds."""
        now = time.time()
        expired = [
            rid for rid, a in self._pending_approvals.items()
            if now - a.created_at > max_age
        ]
        for rid in expired:
            self._pending_approvals.pop(rid, None)
            waiter = self._approval_waiters.pop(rid, None)
            if waiter and not waiter.done():
                waiter.cancel()
        return len(expired)

    async def request_approval(
        self,
        *,
        tool_name: str,
        agent_name: str,
        args: Optional[Dict[str, Any]] = None,
        session_id: str,
        reason: str = "",
        timeout: float = 300.0,
    ) -> Tuple[bool, str]:
        """Request approval and wait for a user response."""
        if not session_id:
            return False, "approval requires a valid session_id"
        if self._notifier is None:
            return False, "approval channel is unavailable"

        request_id = uuid.uuid4().hex[:12]
        approval = self.create_approval_request(
            request_id=request_id,
            tool_name=tool_name,
            agent_name=agent_name,
            args=args,
            session_id=session_id,
            reason=reason,
        )

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._approval_waiters[request_id] = waiter

        try:
            await self._notifier(approval.to_dict())
        except Exception as exc:
            self._pending_approvals.pop(request_id, None)
            self._approval_waiters.pop(request_id, None)
            return False, f"failed to deliver approval request: {exc}"

        try:
            approved, resolve_reason = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_approvals.pop(request_id, None)
            self._approval_waiters.pop(request_id, None)
            return False, "approval timed out"
        except asyncio.CancelledError:
            self._pending_approvals.pop(request_id, None)
            self._approval_waiters.pop(request_id, None)
            raise

        return approved, resolve_reason


@dataclass
class _PendingApproval:
    request_id: str
    tool_name: str
    agent_name: str
    args: Dict[str, Any]
    session_id: str
    reason: str
    created_at: float
    resolved: bool = False
    approved: bool = False
    resolve_reason: str = ""
    resolved_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "agent_name": self.agent_name,
            "args": self.args,
            "session_id": self.session_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "resolved": self.resolved,
            "approved": self.approved,
            "resolve_reason": self.resolve_reason,
        }


# Global singleton
_engine: Optional[ToolPolicyEngine] = None


def get_tool_policy_engine() -> ToolPolicyEngine:
    global _engine
    if _engine is None:
        _engine = ToolPolicyEngine()
    return _engine
