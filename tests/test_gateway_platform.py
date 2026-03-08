import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.gateway.server as server_module
import src.gateway.transcript as transcript_module
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
