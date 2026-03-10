import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.memory.base_memory_service import MemoryEntry, SearchMemoryResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from src.control_loop import guarded_tools as guarded_tools_module
from src.control_loop.root_workflow import ControlLoop
import src.memory_lifecycle.candidate_store as candidate_store_module
from src.memory_lifecycle.adk_memory_service import PromotedMemoryService
from src.memory_lifecycle.candidate_store import CandidateStore
from src.memory_lifecycle.memory_schema import (
    MemoryCandidate,
    MemoryType,
    OriginatorType,
    Provenance,
    SensitivityLevel,
)
from src.memory_lifecycle.promoted_store import PromotedMemoryStore
from src.runtime.state_keys import StateKeys
from src.tools.context import resolve_callback_context, resolve_tool_context


def _make_runtime_context():
    return SimpleNamespace(
        agent_name="executor",
        invocation_id="inv-1",
        _invocation_context=SimpleNamespace(
            app_name="boiled_claw_v2",
            user_id="user-1",
            session=SimpleNamespace(id="session-1"),
        ),
    )


def test_resolve_tool_context_uses_invocation_context():
    resolved = resolve_tool_context(_make_runtime_context())

    assert resolved == {
        "agent_name": "executor",
        "session_id": "session-1",
        "user_id": "user-1",
        "app_name": "boiled_claw_v2",
        "invocation_id": "inv-1",
    }


def test_resolve_callback_context_falls_back_to_legacy_session():
    callback_context = SimpleNamespace(
        agent_name="verifier",
        invocation_id="inv-2",
        session=SimpleNamespace(id="legacy-session"),
    )

    resolved = resolve_callback_context(callback_context)

    assert resolved["agent_name"] == "verifier"
    assert resolved["session_id"] == "legacy-session"
    assert resolved["user_id"] == ""


@pytest.mark.asyncio
async def test_guarded_memory_read_requires_capability():
    tool_context = SimpleNamespace(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: json.dumps(
                {"required_capabilities": [{"name": "web.search"}]}
            ),
        }
    )

    with pytest.raises(PermissionError, match="memory.read"):
        await guarded_tools_module.guarded_memory_read(
            query="release notes",
            tool_context=tool_context,
        )


@pytest.mark.asyncio
async def test_guarded_memory_read_prefers_adk_memory():
    class FakeToolContext(SimpleNamespace):
        async def search_memory(self, query: str) -> SearchMemoryResponse:
            assert query == "project history"
            return SearchMemoryResponse(
                memories=[
                    MemoryEntry(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text="remembered fact")],
                        ),
                        author="memory",
                        timestamp="2026-03-10T00:00:00Z",
                    )
                ]
            )

    tool_context = FakeToolContext(
        state={
            StateKeys.APPROVAL_STATUS: "policy_approved",
            StateKeys.PLAN_APPROVED: {
                "required_capabilities": [{"name": "memory.read"}]
            },
        }
    )

    result = await guarded_tools_module.guarded_memory_read(
        query="project history",
        tool_context=tool_context,
    )

    assert result["source"] == "adk_memory"
    assert result["count"] == 1
    assert result["results"][0]["content"] == "remembered fact"


@pytest.mark.asyncio
async def test_control_loop_resumes_after_human_approval(monkeypatch, tmp_path):
    session_service = InMemorySessionService()
    candidate_store = CandidateStore(
        str(tmp_path / "candidates.db")
    )
    monkeypatch.setattr(candidate_store_module, "_candidate_store", candidate_store)
    memory_service = PromotedMemoryService(
        PromotedMemoryStore(str(tmp_path / "promoted.db"))
    )
    await session_service.create_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
        state={
            StateKeys.TASK_GOAL: "Ship the patch",
            StateKeys.TASK_CONSTRAINTS: [],
            StateKeys.REPAIR_COUNT: 0,
            StateKeys.APPROVAL_STATUS: "needs_human",
            StateKeys.PLAN_APPROVED: {
                "plan_id": "plan-1",
                "required_capabilities": [{"name": "file.read"}],
            },
            "custom:key": "keep",
        },
    )
    candidate_store.save(
        MemoryCandidate(
            candidate_id="cand-1",
            session_id="sess-1",
            user_id="user-1",
            memory_type=MemoryType.PROCEDURAL,
            content="Ship the patch via control loop",
            subject="Ship the patch",
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="test",
                captured_at=datetime.now(timezone.utc),
            ),
            confidence=0.9,
            trust_score=0.9,
            sensitivity=SensitivityLevel.INTERNAL,
        )
    )

    loop = ControlLoop(
        session_service=session_service,
        memory_service=memory_service,
    )
    calls: list[str] = []

    async def fake_run_agent(agent, *, session_id, user_id, message):
        calls.append(agent.name)
        session = await session_service.get_session(
            app_name="boiled_claw_v2",
            user_id=user_id,
            session_id=session_id,
        )
        assert session is not None

        if agent.name == "executor":
            await session_service.append_event(
                session,
                Event(
                    invocation_id="test:executor",
                    author=agent.name,
                    actions=EventActions(
                        state_delta={
                            StateKeys.TEMP_EXECUTOR_OUTPUTS: {
                                "plan_id": "plan-1",
                                "summary": "executed",
                            }
                        }
                    ),
                ),
            )
            return

        if agent.name == "verifier":
            await session_service.append_event(
                session,
                Event(
                    invocation_id="test:verifier",
                    author=agent.name,
                    actions=EventActions(
                        state_delta={
                            StateKeys.VERIFY_LAST_REPORT: {
                                "report_id": "report-1",
                                "plan_id": "plan-1",
                                "status": "pass",
                                "overall_score": 0.9,
                                "summary": "verified",
                            }
                        }
                    ),
                ),
            )
            return

        raise AssertionError("planner should not run when resuming an approved plan")

    monkeypatch.setattr(loop, "_run_agent", fake_run_agent)

    resolved = await loop.resolve_human_approval(
        user_id="user-1",
        session_id="sess-1",
        approved=True,
    )
    assert resolved is True

    result = await loop.run(
        goal="Ship the patch",
        user_id="user-1",
        session_id="sess-1",
    )

    session = await session_service.get_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
    )
    assert session is not None
    assert session.state["custom:key"] == "keep"
    assert calls == ["executor", "verifier"]
    assert result.success is True
    assert result.plan_id == "plan-1"
    assert len(result.promoted_memory_ids) == 1
    assert result.metadata["session_created"] is False

    memory_result = await memory_service.search_memory(
        app_name="boiled_claw_v2",
        user_id="user-1",
        query="control loop",
    )
    assert memory_result.memories


@pytest.mark.asyncio
async def test_control_loop_rejects_goal_change_for_existing_session():
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="boiled_claw_v2",
        user_id="user-1",
        session_id="sess-1",
        state={StateKeys.TASK_GOAL: "Old goal"},
    )

    loop = ControlLoop(session_service=session_service)

    with pytest.raises(ValueError, match="different task goal"):
        await loop.run(
            goal="New goal",
            user_id="user-1",
            session_id="sess-1",
        )
