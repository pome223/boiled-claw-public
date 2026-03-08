from types import SimpleNamespace

import pytest

from src.security.policy import SecurityPolicy
from src.security.tool_policy import ToolPolicyEngine
from src.tools import file_manager as file_module
from src.tools import shell as shell_module


@pytest.mark.asyncio
async def test_tool_policy_request_approval_round_trip():
    engine = ToolPolicyEngine()
    captured = {}

    async def notifier(payload):
        captured.update(payload)
        engine.resolve_approval(payload["request_id"], True, "approved in test")

    engine.set_notifier(notifier)

    approved, reason = await engine.request_approval(
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "echo hello"},
        session_id="session-1",
        reason="shell commands need approval",
    )

    assert approved is True
    assert reason == "approved in test"
    assert captured["tool_name"] == "run_shell"
    assert captured["agent_name"] == "boiled_claw"


@pytest.mark.asyncio
async def test_run_shell_waits_for_approval(monkeypatch):
    engine = ToolPolicyEngine()

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], True, "approved")

    engine.set_notifier(notifier)
    monkeypatch.setattr(shell_module, "get_tool_policy_engine", lambda: engine)

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )

    result = await shell_module.run_shell("echo approved", tool_context=tool_context)

    assert result["return_code"] == 0
    assert "approved" in result["stdout"]


@pytest.mark.asyncio
async def test_write_file_rejected_when_approval_denied(monkeypatch, tmp_path):
    engine = ToolPolicyEngine()

    async def notifier(payload):
        engine.resolve_approval(payload["request_id"], False, "rejected")

    engine.set_notifier(notifier)
    monkeypatch.setattr(file_module, "get_tool_policy_engine", lambda: engine)
    monkeypatch.setattr(file_module, "get_security_policy", lambda: SecurityPolicy())

    tool_context = SimpleNamespace(
        agent_name="boiled_claw",
        session=SimpleNamespace(id="session-1"),
    )
    target = tmp_path / "blocked.txt"

    result = await file_module.write_file(
        str(target),
        "blocked content",
        tool_context=tool_context,
    )

    assert "error" in result
    assert "denied" in result["error"]
    assert not target.exists()
