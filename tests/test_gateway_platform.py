import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from google.adk.events.event import Event
from google.genai import types
import pytest

from src.control_loop.root_workflow import ExecutionResult
import src.gateway.server as server_module
import src.gateway.transcript as transcript_module
import src.memory_lifecycle.adk_memory_service as adk_memory_module
import src.memory_lifecycle.promoted_store as promoted_store_module
import src.security.tool_policy as tool_policy_module
from src.gateway.transcript import TranscriptStore


class _FakeScheduler:
    def __init__(self):
        self.spawn_fn = None
        self.notifier = None
        self.events = []

    def set_spawn_fn(self, fn):
        self.spawn_fn = fn

    def set_notifier(self, fn):
        self.notifier = fn

    def start(self):
        return None

    async def shutdown(self):
        return None

    async def fire_system_event(self, event_name, context=None):
        self.events.append((event_name, context or {}))
        return 0

    def list_jobs(self):
        return []


def _build_gateway(
    monkeypatch,
    tmp_path,
    *,
    gateway_api_key=None,
    gateway_auth_user_header=None,
):
    transcript_module._store = TranscriptStore(tmp_path / "transcript.db")
    tool_policy_module._engine = None
    promoted_store_module._promoted_store = None
    adk_memory_module._promoted_memory_service = None
    scheduler = _FakeScheduler()

    async def _noop_skills():
        return None

    monkeypatch.setattr(server_module, "ensure_skills_loaded", _noop_skills)
    monkeypatch.setattr(server_module, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        server_module,
        "get_settings",
        lambda: SimpleNamespace(
            gateway_api_key=gateway_api_key,
            gateway_auth_user_header=gateway_auth_user_header,
            gateway_host="127.0.0.1",
            gateway_port=18789,
            memory_db_path=tmp_path / "memory.db",
        ),
    )

    gateway = server_module.GatewayServer()

    async def _fake_run_agent_http(user_id: str, session_id: str, message: str):
        return {"type": "agent_message", "message": f"echo:{message}"}

    gateway._run_agent_http = _fake_run_agent_http
    return gateway, scheduler


def test_http_run_persists_transcript_and_session_listing(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        run = client.post(
            "/agent/run",
            json={"user_id": "alice", "message": "hello gateway"},
        )
        assert run.status_code == 200
        payload = run.json()
        session_id = payload["session_id"]

        sessions = client.get("/sessions/alice")
        assert sessions.status_code == 200
        listed = sessions.json()["sessions"]
        assert listed[0]["id"] == session_id
        assert listed[0]["preview"].startswith("hello gateway")

        history = client.get(f"/sessions/alice/{session_id}/history")
        assert history.status_code == 200
        entries = history.json()["entries"]
        assert [entry["role"] for entry in entries] == ["user", "assistant"]
        assert entries[0]["content"] == "hello gateway"
        assert entries[1]["content"] == "echo:hello gateway"


def test_gateway_lifespan_rebinds_runtime_hooks(monkeypatch, tmp_path):
    gateway, scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app):
        assert scheduler.events == [("startup", {})]
        assert scheduler.spawn_fn is not None
        assert scheduler.notifier is not None
        assert gateway.tool_policy._notifier is not None
        assert gateway._heartbeat_task is not None

    assert gateway.tool_policy._notifier is None
    assert gateway._heartbeat_task is None

    with TestClient(gateway.app):
        assert scheduler.events == [("startup", {}), ("startup", {})]
        assert scheduler.spawn_fn is not None
        assert scheduler.notifier is not None
        assert gateway.tool_policy._notifier is not None


def test_websocket_history_and_protocol_validation(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        run = client.post(
            "/agent/run",
            json={"user_id": "alice", "message": "persist me"},
        )
        session_id = run.json()["session_id"]

        with client.websocket_connect(f"/ws/alice?session_id={session_id}") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"
            assert connected["session_id"] == session_id

            ws.send_json({"event": "chat.history", "limit": 20})
            history = ws.receive_json()
            assert history["event"] == "chat.history"
            assert history["session_id"] == session_id
            assert [entry["role"] for entry in history["entries"]] == ["user", "assistant"]

            ws.send_json({"event": "chat.send", "text": ""})
            protocol_error = ws.receive_json()
            assert protocol_error["event"] == "system.event"
            assert protocol_error["source"] == "protocol"
            assert protocol_error["status"] == "error"
            assert "at least 1 characters" in protocol_error["message"]


@pytest.mark.asyncio
async def test_run_agent_http_uses_runner_for_non_stock_query(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway._run_agent_http = server_module.GatewayServer._run_agent_http.__get__(
        gateway,
        server_module.GatewayServer,
    )

    async def _fake_run_async(*, user_id, session_id, new_message):
        assert user_id == "alice"
        assert session_id == "sess-1"
        assert new_message.parts[0].text.endswith("Explain the ADK state model")
        yield Event(
            author="root_agent",
            content=types.Content(
                role="model",
                parts=[types.Part(text="runner response")],
            ),
        )

    monkeypatch.setattr(gateway.runner, "run_async", _fake_run_async)

    result = await gateway._run_agent_http(
        user_id="alice",
        session_id="sess-1",
        message="Explain the ADK state model",
    )

    assert result == {
        "type": "agent_message",
        "message": "runner response",
        "ok": True,
    }


def test_websocket_emits_tool_events_for_runner_calls(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc-1",
                            name="web_search",
                            args={"query": "NVIDIA GTC"},
                        )
                    )
                ],
            ),
        )
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc-1",
                            name="web_search",
                            response={"results": [{"title": "NVIDIA GTC", "url": "https://example.com"}]},
                        )
                    )
                ],
            ),
        )
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="model",
                parts=[types.Part(text="grounded answer")],
            ),
        )

    monkeypatch.setattr(gateway.runner, "run_async", _fake_run_async)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "NVIDIA を検索"})

            tool_start = ws.receive_json()
            tool_result = ws.receive_json()
            done = ws.receive_json()

            assert tool_start["event"] == "tool.start"
            assert tool_start["tool_name"] == "web_search"
            assert tool_start["request_id"] == "fc-1"
            assert tool_result["event"] == "tool.result"
            assert tool_result["tool_name"] == "web_search"
            assert tool_result["ok"] is True
            assert done["event"] == "chat.done"
            assert done["text"] == "grounded answer"


def test_websocket_forces_web_search_for_fresh_query(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    recorded = {}

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "[Current request]" in text
        assert "今年日本へやってくる海外のアーティスト" in text
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"web_researcher",'
                            '"handoff_mode":"preflight_then_root",'
                            '"reason":"fresh research query","confidence":0.95,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_web_search(*, query, max_results=5, timelimit="", region="jp-jp", tool_context=None):
        recorded["query"] = query
        recorded["timelimit"] = timelimit
        recorded["region"] = region
        return {
            "results": [
                {
                    "title": "Summer Sonic lineup",
                    "snippet": "Headliners announced for Japan festivals",
                    "url": "https://example.com/lineup",
                }
            ],
            "query": query,
            "meta": {"timelimit": timelimit, "region": region},
        }

    async def _fake_specialist_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "[Grounding from web_search]" in text
        assert "Summer Sonic lineup" in text
        yield Event(
            author="web_researcher",
            content=types.Content(
                role="model",
                parts=[types.Part(text="specialist findings")],
            ),
        )

    async def _fake_root_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "[Gateway routing]" in text
        assert "Primary specialist: web_researcher" in text
        assert "[Specialist output from web_researcher]" in text
        assert "specialist findings" in text
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="model",
                parts=[types.Part(text="researched answer")],
            ),
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(server_module, "web_search", _fake_web_search)
    monkeypatch.setattr(
        gateway.specialist_runners["web_researcher"],
        "run_async",
        _fake_specialist_run_async,
    )
    monkeypatch.setattr(gateway.runner, "run_async", _fake_root_run_async)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "今年日本へやってくる海外のアーティストで有名な人は"})

            route_selected = ws.receive_json()
            tool_start = ws.receive_json()
            tool_result = ws.receive_json()
            route_forwarded = ws.receive_json()
            done = ws.receive_json()

            assert recorded["query"] == "今年日本へやってくる海外のアーティストで有名な人は"
            assert recorded["timelimit"] == "y"
            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "web_researcher"
            assert tool_start["event"] == "tool.start"
            assert tool_start["tool_name"] == "web_search"
            assert tool_start["agent_name"] == "web_researcher"
            assert tool_result["event"] == "tool.result"
            assert tool_result["tool_name"] == "web_search"
            assert tool_result["agent_name"] == "web_researcher"
            assert route_forwarded["event"] == "system.event"
            assert route_forwarded["source"] == "router"
            assert route_forwarded["agent_name"] == "root_agent"
            assert done["event"] == "chat.done"
            assert done["text"] == "researched answer"


def test_websocket_auto_routes_longform_research_to_control_loop(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"control_loop","specialist":null,'
                            '"handoff_mode":"direct","reason":"long-form report",'
                            '"confidence":0.94,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_control_run(*, goal: str, user_id: str, constraints=None, session_id=None):
        assert goal == "中東情勢を詳細に調べてレポートを書いて"
        assert user_id == "alice"
        return ExecutionResult(
            request_id="req-ctl-1",
            session_id=session_id or "session-1",
            user_id=user_id,
            final_text="control loop answer",
            plan_id="plan-ctl-1",
            success=True,
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_run)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "中東情勢を詳細に調べてレポートを書いて"})

            route_selected = ws.receive_json()
            done = ws.receive_json()

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "root_workflow"
            assert "control loop" in route_selected["message"]
            assert done["event"] == "chat.done"
            assert done["text"] == "control loop answer"


@pytest.mark.asyncio
async def test_spawn_cron_target_auto_uses_routing_agent(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"system_operator",'
                            '"handoff_mode":"direct","reason":"shell task",'
                            '"confidence":0.93,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_spawn(**kwargs):
        assert kwargs["agent_name"] == "system_operator"
        return {
            "status": "accepted",
            "run_id": "sub-123",
            "agent_name": "system_operator",
            "mode": "run",
            "requester_session_id": kwargs["requester_session_id"],
        }

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(gateway.subagent_manager, "spawn", _fake_spawn)

    result = await gateway._spawn_cron_target(
        task="docker logs を見て",
        agent_name="auto",
        requester_session_id="sess-cron",
        user_id="cron",
        app_name="boiled-claw",
        mode="run",
    )

    assert result["status"] == "accepted"
    assert result["agent_name"] == "system_operator"


@pytest.mark.asyncio
async def test_run_agent_http_routes_shell_request_to_system_operator(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway._run_agent_http = server_module.GatewayServer._run_agent_http.__get__(
        gateway,
        server_module.GatewayServer,
    )

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"system_operator",'
                            '"handoff_mode":"direct","reason":"shell task",'
                            '"confidence":0.91,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_specialist_run_async(*, user_id, session_id, new_message):
        assert "git status を見て" in new_message.parts[0].text
        yield Event(
            author="system_operator",
            content=types.Content(
                role="model",
                parts=[types.Part(text="system operator response")],
            ),
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(
        gateway.specialist_runners["system_operator"],
        "run_async",
        _fake_specialist_run_async,
    )

    result = await gateway._run_agent_http(
        user_id="alice",
        session_id="sess-shell",
        message="git status を見て",
    )

    assert result == {
        "type": "specialist",
        "message": "system operator response",
        "ok": True,
    }


def test_websocket_tool_approval_resolution(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"
            session_id = connected["session_id"]

            gateway.tool_policy.create_approval_request(
                request_id="req-123",
                tool_name="run_shell",
                agent_name="boiled_claw",
                args={"command": "echo hi"},
                session_id=session_id,
                reason="shell commands need approval",
            )
            ws.send_json(
                {
                    "event": "tools.approval",
                    "request_id": "req-123",
                    "approved": True,
                    "reason": "approved in test",
                }
            )
            resolved = ws.receive_json()
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"
            assert gateway.tool_policy.get_pending_approval("req-123") is None


def test_websocket_tool_approval_falls_back_to_control_loop(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _resolve_control_loop(
        *,
        user_id: str,
        session_id: str,
        approved: bool,
        request_id: str | None = None,
    ):
        assert user_id == "alice"
        assert session_id
        assert approved is True
        assert request_id == "missing-control-loop-request"
        return True

    monkeypatch.setattr(
        gateway.control_loop,
        "resolve_human_approval",
        _resolve_control_loop,
    )

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json(
                {
                    "event": "tools.approval",
                    "request_id": "missing-control-loop-request",
                    "approved": True,
                }
            )

            resolved = ws.receive_json()
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"


def test_http_control_loop_run_returns_pending_approval(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_control_run(*, goal: str, user_id: str, constraints=None, session_id=None):
        assert goal == "Ship the ADK-aligned runtime"
        assert user_id == "alice"
        assert session_id
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id,
            user_id=user_id,
            final_text="Plan requires human approval. Please review plan:approved in session state.",
            plan_id="plan-1",
            success=False,
            metadata={
                "needs_human": True,
                "approval_request": {
                    "request_id": "plan_req_1",
                    "plan_id": "plan-1",
                    "goal": goal,
                    "risk_level": "critical",
                    "required_capabilities": ["file.write"],
                    "plan": {"constraints": []},
                    "reason": "Human approval required due to capability or risk level.",
                },
            },
        )

    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_run)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/control-loop/run",
            json={
                "user_id": "alice",
                "goal": "Ship the ADK-aligned runtime",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert payload["needs_human"] is True
        assert payload["approval_request"]["request_id"] == "plan_req_1"


def test_websocket_control_run_emits_control_approval_request(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_control_run(*, goal: str, user_id: str, constraints=None, session_id=None):
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id or "session-1",
            user_id=user_id,
            final_text="Plan requires human approval. Please review plan:approved in session state.",
            plan_id="plan-2",
            success=False,
            metadata={
                "needs_human": True,
                "approval_request": {
                    "request_id": "plan_req_2",
                    "plan_id": "plan-2",
                    "goal": goal,
                    "risk_level": "high",
                    "required_capabilities": ["browser.navigate"],
                    "plan": {"constraints": []},
                    "reason": "Human approval required due to capability or risk level.",
                },
            },
        )

    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_run)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "control.run", "goal": "Review production diff"})

            approval = ws.receive_json()
            done = ws.receive_json()
            assert approval["event"] == "control.approval_request"
            assert approval["request_id"] == "plan_req_2"
            assert done["event"] == "chat.done"
            assert "human approval" in done["text"]


def test_cron_main_delivery_rejects_startup_without_bound_session(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    try:
        gateway._resolve_cron_delivery_target(
            {
                "delivery_target": "main",
                "system_event": "startup",
            }
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "connect/disconnect" in str(exc)


def test_http_auth_uses_authenticated_user_header_for_transcript_ownership(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        gateway_api_key="secret-token",
        gateway_auth_user_header="X-Auth-User",
    )
    alice_headers = {
        "Authorization": "Bearer secret-token",
        "X-Auth-User": "alice",
    }
    bob_headers = {
        "Authorization": "Bearer secret-token",
        "X-Auth-User": "bob",
    }

    with TestClient(gateway.app) as client:
        run = client.post(
            "/agent/run",
            headers=alice_headers,
            json={"user_id": "mallory", "message": "private transcript"},
        )
        assert run.status_code == 200
        payload = run.json()
        session_id = payload["session_id"]

        assert payload["user_id"] == "alice"
        stored = gateway.transcript.get_session(session_id)
        assert stored is not None
        assert stored.user_id == "alice"

        sessions = client.get("/sessions/mallory", headers=alice_headers)
        assert sessions.status_code == 200
        listed = sessions.json()["sessions"]
        assert listed[0]["id"] == session_id
        assert listed[0]["user_id"] == "alice"

        denied = client.get(f"/sessions/alice/{session_id}/history", headers=bob_headers)
        assert denied.status_code == 404


def test_http_auth_requires_trusted_user_header_when_configured(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        gateway_api_key="secret-token",
        gateway_auth_user_header="X-Auth-User",
    )

    with TestClient(gateway.app) as client:
        response = client.post(
            "/agent/run",
            headers={"Authorization": "Bearer secret-token"},
            json={"user_id": "alice", "message": "missing header"},
        )
        assert response.status_code == 401
        assert "X-Auth-User" in response.json()["detail"]


def test_http_auth_without_identity_header_uses_shared_api_key_principal(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        gateway_api_key="secret-token",
    )

    with TestClient(gateway.app) as client:
        response = client.post(
            "/agent/run",
            headers={"Authorization": "Bearer secret-token"},
            json={"user_id": "mallory", "message": "shared principal"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["user_id"] == gateway._shared_api_key_principal()
        assert payload["user_id"] != "mallory"


def test_websocket_auth_ignores_path_user_id_when_identity_header_is_present(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        gateway_api_key="secret-token",
        gateway_auth_user_header="X-Auth-User",
    )

    with TestClient(gateway.app) as client:
        with client.websocket_connect(
            "/ws/spoofed?token=secret-token",
            headers={"X-Auth-User": "alice"},
        ) as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"
            assert connected["user_id"] == "alice"
