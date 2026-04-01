import asyncio
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types
import pytest

from src.control_loop.root_workflow import ExecutionResult
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

    async def _unexpected_control_loop_run(*args, **kwargs):
        raise AssertionError("control loop should not execute without current-browser runtime")

    monkeypatch.setattr(gateway.control_loop, "run", _unexpected_control_loop_run)

    result = await gateway._run_control_loop_http(
        user_id="alice",
        session_id="sess-browser",
        goal="私が開いているブラウザのスプレッドシートに GTC の予想を書いて",
        constraints=[],
    )

    assert result.success is False
    assert "現在開いているブラウザや既存のスプレッドシートは操作できませんでした" in result.final_text
    assert "Desktop Bridge が無効です" in result.final_text


@pytest.mark.asyncio
async def test_run_control_loop_http_adds_current_browser_constraints(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(*, goal, user_id, constraints, session_id):
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
async def test_control_loop_task_preserves_control_ui_tab_for_current_browser(
    monkeypatch,
    tmp_path,
):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_current_browser_runtime_error(_goal: str):
        return None

    async def _fake_control_loop_run(*, goal, user_id, constraints, session_id):
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

            route_selected = ws.receive_json()
            route_forwarded = ws.receive_json()
            done = ws.receive_json()

            assert route_selected["event"] == "system.event"
            assert route_selected["source"] == "router"
            assert route_selected["agent_name"] == "desktop_operator"
            assert route_forwarded["event"] == "system.event"
            assert route_forwarded["agent_name"] == "root_agent"
            assert done["event"] == "chat.done"
            assert done["text"] == "desktop answer"


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

            route_selected = ws.receive_json()
            done = ws.receive_json()

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
            resolved = ws.receive_json()
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
            resolved = ws.receive_json()
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

            done = ws.receive_json()
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

            resolved = ws.receive_json()
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"


def test_websocket_control_loop_approval_resume_uses_session_task_goal(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

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

    async def _fake_start(*, session_id: str, user_id: str, goal: str, constraints: list[str], request_id=None):
        captured["goal"] = goal
        captured["constraints"] = constraints

    monkeypatch.setattr(gateway.control_loop, "get_pending_approval", _fake_pending)
    monkeypatch.setattr(gateway.control_loop, "resolve_human_approval", _fake_resolve)
    monkeypatch.setattr(gateway.control_loop, "get_task_goal", _fake_task_goal)
    monkeypatch.setattr(gateway, "_start_control_loop_run", _fake_start)

    with TestClient(gateway.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            ws.send_json(
                {
                    "event": "tools.approval",
                    "request_id": "plan_resume_1",
                    "approved": True,
                }
            )

            resolved = ws.receive_json()
            assert resolved["event"] == "system.event"
            assert resolved["source"] == "tools.approval"
            assert resolved["status"] == "resolved"

    assert captured["goal"] == "original user goal"
    assert captured["constraints"] == ["keep-browser"]


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


def test_http_control_loop_run_creates_task_object(monkeypatch, tmp_path):
    gateway, _scheduler = _build_gateway(monkeypatch, tmp_path)

    async def _fake_runtime_error(goal: str):
        return None

    async def _fake_control_run(*, goal: str, user_id: str, constraints=None, session_id=None):
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
