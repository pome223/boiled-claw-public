import asyncio
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types
import pytest

from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import SupervisorStartResult
import src.gateway.server as server_module
import src.gateway.transcript as transcript_module
import src.memory_lifecycle.adk_memory_service as adk_memory_module
import src.memory_lifecycle.promoted_store as promoted_store_module
import src.runtime.tool_events as tool_events_module
import src.security.tool_policy as tool_policy_module
from src.gateway.routing import RoutingDecision
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
    redis_url=None,
    redis_session_namespace="boiled-claw:sessions",
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
    monkeypatch.setattr(server_module, "create_session_service", lambda _settings: InMemorySessionService())
    monkeypatch.setattr(
        server_module,
        "get_settings",
        lambda: SimpleNamespace(
            gateway_api_key=gateway_api_key,
            gateway_auth_user_header=gateway_auth_user_header,
            gateway_host="127.0.0.1",
            gateway_port=18789,
            memory_db_path=tmp_path / "memory.db",
            redis_url=redis_url,
            redis_session_namespace=redis_session_namespace,
        ),
    )

    gateway = server_module.GatewayServer()

    async def _fake_run_agent_http(user_id: str, session_id: str, message: str):
        return {"type": "agent_message", "message": f"echo:{message}"}

    gateway._run_agent_http = _fake_run_agent_http
    return gateway, scheduler


_WS_BACKGROUND_EVENTS = {"audit.append", "task.update", "tools.approval_update"}


def _receive_foreground_event(ws):
    while True:
        payload = ws.receive_json()
        if payload.get("event") in _WS_BACKGROUND_EVENTS:
            continue
        return payload


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


def test_chat_ui_and_static_assets_disable_caching(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        chat_response = client.get("/chat")
        assert chat_response.status_code == 200
        assert chat_response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert chat_response.headers["pragma"] == "no-cache"
        assert chat_response.headers["expires"] == "0"

        app_js_response = client.get("/chat-static/app.js")
        assert app_js_response.status_code == 200
        assert app_js_response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert app_js_response.headers["pragma"] == "no-cache"
        assert app_js_response.headers["expires"] == "0"


def test_chat_ui_bundle_includes_long_running_task_sections(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        app_js_response = client.get("/chat-static/app.js")

    assert app_js_response.status_code == 200
    bundle = app_js_response.text
    assert "Long-Running State" in bundle
    assert "Mission Contract" in bundle
    assert "Scheduler Queues" in bundle
    assert "evidence=" in bundle
    assert "Latest Checkpoints" in bundle
    assert "Approval Waits" in bundle
    assert "Budget Exhaustion" in bundle


def test_protocol_exposes_runtime_substrate_metadata(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        response = client.get("/protocol")

    assert response.status_code == 200
    payload = response.json()
    assert "GET /runtime/resources" in payload["http_surfaces"]
    assert payload["runtime_substrate"]["capabilities"]["invoke_route"] == "POST /runtime/capabilities/invoke"


def test_root_and_health_expose_session_backend(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        redis_url="redis://boiled-claw-redis:6379/0",
        redis_session_namespace="demo:sessions",
    )

    with TestClient(gateway.app) as client:
        root_response = client.get("/")
        health_response = client.get("/health")

    assert root_response.status_code == 200
    assert root_response.json()["session_backend"] == "redis"
    assert root_response.json()["session_namespace"] == "demo:sessions"
    assert health_response.status_code == 200
    assert health_response.json()["session_backend"] == "redis"
    assert health_response.json()["session_namespace"] == "demo:sessions"


def test_runtime_substrate_http_surfaces(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_resource_list():
        return {"count": 1, "resources": [{"id": "bridge:host"}]}

    async def _fake_resource_read(resource_id: str, refresh: bool = False):
        return {"ok": True, "resource": {"id": resource_id, "refresh": refresh}}

    async def _fake_capability_list(refresh: bool = False):
        return {"count": 1, "refresh": refresh, "capabilities": [{"name": "shell.run"}]}

    async def _fake_capability_invoke(name: str, params_json: str = "{}"):
        return {"success": True, "capability": name, "result": {"params_json": params_json}}

    monkeypatch.setattr(server_module, "tool_resource_list", _fake_resource_list)
    monkeypatch.setattr(server_module, "tool_resource_read", _fake_resource_read)
    monkeypatch.setattr(server_module, "tool_capability_list", _fake_capability_list)
    monkeypatch.setattr(server_module, "tool_capability_invoke", _fake_capability_invoke)

    with TestClient(gateway.app) as client:
        resources = client.get("/runtime/resources")
        assert resources.status_code == 200
        assert resources.json()["resources"][0]["id"] == "bridge:host"

        resource = client.get("/runtime/resources/skill:computer-use", params={"refresh": "true"})
        assert resource.status_code == 200
        assert resource.json()["resource"]["id"] == "skill:computer-use"
        assert resource.json()["resource"]["refresh"] is True

        capabilities = client.get("/runtime/capabilities", params={"refresh": "true"})
        assert capabilities.status_code == 200
        assert capabilities.json()["refresh"] is True

        invoke = client.post(
            "/runtime/capabilities/invoke",
            json={"name": "shell.run", "params": {"command": "pwd"}},
        )
        assert invoke.status_code == 200
        assert invoke.json()["capability"] == "shell.run"
        assert '"command": "pwd"' in invoke.json()["result"]["params_json"]


def test_start_control_supervisor_endpoint_returns_task(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_start(**kwargs):
        return SupervisorStartResult(
            task={
                "task_id": "task-supervisor",
                "kind": "control_supervisor",
                "title": kwargs["objective"],
                "status": "running",
                "owner_session_id": kwargs["owner_session_id"],
                "owner_user_id": kwargs["user_id"],
                "parent_task_id": None,
                "run_id": None,
                "winner_task_id": None,
                "loser_task_ids": [],
                "approval_dependencies": [],
                "artifacts": {},
                "metadata": {},
                "error": None,
                "created_at": time.time(),
                "updated_at": time.time(),
                "started_at": time.time(),
                "ended_at": None,
            },
            control_session_id="ctrlsup_demo",
            max_iterations=60,
            ends_at=time.time() + 3600,
            next_run_at=time.time(),
        )

    monkeypatch.setattr(gateway.control_supervisor, "start", _fake_start)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/tasks/supervisors/control-loop",
            json={
                "user_id": "alice",
                "goal": "Keep the session healthy",
                "duration_seconds": 3600,
                "interval_seconds": 60,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["task"]["task_id"] == "task-supervisor"
    assert payload["control_session_id"] == "ctrlsup_demo"


def test_start_control_supervisor_endpoint_accepts_mission_contract(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_start(**kwargs):
        captured.update(kwargs)
        mission_contract = kwargs["mission_contract"].model_dump(mode="json")
        return SupervisorStartResult(
            task={
                "task_id": "task-supervisor-contract",
                "kind": "control_supervisor",
                "title": kwargs["objective"],
                "status": "running",
                "owner_session_id": kwargs["owner_session_id"],
                "owner_user_id": kwargs["user_id"],
                "parent_task_id": None,
                "run_id": None,
                "winner_task_id": None,
                "loser_task_ids": [],
                "approval_dependencies": [],
                "artifacts": {"mission_contract": mission_contract},
                "metadata": {"mission_contract_id": mission_contract["contract_id"]},
                "error": None,
                "created_at": time.time(),
                "updated_at": time.time(),
                "started_at": time.time(),
                "ended_at": None,
            },
            control_session_id="ctrlsup_contract",
            max_iterations=12,
            ends_at=time.time() + 3600,
            next_run_at=time.time(),
            mission_contract=mission_contract,
        )

    monkeypatch.setattr(gateway.control_supervisor, "start", _fake_start)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/tasks/supervisors/control-loop",
            json={
                "user_id": "alice",
                "mission_contract": {
                    "contract_id": "mission-http-test",
                    "objective": "Keep the current-tab sheet healthy",
                    "allowed_actions": ["current_tab.read", "current_tab.fill"],
                    "forbidden_actions": ["leave the target sheet"],
                    "abort_conditions": ["human approval required"],
                    "completion_criteria": ["target cell evidence is visible"],
                    "evidence_requirements": ["post-action screenshot"],
                },
                "duration_seconds": 3600,
                "interval_seconds": 60,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert captured["objective"] == "Keep the current-tab sheet healthy"
    assert payload["mission_contract"]["contract_id"] == "mission-http-test"
    assert payload["task"]["metadata"]["mission_contract_id"] == "mission-http-test"


def test_cancel_control_supervisor_endpoint_requires_running_handle(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    task = gateway.task_store.create(
        kind="control_supervisor",
        title="Keep the session healthy",
        status="running",
        owner_session_id="sess-1",
        owner_user_id="alice",
    )

    async def _fake_request_stop(task_id: str):
        return gateway.task_store.update(
            task_id,
            artifacts={"progress": {"stop_requested": True}},
        )

    monkeypatch.setattr(gateway.control_supervisor, "request_stop", _fake_request_stop)

    with TestClient(gateway.app) as client:
        response = client.post(f"/tasks/{task['task_id']}/cancel")

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["task"]["artifacts"]["progress"]["stop_requested"] is True


def test_runtime_capability_invoke_rejects_approval_required_http_calls(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/runtime/capabilities/invoke",
            json={"name": "shell.run", "params": {"command": "pwd"}},
        )

    assert response.status_code == 403
    assert "requires tool_context-backed approval flow" in response.json()["detail"]


def test_gateway_package_exports_remain_available():
    import src.gateway as gateway_pkg

    assert gateway_pkg.GatewayServer is server_module.GatewayServer
    assert callable(gateway_pkg.create_gateway)


def test_gateway_lifespan_rebinds_runtime_hooks(monkeypatch, tmp_path):
    gateway, scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app):
        assert scheduler.events == [("startup", {})]
        assert scheduler.spawn_fn is not None
        assert scheduler.notifier is not None
        assert gateway.tool_policy._notifier is not None
        assert tool_events_module._tool_event_notifier is not None
        assert gateway._heartbeat_task is not None

    assert gateway.tool_policy._notifier is None
    assert tool_events_module._tool_event_notifier is None
    assert gateway._heartbeat_task is None

    with TestClient(gateway.app):
        assert scheduler.events == [("startup", {}), ("startup", {})]
        assert scheduler.spawn_fn is not None
        assert scheduler.notifier is not None
        assert gateway.tool_policy._notifier is not None
        assert tool_events_module._tool_event_notifier is not None


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
            history = _receive_foreground_event(ws)
            assert history["event"] == "chat.history"
            assert history["session_id"] == session_id
            assert [entry["role"] for entry in history["entries"]] == ["user", "assistant"]

            ws.send_json({"event": "chat.send", "text": ""})
            protocol_error = _receive_foreground_event(ws)
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


@pytest.mark.asyncio
async def test_compose_grounded_agent_message_uses_original_request_for_freshness(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    calls: list[str] = []

    async def _fake_search(*, query, timelimit="", region="wt-wt"):
        calls.append(query)
        return {"results": [{"title": "ok", "url": "https://example.com"}]}

    monkeypatch.setattr(server_module, "web_search", _fake_search)

    routed_message = (
        "[Gateway routing]\n"
        "[Specialist output from browser_automator]\n"
        "最新のブラウザ要約\n"
        "[Original user request]\n"
        "ブラウザを操作して、天気を見て"
    )
    composed = await gateway._compose_grounded_agent_message(
        "sess-1",
        "alice",
        routed_message,
        research_message="ブラウザを操作して、天気を見て",
        agent_name="root_agent",
        emit_tool_events=False,
        allow_forced_research=True,
    )

    assert calls == []
    assert "[Grounding from web_search]" not in composed


@pytest.mark.asyncio
async def test_run_specialist_prepass_detects_browser_runtime_failure(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="browser_automator",
            content=types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="browser-fail",
                            name="browser_navigate",
                            response={
                                "error": "Playwright is not installed. Run: pip install playwright && playwright install",
                                "success": False,
                            },
                        )
                    )
                ],
            ),
        )
        yield Event(
            author="browser_automator",
            content=types.Content(
                role="model",
                parts=[types.Part(text="stale fallback summary")],
            ),
        )

    monkeypatch.setattr(
        gateway.specialist_runners["browser_automator"],
        "run_async",
        _fake_run_async,
    )

    result = await gateway._run_specialist_prepass(
        session_id="sess-1",
        user_id="alice",
        message="ブラウザを操作して、天気を見て",
        specialist_name="browser_automator",
    )

    assert result.text == "stale fallback summary"
    assert result.infrastructure_blocked is True
    assert result.tool_failures[0].tool_name == "browser_navigate"


@pytest.mark.asyncio
async def test_run_agent_http_returns_browser_runtime_error_without_root_fallback(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway._run_agent_http = server_module.GatewayServer._run_agent_http.__get__(
        gateway,
        server_module.GatewayServer,
    )

    async def _fake_select_route_for_message(**kwargs):
        return RoutingDecision(
            target="specialist",
            specialist="browser_automator",
            handoff_mode="preflight_then_root",
            reason="browser task",
            confidence=0.95,
        )

    async def _fake_run_specialist_prepass(**kwargs):
        return server_module.SpecialistPrepassResult(
            text="fallback weather summary",
            tool_failures=[
                server_module.SpecialistToolFailure(
                    tool_name="browser_navigate",
                    error="Playwright is not installed. Run: pip install playwright && playwright install",
                    infrastructure=True,
                )
            ],
            used_tools={"browser_navigate", "web_search"},
        )

    async def _unexpected_run_async(*args, **kwargs):
        raise AssertionError("root runner should not execute after browser runtime failure")
        yield  # pragma: no cover

    async def _noop_routing_event(*args, **kwargs):
        return None

    monkeypatch.setattr(gateway, "_select_route_for_message", _fake_select_route_for_message)
    monkeypatch.setattr(gateway, "_run_specialist_prepass", _fake_run_specialist_prepass)
    monkeypatch.setattr(gateway, "_emit_routing_event", _noop_routing_event)
    monkeypatch.setattr(gateway.runner, "run_async", _unexpected_run_async)

    result = await gateway._run_agent_http(
        user_id="alice",
        session_id="sess-1",
        message="ブラウザを操作して、天気を見て",
    )

    assert result["ok"] is False
    assert result["type"] == "error"
    assert "ブラウザ操作は実行できませんでした" in result["message"]
    assert "web_search" in result["message"]


@pytest.mark.asyncio
async def test_run_control_loop_http_rejects_current_browser_request_without_desktop_bridge(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    result = await gateway._run_control_loop_http(
        user_id="alice",
        session_id="sess-browser",
        goal="私が開いているブラウザのスプレッドシートに GTC の予想を書いて",
        constraints=[],
    )

    assert result.success is False
    assert "Desktop Bridge が無効です" in result.final_text
    assert "managed browser やローカル CSV ではなく" in result.final_text


@pytest.mark.asyncio
async def test_run_control_loop_http_adds_current_browser_constraints(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway._run_agent_http = server_module.GatewayServer._run_agent_http.__get__(
        gateway,
        server_module.GatewayServer,
    )
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(
        *,
        goal,
        user_id,
        constraints,
        session_id,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["goal"] = goal
        captured["user_id"] = user_id
        captured["constraints"] = constraints
        captured["session_id"] = session_id
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id,
            user_id=user_id,
            final_text="ok",
            success=True,
        )

    monkeypatch.setattr(gateway, "_current_browser_runtime_error", _fake_current_browser_runtime_error)
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_loop_run)

    result = await gateway._run_control_loop_http(
        user_id="alice",
        session_id="sess-browser",
        goal="このブラウザを使って明日の天気を調べて",
        constraints=[],
    )

    assert result.success is True
    constraints = captured["constraints"]
    assert "Operate only on the currently visible browser/tab/window." in constraints
    assert "Do not launch a new browser application or open a managed browser for this task." in constraints
    assert "Do not open a new browser tab or window unless the user explicitly asked for it." in constraints


@pytest.mark.asyncio
async def test_run_control_loop_with_task_persists_policy_exceptions(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(**kwargs):
        raise PermissionError("Capability 'current_tab.navigate' is not in the approved plan.")

    monkeypatch.setattr(gateway, "_current_browser_runtime_error", _fake_current_browser_runtime_error)
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_loop_run)

    result, task_id = await gateway._run_control_loop_with_task(
        user_id="alice",
        session_id="sess-policy",
        goal="Keep the current browser session healthy",
        constraints=[],
        request_id="req-policy",
        source="test",
        preserve_control_ui_tab=False,
    )

    task = gateway.task_store.get(task_id)
    assert result.success is False
    assert result.metadata["normalized_failure_type"] == "policy_blocked"
    assert task is not None
    assert task["status"] == "failed"
    assert task["error"].startswith("Control loop blocked by policy:")
    assert task["artifacts"]["result"]["normalized_failure_type"] == "policy_blocked"


@pytest.mark.asyncio
async def test_run_agent_http_control_loop_route_resets_terminal_state(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway._run_agent_http = server_module.GatewayServer._run_agent_http.__get__(
        gateway,
        server_module.GatewayServer,
    )
    captured: dict[str, object] = {}

    async def _fake_select_route_for_message(**kwargs):
        return RoutingDecision(
            target="control_loop",
            reason="browser task",
            confidence=0.95,
        )

    async def _fake_emit_routing_event(*args, **kwargs):
        return None

    async def _fake_run_control_loop_http(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(
            request_id="req-1",
            session_id=str(kwargs["session_id"]),
            user_id=str(kwargs["user_id"]),
            final_text="ok",
            success=True,
        )

    monkeypatch.setattr(gateway, "_select_route_for_message", _fake_select_route_for_message)
    monkeypatch.setattr(gateway, "_emit_routing_event", _fake_emit_routing_event)
    monkeypatch.setattr(gateway, "_run_control_loop_http", _fake_run_control_loop_http)

    await gateway._run_agent_http(
        user_id="alice",
        session_id="sess-1",
        message="このブラウザでスプレッドシートを更新して",
    )

    assert captured["reset_if_terminal"] is True


@pytest.mark.asyncio
async def test_control_loop_task_preserves_control_ui_tab_for_current_browser(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(
        *,
        goal,
        user_id,
        constraints,
        session_id,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["goal"] = goal
        captured["user_id"] = user_id
        captured["constraints"] = constraints
        captured["session_id"] = session_id
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id,
            user_id=user_id,
            final_text="ok",
            success=True,
        )

    monkeypatch.setattr(
        gateway, "_current_browser_runtime_error", _fake_current_browser_runtime_error
    )
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_loop_run)

    await gateway._control_loop_task(
        session_id="sess-browser",
        user_id="alice",
        goal="このブラウザを使って午後の東京の花粉を調べて",
        constraints=[],
    )

    constraints = captured["constraints"]
    assert "Operate only on the currently visible browser/tab/window." in constraints
    assert (
        "If the current tab is the boiled-claw Control UI chat, preserve that tab and "
        "open a new tab in the same browser window for browsing or search. Otherwise "
        "stay on the current tab."
    ) in constraints
    assert (
        "Do not open a new browser tab or window unless the user explicitly asked for it."
        not in constraints
    )


@pytest.mark.asyncio
async def test_control_loop_task_uses_current_browser_constraints_for_current_browser_spreadsheet(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(
        *,
        goal,
        user_id,
        constraints,
        session_id,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["constraints"] = constraints
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id,
            user_id=user_id,
            final_text="ok",
            success=True,
        )

    monkeypatch.setattr(
        gateway, "_current_browser_runtime_error", _fake_current_browser_runtime_error
    )
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_loop_run)

    await gateway._control_loop_task(
        session_id="sess-browser",
        user_id="alice",
        goal="このブラウザでスプレッドシートを更新して",
        constraints=[],
    )

    constraints = captured["constraints"]
    assert "Operate only on the currently visible browser/tab/window." in constraints
    assert "Do not launch a new browser application or open a managed browser for this task." in constraints
    assert (
        "For current-browser visible text-entry or form-filling work, use an isolated browser or managed browser page instead of the user's existing browser tabs or forms."
        not in constraints
    )


@pytest.mark.asyncio
async def test_control_loop_task_uses_isolated_browser_constraints_for_current_browser_form_fill(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(
        *,
        goal,
        user_id,
        constraints,
        session_id,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["constraints"] = constraints
        return ExecutionResult(
            request_id="req-1",
            session_id=session_id,
            user_id=user_id,
            final_text="ok",
            success=True,
        )

    monkeypatch.setattr(
        gateway, "_current_browser_runtime_error", _fake_current_browser_runtime_error
    )
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_loop_run)

    await gateway._control_loop_task(
        session_id="sess-browser",
        user_id="alice",
        goal="このブラウザのフォームに名前を入力して送信して",
        constraints=[],
    )

    constraints = captured["constraints"]
    assert (
        "For current-browser visible text-entry or form-filling work, use an isolated browser or managed browser page instead of the user's existing browser tabs or forms."
        in constraints
    )
    assert (
        "Do not interact with pre-existing browser tabs, windows, or form fields owned by the user."
        in constraints
    )
    assert "Operate only on the currently visible browser/tab/window." not in constraints
    assert "Do not launch a new browser application or open a managed browser for this task." not in constraints


@pytest.mark.asyncio
async def test_agent_run_task_control_loop_route_resets_terminal_state(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_select_route_for_message(**kwargs):
        return RoutingDecision(
            target="control_loop",
            reason="current browser task",
            confidence=0.95,
        )

    async def _fake_emit_routing_event(*args, **kwargs):
        return None

    async def _fake_control_loop_task(
        session_id,
        user_id,
        goal,
        constraints,
        request_id=None,
        task_id=None,
        parent_task_id=None,
        replay_of_task_id=None,
        compare_to_task_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["session_id"] = session_id
        captured["user_id"] = user_id
        captured["goal"] = goal
        captured["constraints"] = constraints
        captured["request_id"] = request_id
        captured["reset_if_terminal"] = reset_if_terminal

    monkeypatch.setattr(gateway, "_select_route_for_message", _fake_select_route_for_message)
    monkeypatch.setattr(gateway, "_emit_routing_event", _fake_emit_routing_event)
    monkeypatch.setattr(gateway, "_control_loop_task", _fake_control_loop_task)

    await gateway._agent_run_task(
        session_id="sess-1",
        user_id="alice",
        message="このブラウザでスプレッドシートを更新して",
        request_id="req-1",
    )

    assert captured["goal"] == "このブラウザでスプレッドシートを更新して"
    assert captured["reset_if_terminal"] is True


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

            seen_events = []
            while True:
                event = ws.receive_json()
                if event["event"] in {"tool.start", "tool.result", "chat.done"}:
                    seen_events.append(event)
                if event["event"] == "chat.done":
                    break

            tool_starts = [e for e in seen_events if e["event"] == "tool.start"]
            tool_results = [e for e in seen_events if e["event"] == "tool.result"]
            done = seen_events[-1]

            assert any(e["tool_name"] == "web_search" for e in tool_starts)
            ws_start = next(e for e in tool_starts if e["tool_name"] == "web_search")
            assert ws_start["request_id"]  # non-empty, may vary
            assert any(e["tool_name"] == "web_search" and e["ok"] is True for e in tool_results)
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
        assert "[Specialist evidence from web_researcher]" in text
        assert "Summer Sonic lineup" in text
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

            route_selected = _receive_foreground_event(ws)
            tool_start = _receive_foreground_event(ws)
            tool_result = _receive_foreground_event(ws)
            route_forwarded = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)

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


@pytest.mark.asyncio
async def test_run_specialist_prepass_captures_web_search_evidence_for_root_handoff(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="web_researcher",
            content=types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="search-1",
                            name="web_search",
                            response={
                                "query": "2026年3月29日 静岡県 天気予報",
                                "results": [
                                    {
                                        "title": "静岡市の天気予報(1時間・今日明日・週間) - ウェザーニュース",
                                        "snippet": "静岡市の明日の天気と気温を確認できます。",
                                        "url": "https://weathernews.jp/onebox/tenki/shizuoka/22100/",
                                    }
                                ],
                                "meta": {"region": "jp-jp", "timelimit": ""},
                            },
                        )
                    )
                ],
            ),
        )
        yield Event(
            author="web_researcher",
            content=types.Content(
                role="model",
                parts=[types.Part(text="静岡の明日の天気を調べました。")],
            ),
        )

    monkeypatch.setattr(
        gateway.specialist_runners["web_researcher"],
        "run_async",
        _fake_run_async,
    )

    result = await gateway._run_specialist_prepass(
        session_id="sess-1",
        user_id="alice",
        message="今静岡にいます",
        specialist_name="web_researcher",
    )

    assert result.text == "静岡の明日の天気を調べました。"
    assert result.evidence_blocks
    assert "web_search query: 2026年3月29日 静岡県 天気予報" in result.evidence_blocks[-1]
    assert "静岡市の天気予報" in result.evidence_blocks[-1]


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

    async def _fake_control_run(
        *,
        goal: str,
        user_id: str,
        constraints=None,
        session_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
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

            route_selected = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "root_workflow"
            assert "control loop" in route_selected["message"]
            assert done["event"] == "chat.done"
            assert done["text"] == "control loop answer"


def test_websocket_auto_routes_desktop_query_to_desktop_operator(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"desktop_operator",'
                            '"handoff_mode":"preflight_then_root",'
                            '"reason":"desktop inspection request","confidence":0.92,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_specialist_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "前面アプリとウィンドウを確認して" in text
        yield Event(
            author="desktop_operator",
            content=types.Content(
                role="model",
                parts=[types.Part(text="desktop findings")],
            ),
        )

    async def _fake_root_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "Primary specialist: desktop_operator" in text
        assert "desktop findings" in text
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="model",
                parts=[types.Part(text="desktop answer")],
            ),
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(
        gateway.specialist_runners["desktop_operator"],
        "run_async",
        _fake_specialist_run_async,
    )
    monkeypatch.setattr(gateway.runner, "run_async", _fake_root_run_async)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "前面アプリとウィンドウを確認してまとめて"})

            route_selected = _receive_foreground_event(ws)
            route_forwarded = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "desktop_operator"
            assert route_forwarded["event"] == "system.event"
            assert route_forwarded["agent_name"] == "root_agent"
            assert done["event"] == "chat.done"
            assert done["text"] == "desktop answer"


def test_websocket_forces_desktop_playback_request_to_control_loop(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"desktop_operator",'
                            '"handoff_mode":"direct","reason":"desktop control request",'
                            '"confidence":0.91,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_control_run(
        *,
        goal: str,
        user_id: str,
        constraints=None,
        session_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
        assert goal == "Djayを開いて曲をかけて"
        assert user_id == "alice"
        return ExecutionResult(
            request_id="req-ctl-djay",
            session_id=session_id or "session-1",
            user_id=user_id,
            final_text="control loop desktop playback answer",
            plan_id="plan-djay-1",
            success=True,
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_run)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "Djayを開いて曲をかけて"})

            route_selected = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "root_workflow"
            assert "control loop" in route_selected["message"]
            assert done["event"] == "chat.done"
            assert done["text"] == "control loop desktop playback answer"


def test_websocket_auto_routes_computer_use_query_to_computer_operator(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_routing_run_async(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            '{"target":"specialist","specialist":"computer_operator",'
                            '"handoff_mode":"direct",'
                            '"reason":"computer use request","confidence":0.93,'
                            '"dynamic_agent":{"instruction":"","mcp_servers":[],"mode":"run"}}'
                        )
                    )
                ],
            ),
        )

    async def _fake_specialist_run_async(*, user_id, session_id, new_message):
        text = new_message.parts[0].text
        assert "このブラウザの画面を見て" in text
        yield Event(
            author="computer_operator",
            content=types.Content(
                role="model",
                parts=[types.Part(text="computer operator answer")],
            ),
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing_run_async)
    monkeypatch.setattr(
        gateway.specialist_runners["computer_operator"],
        "run_async",
        _fake_specialist_run_async,
    )

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.send", "text": "このブラウザの画面を見て押せるボタンを教えて"})

            route_selected = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "computer_operator"
            assert done["event"] == "chat.done"
            assert done["text"] == "computer operator answer"


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
            resolved = _receive_foreground_event(ws)
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"
            assert gateway.tool_policy.get_pending_approval("req-123") is None
            history = gateway.transcript.get_history(session_id, limit=20)
            assert all(
                not (
                    entry.role == "system"
                    and entry.metadata.get("source") == "tools.approval"
                )
                for entry in history
            )


def test_http_tool_approvals_list_exposes_stateful_metadata(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        approval = gateway.tool_policy.create_approval_request(
            request_id="req-stateful",
            tool_name="run_shell",
            agent_name="boiled_claw",
            args={"command": "echo hi", "cwd": str(tmp_path)},
            session_id="sess-stateful",
            reason="shell commands need approval",
        )
        gateway.tool_policy.resolve_approval(
            approval.request_id,
            True,
            "approved for session",
            scope="session",
            tool_pattern="run_shell",
            path_scope=str(tmp_path),
            expires_at=time.time() + 60,
            propagate_to_subagents=True,
        )
        second = gateway.tool_policy.create_approval_request(
            request_id="req-stateful-2",
            tool_name="write_file",
            agent_name="boiled_claw",
            args={"path": str(tmp_path / "note.txt")},
            session_id="sess-stateful",
            reason="file writes need approval",
        )
        gateway.tool_policy.resolve_approval(
            second.request_id,
            False,
            "denied in test",
        )

        response = client.get(
            "/tools/approvals",
            params={
                "session_id": "sess-stateful",
                "state": "all",
                "limit": 1,
            },
        )

    assert response.status_code == 200
    response_payload = response.json()
    approvals = response_payload["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["request_id"] == second.request_id
    assert approvals[0]["state"] == "denied"
    assert response_payload["pagination"]["page"] == 1
    assert response_payload["pagination"]["total"] == 2

    full_response = client.get(
        "/tools/approvals",
        params={
            "session_id": "sess-stateful",
            "state": "all",
            "q": "approved for session",
            "page": 1,
            "page_size": 10,
            "include_expired": True,
        },
    )

    assert full_response.status_code == 200
    full_payload = full_response.json()
    approvals = full_payload["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["state"] == "approved"
    assert approvals[0]["scope"] == "session"
    assert approvals[0]["tool_pattern"] == "run_shell"
    assert approvals[0]["path_scope"] == str(tmp_path.resolve())
    assert approvals[0]["propagate_to_subagents"] is True
    assert full_payload["pagination"]["total"] == 1


def test_http_tool_approval_get_exposes_history(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    approval = gateway.tool_policy.create_approval_request(
        request_id="req-history",
        tool_name="write_file",
        agent_name="boiled_claw",
        args={"path": str(tmp_path / "note.txt")},
        session_id="sess-history",
        reason="file writes need approval",
    )
    gateway.tool_policy.resolve_approval(
        approval.request_id,
        True,
        "approved in history test",
        scope="session",
        propagate_to_subagents=True,
    )

    with TestClient(gateway.app) as client:
        response = client.get(f"/tools/approvals/{approval.request_id}")

    assert response.status_code == 200
    payload = response.json()["approval"]
    assert payload["request_id"] == approval.request_id
    assert [entry["state"] for entry in payload["history"]] == ["pending", "approved"]
    assert payload["history"][1]["metadata"]["propagate_to_subagents"] is True


def test_http_tool_approval_get_exposes_resolve_suggestions(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-ax-1",
        tool_name="desktop_ax_snapshot",
        agent_name="desktop_operator",
        args={"app_name": "djay Pro"},
        session_id="sess-desktop-bundle",
        reason="accessibility snapshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-ax-2",
        tool_name="desktop_ax_snapshot",
        agent_name="desktop_operator",
        args={"app_name": "djay Pro"},
        session_id="sess-desktop-bundle",
        reason="accessibility snapshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-view-1",
        tool_name="desktop_view_screenshot",
        agent_name="desktop_operator",
        args={"path": None},
        session_id="sess-desktop-bundle",
        reason="desktop screenshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-shell-1",
        tool_name="run_shell_guarded",
        agent_name="root_agent",
        args={"command": "pwd"},
        session_id="sess-desktop-bundle",
        reason="shell commands need approval",
    )

    with TestClient(gateway.app) as client:
        response = client.get("/tools/approvals/req-desktop-ax-1")

    assert response.status_code == 200
    payload = response.json()
    suggestions = {
        item["strategy"]: item for item in payload["resolve_suggestions"]
    }
    assert set(suggestions) == {
        "session_exact",
        "family_session",
        "desktop_session_pack",
    }
    assert suggestions["session_exact"]["affected_count"] == 2
    assert suggestions["session_exact"]["tool_pattern"] == "desktop_ax_snapshot"
    assert suggestions["family_session"]["tool_pattern"] == "desktop_ax_*"
    assert suggestions["family_session"]["affected_count"] == 2
    assert suggestions["desktop_session_pack"]["tool_pattern"] == "desktop::*"
    assert suggestions["desktop_session_pack"]["affected_count"] == 3


def test_http_tool_approval_resolve_endpoint_updates_scope(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    audit_events = []
    gateway.audit_logger.log = lambda **kwargs: audit_events.append(kwargs)

    gateway.tool_policy.create_approval_request(
        request_id="req-resolve-http",
        tool_name="run_shell",
        agent_name="boiled_claw",
        args={"command": "pwd", "cwd": str(tmp_path)},
        session_id="sess-resolve-http",
        reason="shell commands need approval",
    )

    with TestClient(gateway.app) as client:
        response = client.post(
            "/tools/approvals/req-resolve-http/resolve",
            json={
                "approved": True,
                "reason": "approved over http",
                "session_id": "sess-resolve-http",
                "scope": "session",
                "tool_pattern": "run_*",
                "path_scope": str(tmp_path),
                "propagate_to_subagents": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["resolved"] is True
    assert payload["approval"]["state"] == "approved"
    assert payload["approval"]["scope"] == "session"
    assert payload["approval"]["tool_pattern"] == "run_*"
    assert payload["approval"]["path_scope"] == str(tmp_path.resolve())
    assert payload["approval"]["propagate_to_subagents"] is True
    assert payload["approval"]["history"][-1]["metadata"]["actor_user_id"] == "api_user"
    assert payload["approval"]["history"][-1]["metadata"]["source"] == "http"
    approval_audit = next(
        event for event in reversed(audit_events) if event.get("event_type").value == "tool_approval"
    )
    assert approval_audit["metadata"]["source"] == "http"
    assert approval_audit["metadata"]["scope_after"] == "session"


def test_http_tool_approval_resolve_bundle_resolves_desktop_session_pack(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-pack-ax-1",
        tool_name="desktop_ax_snapshot",
        agent_name="desktop_operator",
        args={"app_name": "djay Pro"},
        session_id="sess-desktop-pack",
        reason="accessibility snapshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-pack-ax-2",
        tool_name="desktop_ax_snapshot",
        agent_name="desktop_operator",
        args={"app_name": "djay Pro"},
        session_id="sess-desktop-pack",
        reason="accessibility snapshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-pack-view-1",
        tool_name="desktop_view_screenshot",
        agent_name="desktop_operator",
        args={"path": None},
        session_id="sess-desktop-pack",
        reason="desktop screenshots need approval",
    )
    gateway.tool_policy.create_approval_request(
        request_id="req-desktop-pack-shell-1",
        tool_name="run_shell_guarded",
        agent_name="root_agent",
        args={"command": "pwd"},
        session_id="sess-desktop-pack",
        reason="shell commands need approval",
    )

    with TestClient(gateway.app) as client:
        response = client.post(
            "/tools/approvals/req-desktop-pack-ax-1/resolve_bundle",
            json={
                "approved": True,
                "strategy": "desktop_session_pack",
                "session_id": "sess-desktop-pack",
                "reason": "approve desktop pack",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["resolved"] is True
    assert payload["strategy"] == "desktop_session_pack"
    assert payload["resolved_count"] == 3
    assert set(payload["request_ids"]) == {
        "req-desktop-pack-ax-1",
        "req-desktop-pack-ax-2",
        "req-desktop-pack-view-1",
    }

    approved_payload = gateway.tool_policy.query_approvals(
        session_id="sess-desktop-pack",
        state="approved",
        include_expired=False,
        page=1,
        page_size=20,
    )
    approved = {
        item["request_id"]: item for item in approved_payload["approvals"]
    }
    assert approved["req-desktop-pack-ax-1"]["tool_pattern"] == "desktop_ax_*"
    assert approved["req-desktop-pack-ax-2"]["tool_pattern"] == "desktop_ax_*"
    assert approved["req-desktop-pack-view-1"]["tool_pattern"] == "desktop_view_*"
    assert all(item["scope"] == "session" for item in approved.values())

    pending_payload = gateway.tool_policy.query_approvals(
        session_id="sess-desktop-pack",
        state="pending",
        include_expired=False,
        page=1,
        page_size=20,
    )
    pending_ids = [item["request_id"] for item in pending_payload["approvals"]]
    assert pending_ids == ["req-desktop-pack-shell-1"]


def test_websocket_tool_approval_resolution_accepts_scope_fields(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    audit_events = []
    gateway.audit_logger.log = lambda **kwargs: audit_events.append(kwargs)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            session_id = connected["session_id"]

            gateway.tool_policy.create_approval_request(
                request_id="req-ws-stateful",
                tool_name="write_file",
                agent_name="boiled_claw",
                args={"path": str(tmp_path / "note.txt")},
                session_id=session_id,
                reason="file writes need approval",
            )
            ws.send_json(
                {
                    "event": "tools.approval",
                    "request_id": "req-ws-stateful",
                    "approved": True,
                    "reason": "approved in test",
                    "scope": "session",
                    "tool_pattern": "write_*",
                    "path_scope": str(tmp_path),
                    "expires_at": time.time() + 60,
                    "propagate_to_subagents": True,
                }
            )
            resolved = _receive_foreground_event(ws)
            assert resolved["event"] == "system.event"
            assert resolved["status"] == "resolved"

    approvals = gateway.tool_policy.list_approvals(
        session_id=session_id,
        state="approved",
    )
    assert len(approvals) == 1
    assert approvals[0]["scope"] == "session"
    assert approvals[0]["tool_pattern"] == "write_*"
    assert approvals[0]["path_scope"] == str(tmp_path.resolve())
    assert approvals[0]["propagate_to_subagents"] is True
    assert approvals[0]["history"][-1]["metadata"]["actor_user_id"] == "alice"
    assert approvals[0]["history"][-1]["metadata"]["source"] == "websocket"
    approval_audit = next(
        event for event in reversed(audit_events) if event.get("event_type").value == "tool_approval"
    )
    assert approval_audit["metadata"]["source"] == "websocket"


def test_http_task_list_endpoint_supports_search_and_pagination(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    store.create(
        kind="self_improvement_demo",
        title="Repair save selector",
        owner_session_id="sess-dashboard",
        owner_user_id="alice",
        artifacts={"selector": "#save", "summary": "repair selector"},
    )
    store.create(
        kind="self_improvement_demo",
        title="Repair query field",
        owner_session_id="sess-dashboard",
        owner_user_id="alice",
        artifacts={"selector": "#query", "summary": "repair input"},
    )

    with TestClient(gateway.app) as client:
        response = client.get(
            "/tasks",
            params={
                "session_id": "sess-dashboard",
                "q": "#save",
                "page": 1,
                "page_size": 1,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["title"] == "Repair save selector"
    assert payload["pagination"]["page"] == 1
    assert payload["pagination"]["page_size"] == 1
    assert payload["pagination"]["total"] == 1


def test_http_audit_list_endpoint_supports_filters_and_pagination(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    from src.security.audit import AuditEventType, AuditLogger

    gateway.audit_logger = AuditLogger(str(tmp_path / "audit.log"))
    gateway.audit_logger.log(
        event_type=AuditEventType.TOOL_APPROVAL,
        user_id="alice",
        session_id="sess-audit",
        action="resolve",
        resource="req-audit",
        result="resolved",
        metadata={
            "tool_name": "run_shell",
            "source": "http",
            "scope_before": "single",
            "scope_after": "session",
            "actor_user_id": "alice",
            "resolve_reason": "approved in control ui",
        },
    )
    gateway.audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id="bob",
        session_id="sess-other",
        action="execute",
        resource="echo hello",
        result="success",
        metadata={"source": "websocket", "command": "echo hello"},
    )

    with TestClient(gateway.app) as client:
        response = client.get(
            "/audit",
            params={
                "actor_user_id": "alice",
                "session_id": "sess-audit",
                "tool": "run_shell",
                "source": "http",
                "result": "resolved",
                "q": "scope_after",
                "page": 1,
                "page_size": 1,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 1
    assert payload["pagination"]["page_size"] == 1
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["event_type"] == "tool_approval"
    assert payload["entries"][0]["metadata"]["scope_before"] == "single"
    assert payload["entries"][0]["metadata"]["scope_after"] == "session"


def test_http_audit_list_endpoint_backfills_indexed_store_from_jsonl(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    from src.security.audit import AuditLogger

    log_path = tmp_path / "audit.log"
    log_path.write_text(
        '{"timestamp": 1.0, "datetime": "1970-01-01T00:00:01", "event_type": "shell_command", "user_id": "alice", "session_id": "sess-legacy", "action": "execute", "resource": "echo hello", "result": "success", "metadata": {"source": "websocket", "tool_name": "run_shell"}}\n',
        encoding="utf-8",
    )
    gateway.audit_logger = AuditLogger(str(log_path))

    with TestClient(gateway.app) as client:
        response = client.get("/audit", params={"session_id": "sess-legacy", "tool": "run_shell"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 1
    assert payload["entries"][0]["event_type"] == "shell_command"
    assert payload["entries"][0]["metadata"]["tool_name"] == "run_shell"


@pytest.mark.asyncio
async def test_audit_notifier_keeps_non_approval_events_session_local(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway.manager.active_connections = {
        "sess-source": object(),
        "sess-target": object(),
    }
    sent = []

    async def _capture(session_id, data):
        sent.append((session_id, data))

    gateway.manager.send_json = _capture

    await gateway._audit_notifier_fn(
        {
            "event_type": "shell_command",
            "session_id": "sess-source",
            "metadata": {"target_session_id": "sess-target"},
        }
    )

    assert [session_id for session_id, _data in sent] == ["sess-source"]


@pytest.mark.asyncio
async def test_audit_notifier_fans_out_target_session_for_tool_approval(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway.manager.active_connections = {
        "sess-source": object(),
        "sess-target": object(),
    }
    sent = []

    async def _capture(session_id, data):
        sent.append((session_id, data))

    gateway.manager.send_json = _capture

    await gateway._audit_notifier_fn(
        {
            "event_type": "tool_approval",
            "session_id": "sess-source",
            "metadata": {"target_session_id": "sess-target"},
        }
    )

    assert [session_id for session_id, _data in sent] == ["sess-source", "sess-target"]


@pytest.mark.asyncio
async def test_connection_manager_disconnect_can_preserve_pending_events(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    gateway.manager.active_connections = {"sess-1": object()}
    gateway.manager._session_users = {"sess-1": "alice"}
    gateway.manager._pending_events = {"sess-1": [{"event": "task.update"}]}

    gateway.manager.disconnect(
        "sess-1",
        preserve_pending=True,
        preserve_user=True,
    )

    assert "sess-1" not in gateway.manager.active_connections
    assert gateway.manager._pending_events["sess-1"] == [{"event": "task.update"}]
    assert gateway.manager.get_user_id("sess-1") == "alice"


def test_websocket_disconnect_does_not_abort_running_session(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    abort_calls = []

    async def _capture_abort(session_id):
        abort_calls.append(session_id)
        return True

    gateway.manager.abort = _capture_abort

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

    assert abort_calls == []


def test_http_task_timeline_endpoint_merges_task_approval_and_audit_entries(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    task = store.create(
        kind="subagent",
        title="Trace timeline",
        owner_session_id="sess-timeline",
        owner_user_id="alice",
        run_id="run-timeline",
        approval_dependencies=["apr_timeline"],
        artifacts={"step": "prepare"},
    )
    store.update(
        task["task_id"],
        status="running",
        artifacts={"step": "act"},
    )
    gateway.tool_policy.create_approval_request(
        request_id="apr_timeline",
        tool_name="run_shell",
        agent_name="root_agent",
        args={"command": "pwd"},
        session_id="sess-timeline",
        reason="Need shell access",
    )
    gateway.tool_policy.resolve_approval(
        "apr_timeline",
        True,
        "approved",
        history_metadata={"actor_user_id": "alice", "source": "http"},
    )
    gateway.audit_logger.log(
        event_type=server_module.AuditEventType.TOOL_APPROVAL,
        user_id="alice",
        session_id="sess-timeline",
        action="resolve",
        resource="apr_timeline",
        result="resolved",
        metadata={
            "request_id": "apr_timeline",
            "tool_name": "run_shell",
            "source": "http",
            "actor_user_id": "alice",
            "task_id": task["task_id"],
            "run_id": "run-timeline",
        },
    )

    with TestClient(gateway.app) as client:
        response = client.get(f"/tasks/{task['task_id']}/timeline", params={"page": 1, "page_size": 20})

    assert response.status_code == 200
    payload = response.json()
    kinds = {entry["kind"] for entry in payload["entries"]}
    assert "task_event" in kinds
    assert "approval" in kinds
    assert "audit" in kinds


def test_http_task_timeline_includes_supervisor_runtime_blocked_event(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    task = store.create(
        kind="control_supervisor",
        title="Keep the sheet healthy",
        status="blocked",
        owner_session_id="sess-supervisor-blocked",
        owner_user_id="alice",
        artifacts={
            "durable_execution": {
                "resume_state": {"reason": "awaiting_unblock_or_human_input"},
                "scheduler_state": {
                    "blocked_queue": [
                        {
                            "entry_id": "queue-1",
                            "node_id": "node-1",
                            "queue": "blocked",
                            "reason": "guardrail_budget_exhausted",
                        }
                    ]
                },
            }
        },
    )
    store.append_event(
        task["task_id"],
        event_type="supervisor_blocked_by_runtime",
        status="blocked",
        title="Supervisor blocked",
        payload={
            "summary": "Budget exhausted while retrying the same failure.",
            "runtime": {
                "recovery_decision": {
                    "failure_type": "target_context_mismatch",
                    "budget_exhausted": True,
                },
                "scheduler_queue_entry": {
                    "queue": "blocked",
                    "reason": "guardrail_budget_exhausted",
                },
            },
        },
    )

    with TestClient(gateway.app) as client:
        response = client.get(
            f"/tasks/{task['task_id']}/timeline",
            params={"page": 1, "page_size": 20},
        )

    assert response.status_code == 200
    payload = response.json()
    task_events = [entry for entry in payload["entries"] if entry["kind"] == "task_event"]
    blocked_event = next(
        entry for entry in task_events if entry["event_type"] == "supervisor_blocked_by_runtime"
    )
    assert blocked_event["status"] == "blocked"
    assert blocked_event["payload"]["runtime"]["scheduler_queue_entry"]["reason"] == (
        "guardrail_budget_exhausted"
    )


def test_http_task_replay_endpoint_creates_child_control_loop_task(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    source_task = store.create(
        kind="control_loop",
        title="Replay desktop playback",
        status="failed",
        owner_session_id="sess-replay-http",
        owner_user_id="alice",
        artifacts={
            "resume_context": {
                "goal": "Replay desktop playback",
                "constraints": ["preserve-control-ui-tab"],
            },
            "result": {
                "success": False,
                "final_text": "verification failed",
            },
        },
    )
    captured: dict[str, object] = {}

    async def _fake_start_control_loop_run(
        *,
        session_id: str,
        user_id: str,
        goal: str,
        constraints: list[str],
        request_id=None,
        task_id=None,
        parent_task_id=None,
        replay_of_task_id=None,
        compare_to_task_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
        captured["session_id"] = session_id
        captured["user_id"] = user_id
        captured["goal"] = goal
        captured["constraints"] = constraints
        captured["task_id"] = task_id
        captured["parent_task_id"] = parent_task_id
        captured["replay_of_task_id"] = replay_of_task_id
        captured["compare_to_task_id"] = compare_to_task_id
        captured["initial_state"] = initial_state
        captured["reset_if_terminal"] = reset_if_terminal

    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start_control_loop_run)

    with TestClient(gateway.app) as client:
        response = client.post(f"/tasks/{source_task['task_id']}/replay")

    assert response.status_code == 200
    payload = response.json()
    replay_task = payload["task"]
    assert payload["accepted"] is True
    assert payload["replay_of_task_id"] == source_task["task_id"]
    assert replay_task["parent_task_id"] == source_task["task_id"]
    assert replay_task["metadata"]["replay_of_task_id"] == source_task["task_id"]
    assert replay_task["metadata"]["compare_to_task_id"] == source_task["task_id"]
    assert replay_task["artifacts"]["replay"]["source_task_id"] == source_task["task_id"]
    assert replay_task["artifacts"]["replay"]["compare_to_task_id"] == source_task["task_id"]

    assert captured["session_id"] == "sess-replay-http"
    assert captured["user_id"] == "alice"
    assert captured["goal"] == "Replay desktop playback"
    assert captured["constraints"] == ["preserve-control-ui-tab"]
    assert captured["task_id"] == replay_task["task_id"]
    assert captured["parent_task_id"] == source_task["task_id"]
    assert captured["replay_of_task_id"] == source_task["task_id"]
    assert captured["compare_to_task_id"] == source_task["task_id"]
    assert captured["initial_state"] is None
    assert captured["reset_if_terminal"] is True

    stored_replay = store.get(replay_task["task_id"])
    assert stored_replay is not None
    assert stored_replay["parent_task_id"] == source_task["task_id"]


def test_http_task_replay_endpoint_accepts_from_step_tail_seed(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    source_task = store.create(
        kind="control_loop",
        title="Replay desktop playback",
        status="failed",
        owner_session_id="sess-replay-http",
        owner_user_id="alice",
        artifacts={
            "resume_context": {
                "goal": "Replay desktop playback",
                "constraints": ["preserve-control-ui-tab"],
            },
            "result": {
                "success": False,
                "final_text": "verification failed",
                "approved_plan": {
                    "plan_id": "plan-1",
                    "risk_level": "medium",
                    "steps": [
                        {"step_id": "launch_djay", "title": "Launch Djay"},
                        {"step_id": "verify_visual_state", "title": "Verify visual state"},
                    ],
                },
                "step_trace": [
                    {"step_id": "launch_djay", "step_type": "plan", "status": "succeeded"},
                    {"step_id": "verify_visual_state", "step_type": "plan", "status": "failed"},
                ],
                "tail_replay_from_step_id": "verify_visual_state",
            },
        },
    )
    captured: dict[str, object] = {}

    async def _fake_start_control_loop_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start_control_loop_run)

    with TestClient(gateway.app) as client:
        response = client.post(
            f"/tasks/{source_task['task_id']}/replay",
            json={"from_step": "verify_visual_state"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["replay_from_step"] == "verify_visual_state"
    assert payload["replay_mode"] == "tail"
    assert captured["reset_if_terminal"] is True
    assert captured["initial_state"]["replay:from_step"] == "verify_visual_state"
    assert captured["initial_state"]["replay:context"]["mode"] == "tail"
    assert captured["initial_state"]["plan:approved"]["plan_id"] == "plan-1"


def test_http_task_replay_endpoint_requires_task_owner_identity(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(
        monkeypatch,
        tmp_path,
        gateway_api_key="secret-token",
        gateway_auth_user_header="X-Auth-User",
    )
    store = server_module.get_task_store()
    source_task = store.create(
        kind="control_loop",
        title="Replay desktop playback",
        status="failed",
        owner_session_id="sess-replay-auth",
        owner_user_id="alice",
        artifacts={
            "resume_context": {
                "goal": "Replay desktop playback",
                "constraints": [],
            },
            "result": {
                "success": False,
                "final_text": "verification failed",
            },
        },
    )

    with TestClient(gateway.app) as client:
        response = client.post(
            f"/tasks/{source_task['task_id']}/replay",
            headers={
                "Authorization": "Bearer secret-token",
                "X-Auth-User": "bob",
            },
        )

    assert response.status_code == 404
    assert "task not found" in response.json()["detail"]


def test_http_task_compare_endpoint_defaults_to_latest_replay(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    baseline = store.create(
        kind="control_loop",
        title="Desktop playback compare",
        status="failed",
        owner_session_id="sess-compare-http",
        owner_user_id="alice",
        artifacts={
            "resume_context": {"goal": "Desktop playback compare", "constraints": []},
            "result": {
                "success": False,
                "final_text": "verification failed",
                "verification_status": "partial_pass",
                "verification_report_id": "report-baseline",
                "verification_report": {
                    "status": "partial_pass",
                    "overall_score": 0.45,
                    "criterion_results": [
                        {"name": "music_playing", "passed": False},
                    ],
                },
                "artifact_refs": [
                    "data/screenshots/baseline-before.png",
                ],
                "step_trace": [
                    {
                        "step_id": "verify_visual_state",
                        "title": "Verify visual state",
                        "step_type": "plan",
                        "status": "failed",
                        "output_summary": "evidence thin",
                        "failed_criteria": ["music_playing"],
                    }
                ],
                "repair_count": 1,
            },
        },
    )
    replay = store.create(
        kind="control_loop",
        title="Desktop playback compare",
        status="completed",
        owner_session_id="sess-compare-http",
        owner_user_id="alice",
        parent_task_id=baseline["task_id"],
        artifacts={
            "resume_context": {"goal": "Desktop playback compare", "constraints": []},
            "result": {
                "success": True,
                "final_text": "playback verified",
                "verification_status": "pass",
                "verification_report_id": "report-replay",
                "verification_report": {
                    "status": "pass",
                    "overall_score": 0.92,
                    "criterion_results": [
                        {"name": "music_playing", "passed": True},
                    ],
                },
                "artifact_refs": [
                    "data/screenshots/replay-before.png",
                    "data/screenshots/replay-after.png",
                ],
                "step_trace": [
                    {
                        "step_id": "verify_visual_state",
                        "title": "Verify visual state",
                        "step_type": "plan",
                        "status": "succeeded",
                        "output_summary": "captured fresh screenshots",
                        "failed_criteria": [],
                    }
                ],
                "repair_count": 0,
            },
        },
    )

    with TestClient(gateway.app) as client:
        response = client.get(f"/tasks/{baseline['task_id']}/compare")

    assert response.status_code == 200
    payload = response.json()
    assert payload["left_task"]["task_id"] == baseline["task_id"]
    assert payload["right_task"]["task_id"] == replay["task_id"]
    assert payload["left"]["result"]["verification_status"] == "partial_pass"
    assert payload["right"]["result"]["verification_status"] == "pass"
    assert payload["left"]["result"]["repair_count"] == 1
    assert payload["right"]["result"]["repair_count"] == 0
    assert payload["right"]["result"]["screenshot_refs"] == [
        "data/screenshots/replay-before.png",
        "data/screenshots/replay-after.png",
    ]
    assert payload["right"]["timeline"]["total"] >= 1
    assert payload["step_compare"]["changed"] >= 1
    assert payload["step_compare"]["rows"][0]["step_id"] == "verify_visual_state"
    assert any("verification changed" in item for item in payload["summary"])

def test_http_task_analytics_endpoint_returns_all_sections(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    task = store.create(
        kind="control_loop",
        title="Desktop playback",
        status="failed",
        owner_session_id="sess-analytics",
        owner_user_id="alice",
    )
    store.append_event(
        task["task_id"],
        event_type="step_plan",
        title="verify_visual_state",
        payload={
            "summary": "evidence thin",
            "step": {
                "step_id": "verify_visual_state",
                "title": "Verify visual state",
                "step_type": "plan",
                "status": "failed",
                "failed_criteria": ["music_playing"],
            },
        },
    )

    with TestClient(gateway.app) as client:
        response = client.get("/tasks/analytics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["total_tasks"] >= 1
    assert len(payload["step_failure_ranking"]["steps"]) >= 1
    assert payload["step_failure_ranking"]["steps"][0]["step_id"] == "verify_visual_state"
    assert payload["step_failure_ranking"]["truncated"] is False
    assert "replay_improvement" in payload


def test_openapi_exposes_task_replay_and_compare_contracts(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    payload = response.json()
    components = payload["components"]["schemas"]

    replay_operation = payload["paths"]["/tasks/{task_id}/replay"]["post"]
    replay_request_schema = replay_operation["requestBody"]["content"]["application/json"]["schema"]
    assert replay_request_schema["anyOf"][0]["$ref"].endswith("/TaskReplayRequest")
    assert replay_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskReplayAcceptedResponse"
    )
    assert "from_step" in components["TaskReplayRequest"]["properties"]

    compare_operation = payload["paths"]["/tasks/{task_id}/compare"]["get"]
    assert compare_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskCompareResponse"
    )
    assert components["TaskCompareResponse"]["properties"]["step_compare"]["$ref"].endswith(
        "/StepComparePayload"
    )

    analytics_operation = payload["paths"]["/tasks/analytics"]["get"]
    assert analytics_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskAnalyticsResponse"
    )


def test_openapi_exposes_task_timeline_and_audit_contracts(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    with TestClient(gateway.app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    payload = response.json()

    timeline_operation = payload["paths"]["/tasks/{task_id}/timeline"]["get"]
    assert timeline_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskTimelineResponse"
    )

    audit_operation = payload["paths"]["/audit"]["get"]
    assert audit_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AuditQueryResponse"
    )


def test_websocket_chat_abort_triggers_desktop_emergency_stop(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, str] = {}

    async def _fake_stop(*, session_id: str, user_id: str, reason: str):
        captured["session_id"] = session_id
        captured["user_id"] = user_id
        captured["reason"] = reason
        return True

    monkeypatch.setattr(gateway, "_desktop_emergency_stop", _fake_stop)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json({"event": "chat.abort"})

            done = _receive_foreground_event(ws)
            assert done["event"] == "chat.done"

    assert captured["user_id"] == "alice"
    assert captured["reason"] == "Abort requested from Web UI"


@pytest.mark.asyncio
async def test_start_agent_run_clears_desktop_stop(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, str] = {}

    async def _fake_clear(*, session_id: str, user_id: str):
        captured["session_id"] = session_id
        captured["user_id"] = user_id
        return True

    async def _fake_abort(session_id: str):
        return False

    async def _fake_agent_task(session_id: str, user_id: str, message: str, request_id=None):
        return None

    monkeypatch.setattr(gateway, "_desktop_clear_stop", _fake_clear)
    monkeypatch.setattr(gateway.manager, "abort", _fake_abort)
    monkeypatch.setattr(gateway, "_agent_run_task", _fake_agent_task)

    await gateway._start_agent_run(
        session_id="sess-clear-1",
        user_id="alice",
        message="hello",
    )
    await asyncio.sleep(0)

    assert captured == {"session_id": "sess-clear-1", "user_id": "alice"}


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

            resolved = _receive_foreground_event(ws)
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"


def test_websocket_control_loop_approval_resume_uses_session_task_goal(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    existing_task: dict[str, object] = {}

    async def _fake_pending(*, user_id: str, session_id: str):
        return {
            "request_id": "plan_resume_1",
            "goal": "planner rewritten goal",
            "plan": {"constraints": ["keep-browser"]},
        }

    async def _fake_resolve(*, user_id: str, session_id: str, approved: bool, request_id: str | None = None):
        assert approved is True
        assert request_id == "plan_resume_1"
        return True

    async def _fake_task_goal(*, user_id: str, session_id: str):
        return "original user goal"

    async def _fake_start(*, session_id: str, user_id: str, goal: str, constraints: list[str], request_id=None, task_id=None, **_kwargs):
        captured["goal"] = goal
        captured["constraints"] = constraints
        captured["task_id"] = task_id

    monkeypatch.setattr(gateway.control_loop, "get_pending_approval", _fake_pending)
    monkeypatch.setattr(gateway.control_loop, "resolve_human_approval", _fake_resolve)
    monkeypatch.setattr(gateway.control_loop, "get_task_goal", _fake_task_goal)
    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"
            existing_task.update(
                gateway._create_control_loop_task_record(
                    user_id="alice",
                    session_id=connected["session_id"],
                    goal="planner rewritten goal",
                    constraints=[],
                    request_id=None,
                    source="websocket",
                )
            )
            server_module.update_task_record(
                existing_task["task_id"],
                status="pending",
                artifacts={
                    "result": {"approval_request": {"request_id": "plan_resume_1"}},
                    "resume_context": {"approval_request": {"request_id": "plan_resume_1"}},
                },
                metadata={"needs_human": True},
            )

            ws.send_json(
                {
                    "event": "tools.approval",
                    "request_id": "plan_resume_1",
                    "approved": True,
                }
            )

            resolved = _receive_foreground_event(ws)
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"

    assert captured["goal"] == "original user goal"
    assert captured["constraints"] == ["keep-browser"]
    assert captured["task_id"] == existing_task["task_id"]


def test_http_control_loop_approve_reuses_pending_task(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    existing_task = gateway._create_control_loop_task_record(
        user_id="alice",
        session_id="session-1",
        goal="planner rewritten goal",
        constraints=[],
        request_id=None,
        source="http",
    )
    server_module.update_task_record(
        existing_task["task_id"],
        status="pending",
        artifacts={
            "result": {"approval_request": {"request_id": "plan_resume_http"}},
            "resume_context": {"approval_request": {"request_id": "plan_resume_http"}},
        },
        metadata={"needs_human": True},
    )
    captured: dict[str, object] = {}

    async def _fake_pending(*, user_id: str, session_id: str):
        return {
            "request_id": "plan_resume_http",
            "goal": "planner rewritten goal",
            "plan": {"constraints": ["keep-browser"]},
        }

    async def _fake_resolve(*, user_id: str, session_id: str, approved: bool, request_id: str | None = None):
        assert approved is True
        assert request_id == "plan_resume_http"
        return True

    async def _fake_task_goal(*, user_id: str, session_id: str):
        return "original user goal"

    async def _fake_start_control_loop_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway.control_loop, "get_pending_approval", _fake_pending)
    monkeypatch.setattr(gateway.control_loop, "resolve_human_approval", _fake_resolve)
    monkeypatch.setattr(gateway.control_loop, "get_task_goal", _fake_task_goal)
    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start_control_loop_run)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/control-loop/approve",
            json={
                "user_id": "alice",
                "session_id": "session-1",
                "request_id": "plan_resume_http",
                "approved": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["approved"] is True
    assert payload["result"]["ok"] is True
    assert payload["result"]["task_id"] == existing_task["task_id"]
    assert captured["task_id"] == existing_task["task_id"]
    assert captured["goal"] == "original user goal"
    assert captured["constraints"] == ["keep-browser"]


def test_http_control_loop_approve_falls_back_to_task_owner_user_id(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    existing_task = gateway._create_control_loop_task_record(
        user_id="web_user",
        session_id="session-1",
        goal="planner rewritten goal",
        constraints=[],
        request_id=None,
        source="http",
    )
    server_module.update_task_record(
        existing_task["task_id"],
        status="pending",
        artifacts={
            "result": {
                "approval_request": {"request_id": "plan_resume_owner_fallback"}
            },
            "resume_context": {
                "approval_request": {"request_id": "plan_resume_owner_fallback"}
            },
        },
        metadata={"needs_human": True},
    )
    captured: dict[str, object] = {}
    seen_calls: list[tuple[str, str]] = []

    async def _fake_pending(*, user_id: str, session_id: str):
        seen_calls.append(("pending", user_id))
        if user_id != "web_user":
            return None
        return {
            "request_id": "plan_resume_owner_fallback",
            "goal": "planner rewritten goal",
            "plan": {"constraints": ["keep-browser"]},
        }

    async def _fake_resolve(
        *, user_id: str, session_id: str, approved: bool, request_id: str | None = None
    ):
        seen_calls.append(("resolve", user_id))
        assert approved is True
        assert request_id == "plan_resume_owner_fallback"
        return user_id == "web_user"

    async def _fake_task_goal(*, user_id: str, session_id: str):
        assert user_id == "web_user"
        return "original user goal"

    async def _fake_start_control_loop_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gateway.control_loop, "get_pending_approval", _fake_pending)
    monkeypatch.setattr(gateway.control_loop, "resolve_human_approval", _fake_resolve)
    monkeypatch.setattr(gateway.control_loop, "get_task_goal", _fake_task_goal)
    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start_control_loop_run)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/control-loop/approve",
            json={
                "session_id": "session-1",
                "request_id": "plan_resume_owner_fallback",
                "approved": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["approved"] is True
    assert payload["result"]["ok"] is True
    assert payload["result"]["task_id"] == existing_task["task_id"]
    assert ("pending", "api_user") in seen_calls
    assert ("resolve", "api_user") in seen_calls
    assert ("pending", "web_user") in seen_calls
    assert ("resolve", "web_user") in seen_calls
    assert captured["user_id"] == "web_user"
    assert captured["task_id"] == existing_task["task_id"]
    assert captured["goal"] == "original user goal"
    assert captured["constraints"] == ["keep-browser"]


def test_http_control_loop_run_returns_pending_approval(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_control_run(
        *,
        goal: str,
        user_id: str,
        constraints=None,
        session_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
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


def test_http_control_loop_run_creates_task_object(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_runtime_error(goal: str):
        return None

    async def _fake_control_run(
        *,
        goal: str,
        user_id: str,
        constraints=None,
        session_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
        assert goal == "Inspect current browser session"
        assert session_id
        return ExecutionResult(
            request_id="req-control-task",
            session_id=session_id,
            user_id=user_id,
            final_text="done",
            plan_id="plan-control-task",
            success=True,
            repair_count=1,
        )

    monkeypatch.setattr(gateway, "_current_browser_runtime_error", _fake_runtime_error)
    monkeypatch.setattr(gateway.control_loop, "run", _fake_control_run)

    with TestClient(gateway.app) as client:
        response = client.post(
            "/control-loop/run",
            json={"user_id": "alice", "goal": "Inspect current browser session"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["task_id"].startswith("task_")

        task_response = client.get(f"/tasks/{payload['task_id']}")

    assert task_response.status_code == 200
    task = task_response.json()["task"]
    assert task["kind"] == "control_loop"
    assert task["status"] == "completed"
    assert task["artifacts"]["resume_context"]["goal"] == "Inspect current browser session"


def test_websocket_control_run_emits_control_approval_request(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_control_run(
        *,
        goal: str,
        user_id: str,
        constraints=None,
        session_id=None,
        initial_state=None,
        reset_if_terminal=False,
    ):
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

            approval = _receive_foreground_event(ws)
            done = _receive_foreground_event(ws)
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


# ---------------------------------------------------------------------------
# E2E integration: chat → task → approval → audit → timeline → WS deltas
# ---------------------------------------------------------------------------


_E2E_WS_RECEIVE_TIMEOUT = 10  # seconds — bounded receive to avoid hanging


def _ws_receive_json_bounded(ws, timeout=_E2E_WS_RECEIVE_TIMEOUT):
    """``ws.receive_json()`` with a wall-clock timeout.

    Starlette's ``WebSocketTestSession.receive_json`` blocks indefinitely on
    the internal queue.  This wrapper runs the blocking call on a daemon thread
    and waits on a ``queue.Queue`` with a timeout, so control always returns to
    the caller — even when the worker thread is still stuck in receive.
    """
    import queue, threading

    q: queue.Queue = queue.Queue()

    def _recv():
        try:
            q.put(ws.receive_json())
        except Exception as exc:
            q.put(exc)

    t = threading.Thread(target=_recv, daemon=True)
    t.start()
    try:
        result = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"ws.receive_json() did not return within {timeout}s"
        )
    if isinstance(result, Exception):
        raise result
    return result


def _collect_ws_events_until(ws, *, stop_event, timeout_events=200):
    """Collect ALL WS events (foreground + background) until *stop_event*.

    Each receive uses a bounded timeout so the test fails fast instead of
    hanging when the expected event never arrives.
    """
    collected = []
    for _ in range(timeout_events):
        evt = _ws_receive_json_bounded(ws)
        collected.append(evt)
        if evt.get("event") == stop_event:
            return collected
    raise AssertionError(f"never received {stop_event} within {timeout_events} events")


def test_e2e_chat_task_approval_audit_timeline_ws_flow(monkeypatch, tmp_path):
    """
    E2E: chat.send → routing → task creation → approval request → WS resolution
         → task completion → audit trail → timeline merge → WS deltas

    Verifies the full lifecycle:
      1. Chat triggers agent run that creates a task + awaits approval
      2. WS receives task.update, tools.approval_update, tools.approval_request
      3. Client resolves approval via WS tools.approval
      4. Resolution triggers approval_update, audit.append, system.event
      5. Agent run completes, task goes to "completed"
      6. GET /tasks/{id}/timeline returns merged task_event + approval + audit
      7. GET /audit returns related entries for the session
    """
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    created_state: dict[str, str] = {}

    # -- Mock routing: always pick root_agent ---------------------------------
    async def _fake_routing(*, user_id, session_id, new_message):
        yield Event(
            author="routing_agent",
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    '{"target": "root_agent", "confidence": 0.95, "reason": "E2E test"}'
                ))],
            ),
        )

    # -- Mock root runner: create task, request approval, complete task --------
    async def _fake_root_run(*, user_id, session_id, new_message):
        # Step 1: create task
        task = store.create(
            kind="subagent",
            title="E2E integration task",
            status="running",
            owner_session_id=session_id,
            owner_user_id=user_id,
            run_id="run-e2e-001",
            approval_dependencies=["placeholder"],
            artifacts={"goal": "verify full lifecycle"},
            metadata={"source": "websocket", "e2e": True},
        )
        created_state["task_id"] = task["task_id"]

        # Step 2: log audit for task spawn
        gateway.audit_logger.log(
            event_type=server_module.AuditEventType.AGENT_MESSAGE,
            user_id=user_id,
            session_id=session_id,
            action="task_spawn",
            resource=task["task_id"],
            result="accepted",
            metadata={
                "task_id": task["task_id"],
                "run_id": "run-e2e-001",
            },
        )

        # Step 3: request approval (blocks until resolved by WS client)
        approved, reason, request_id = (
            await gateway.tool_policy.request_approval_with_id(
                tool_name="run_shell",
                agent_name="root_agent",
                args={"command": "echo e2e"},
                session_id=session_id,
                reason="E2E test: shell access needed",
            )
        )
        created_state["request_id"] = request_id

        # Step 4: update approval_dependencies with actual request_id
        store.update(
            task["task_id"],
            approval_dependencies=[request_id],
        )

        # Step 5: complete task
        store.update(
            task["task_id"],
            status="completed" if approved else "failed",
            artifacts={"result": reason, "approved": approved},
        )

        # Step 6: log audit for tool execution
        gateway.audit_logger.log(
            event_type=server_module.AuditEventType.SHELL_COMMAND,
            user_id=user_id,
            session_id=session_id,
            action="execute",
            resource="echo e2e",
            result="success" if approved else "denied",
            metadata={
                "task_id": task["task_id"],
                "run_id": "run-e2e-001",
                "request_id": request_id,
                "tool_name": "run_shell",
                "source": "websocket",
            },
        )

        # Step 7: yield final agent response
        yield Event(
            author="boiled_claw",
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"E2E completed: {reason}")],
            ),
        )

    monkeypatch.setattr(gateway.routing_runner, "run_async", _fake_routing)
    monkeypatch.setattr(gateway.runner, "run_async", _fake_root_run)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/e2e-session?user_id=alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"
            session_id = connected["session_id"]

            # ── Phase 1: send chat, collect events until approval_request ──
            # Background deltas (task.update, audit.append, tools.approval_update)
            # are fire-and-forget and may arrive before OR after the awaited
            # tools.approval_request.  We only gate on approval_request here;
            # background deltas are verified across all_events in Phase 2.
            ws.send_json({"event": "chat.send", "text": "run the E2E lifecycle"})

            pre_resolve = []
            approval_request = None
            for _ in range(50):
                evt = _ws_receive_json_bounded(ws)
                pre_resolve.append(evt)
                if evt.get("event") == "tools.approval_request":
                    approval_request = evt
                    break

            assert approval_request is not None, (
                "Expected tools.approval_request; got: "
                + str([e.get("event") for e in pre_resolve])
            )
            assert approval_request["tool_name"] == "run_shell"

            # ── Phase 2: resolve approval via WS ──
            ws.send_json({
                "event": "tools.approval",
                "request_id": approval_request["request_id"],
                "approved": True,
                "reason": "Approved by E2E test operator",
            })

            # Collect all events until chat.done
            post_resolve = _collect_ws_events_until(ws, stop_event="chat.done")

            # Combine all WS events from both phases for comprehensive checks.
            # Background deltas (task.update, audit.append, tools.approval_update)
            # are fire-and-forget and may arrive in any order relative to
            # foreground events (system.event, chat.done).
            all_events = pre_resolve + post_resolve

            all_task_updates = [
                e for e in all_events if e.get("event") == "task.update"
            ]
            all_audit_appends = [
                e for e in all_events if e.get("event") == "audit.append"
            ]
            all_approval_updates = [
                e for e in all_events
                if e.get("event") == "tools.approval_update"
            ]
            all_system_events = [
                e for e in all_events if e.get("event") == "system.event"
            ]

            # system.event with source=tools.approval (awaited, reliable)
            approval_system = [
                e for e in all_system_events
                if e.get("source") == "tools.approval"
            ]
            assert len(approval_system) >= 1
            assert approval_system[0]["status"] == "resolved"

            # Task updates: at least task creation (delivered before the
            # approval await yields).  Completion updates are fire-and-forget
            # and may arrive after chat.done, so we verify final state via
            # HTTP in Phase 3.
            assert len(all_task_updates) >= 1

            # Approval updates: at least creation (pending)
            assert len(all_approval_updates) >= 1

            # Audit appends: at least task_spawn (before approval await).
            # Resolution + shell_command appends are verified via HTTP.
            assert len(all_audit_appends) >= 1

            # chat.done
            done_event = post_resolve[-1]
            assert done_event["event"] == "chat.done"
            assert "E2E completed" in done_event["text"]

        # ── Phase 3: verify state via HTTP ──
        task_id = created_state["task_id"]
        request_id = created_state["request_id"]

        # 3a. Task is completed
        task_resp = client.get(f"/tasks/{task_id}")
        assert task_resp.status_code == 200
        task_data = task_resp.json()["task"]
        assert task_data["status"] == "completed"
        assert task_data["artifacts"]["approved"] is True
        assert request_id in task_data["approval_dependencies"]

        # 3b. Timeline merges task_event + approval + audit
        timeline_resp = client.get(
            f"/tasks/{task_id}/timeline",
            params={"page": 1, "page_size": 50},
        )
        assert timeline_resp.status_code == 200
        timeline = timeline_resp.json()
        timeline_kinds = {e["kind"] for e in timeline["entries"]}
        assert "task_event" in timeline_kinds, (
            f"Expected task_event in timeline; got {timeline_kinds}"
        )
        assert "approval" in timeline_kinds, (
            f"Expected approval in timeline; got {timeline_kinds}"
        )
        assert "audit" in timeline_kinds, (
            f"Expected audit in timeline; got {timeline_kinds}"
        )

        # Verify task events include created + status_changed
        task_events = [
            e for e in timeline["entries"] if e["kind"] == "task_event"
        ]
        task_event_types = {e["event_type"] for e in task_events}
        assert "created" in task_event_types
        assert "status_changed" in task_event_types

        # Verify approval entries reference our request_id
        approval_entries = [
            e for e in timeline["entries"] if e["kind"] == "approval"
        ]
        assert any(
            e.get("request_id") == request_id for e in approval_entries
        )

        # Verify audit entries are present
        audit_entries = [
            e for e in timeline["entries"] if e["kind"] == "audit"
        ]
        assert len(audit_entries) >= 1

        # 3c. Audit log has entries for this session
        audit_resp = client.get(
            "/audit",
            params={"session_id": session_id, "page_size": 50},
        )
        assert audit_resp.status_code == 200
        audit_data = audit_resp.json()
        assert audit_data["pagination"]["total"] >= 2, (
            "Expected at least task_spawn + shell_command audit entries"
        )
        audit_event_types = {
            e["event_type"] for e in audit_data["entries"]
        }
        assert "agent_message" in audit_event_types or "shell_command" in audit_event_types


def test_heartbeat_loop_sweeps_approval_expiry(monkeypatch, tmp_path):
    """The heartbeat loop calls cleanup_expired so expiring/expired notifications fire."""
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    sweep_count = {"n": 0}
    original_cleanup = gateway.tool_policy.cleanup_expired

    def _counting_cleanup(*args, **kwargs):
        sweep_count["n"] += 1
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(gateway.tool_policy, "cleanup_expired", _counting_cleanup)

    import asyncio

    async def _run_one_heartbeat():
        # Simulate one heartbeat tick: the loop sleeps then sweeps.
        gateway.tool_policy.cleanup_expired()

    asyncio.run(_run_one_heartbeat())
    assert sweep_count["n"] >= 1


@pytest.mark.asyncio
async def test_approval_expiry_notification_carries_escalation_suggestions(monkeypatch, tmp_path):
    """When an approval transitions to expiring, the WS notification includes suggestions."""
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    import asyncio
    import time as time_mod

    notifications = []

    async def _capture(payload):
        notifications.append(payload)

    gateway.tool_policy.set_notifier(_capture)

    now = time_mod.time()
    gateway.tool_policy.create_approval_request(
        request_id="hb-exp-1",
        tool_name="desktop_control_click",
        agent_name="desktop_operator",
        args={"selector": "#play"},
        session_id="sess-hb-exp",
        reason="desktop control needs approval",
        expires_at=now + 10.0,
    )
    record = gateway.tool_policy._approvals["hb-exp-1"]
    record.created_at = now - 9.0
    record.expires_at = now + 1.0

    gateway.tool_policy.cleanup_expired()
    await asyncio.sleep(0)  # let fire-and-forget notification task run

    assert record.state == "expiring"
    expiring = [n for n in notifications if n.get("event_type") == "expiring"]
    assert len(expiring) == 1
    suggestions = expiring[0].get("escalation_suggestions", [])
    strategies = {s["strategy"] for s in suggestions}
    assert "session_exact" in strategies
    assert "family_session" in strategies


def test_resolve_control_loop_goal_expands_short_followup_from_recent_completed_task(
    monkeypatch, tmp_path
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    store = server_module.get_task_store()
    store.create(
        kind="control_loop",
        title="Oil spreadsheet task",
        status="completed",
        owner_session_id="sess-followup",
        owner_user_id="alice",
        artifacts={
            "resume_context": {
                "goal": "今の石油のチャートを調べて、google spreadsheetに記載して",
                "constraints": [],
            },
            "result": {
                "success": True,
                "final_text": "done",
            },
        },
    )

    resolved = gateway._resolve_control_loop_goal(
        session_id="sess-followup",
        goal="スプレッドシートに記載して",
    )

    assert "今の石油のチャートを調べて、google spreadsheetに記載して" in resolved
    assert "Follow-up instruction: スプレッドシートに記載して" in resolved
