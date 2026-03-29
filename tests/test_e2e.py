"""
E2E smoke tests against a running boiled-claw gateway.

前提条件:
  - `docker compose up -d --build boiled-claw-gateway` で Gateway が起動していること
  - GOOGLE_API_KEY が設定されていること

実行:
  docker compose --profile dev run --rm boiled-claw-dev pytest tests/test_e2e.py -v -m e2e

スキップ条件:
  GATEWAY_URL に接続できない場合はすべてスキップ。
"""

import asyncio
import json
import os
import pytest
import httpx
import websockets

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:18789")
WS_URL = GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://")
TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# pytest fixtures / helpers
# ---------------------------------------------------------------------------

def _is_gateway_up() -> bool:
    try:
        r = httpx.get(f"{GATEWAY_URL}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def require_gateway():
    if not _is_gateway_up():
        pytest.skip("Gateway not running — skipping e2e tests")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=GATEWAY_URL, timeout=TIMEOUT) as c:
        yield c


async def _ws_collect(url: str, send: dict, *, collect_event: str, timeout: float = 30.0) -> dict:
    """WS 接続して send を送り、collect_event を受信するまで待つ。"""
    async with websockets.connect(url) as ws:
        # connected イベントを受け取る
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        connected = json.loads(raw)
        assert connected.get("event") == "connected"

        await ws.send(json.dumps(send))

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            payload = json.loads(raw)
            if payload.get("event") == collect_event:
                return payload
        raise TimeoutError(f"Did not receive '{collect_event}' within {timeout}s")


async def _ws_run_with_auto_approvals(
    url: str,
    send: dict,
    *,
    timeout: float = 60.0,
) -> tuple[dict, list[dict]]:
    """WS 接続して send を送り、approval を自動で返しつつ chat.done まで収集する。"""
    events: list[dict] = []
    async with websockets.connect(url) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        connected = json.loads(raw)
        assert connected.get("event") == "connected"
        events.append(connected)

        await ws.send(json.dumps(send))

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            payload = json.loads(raw)
            events.append(payload)
            event = payload.get("event")
            if event in {"tools.approval_request", "control.approval_request"}:
                await ws.send(
                    json.dumps(
                        {
                            "event": "tools.approval",
                            "request_id": payload["request_id"],
                            "approved": True,
                        }
                    )
                )
                continue
            if event == "chat.done":
                return payload, events
    raise TimeoutError(f"Did not receive 'chat.done' within {timeout}s")


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------

class TestHttpApi:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_root_includes_protocol_version(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["protocol_version"] >= 1
        assert data["version"] == "0.3.0"

    def test_protocol_endpoint(self, client):
        """GET /protocol returns JSON schemas for all events."""
        r = client.get("/protocol")
        assert r.status_code == 200
        data = r.json()
        assert data["version"] >= 1
        assert "chat.send" in data["events"]
        assert "connected" in data["events"]
        assert "tools.approval_request" in data["events"]
        assert isinstance(data["schemas"], dict)

    def test_basic_response(self, client):
        """Case 1: 基本応答"""
        r = client.post("/agent/run", json={
            "user_id": "e2e",
            "message": "こんにちは。一言で自己紹介して"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["session_id"]
        assert len(data["response"]) > 0

    def test_session_continuity(self, client):
        """Case 2: セッション継続"""
        r1 = client.post("/agent/run", json={
            "user_id": "e2e_sess",
            "message": "私の名前はテスト太郎です。覚えておいてください。"
        })
        assert r1.status_code == 200
        session_id = r1.json()["session_id"]

        r2 = client.post("/agent/run", json={
            "user_id": "e2e_sess",
            "session_id": session_id,
            "message": "さっき私が名乗った名前を教えて"
        })
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ok"] is True
        assert "テスト太郎" in data2["response"]

    def test_stock_price_shortcut(self, client):
        """Case 3: 株価ショートカット（is_direct_stock_price_query=True）"""
        r = client.post("/agent/run", json={
            "user_id": "e2e",
            "message": "NVIDIAの株価"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        # OHLC フィールドが含まれること
        assert any(kw in data["response"] for kw in ["始値", "終値", "NVDA", "nvidia"])

    def test_sessions_endpoint(self, client):
        """セッション一覧が取れること"""
        r = client.get("/sessions/e2e")
        assert r.status_code == 200
        assert "sessions" in r.json()


# ---------------------------------------------------------------------------
# Transcript / History API tests
# ---------------------------------------------------------------------------

class TestTranscriptApi:
    def test_transcript_recorded_on_agent_run(self, client):
        """POST /agent/run should record both user and assistant in transcript."""
        r = client.post("/agent/run", json={
            "user_id": "e2e_transcript",
            "message": "transcriptテスト: 1+1="
        })
        assert r.status_code == 200
        session_id = r.json()["session_id"]

        # Fetch history
        r2 = client.get(f"/sessions/e2e_transcript/{session_id}/history")
        assert r2.status_code == 200
        data = r2.json()
        assert data["session_id"] == session_id
        entries = data["entries"]
        assert len(entries) >= 2

        roles = [e["role"] for e in entries]
        assert "user" in roles
        assert "assistant" in roles

        # User message should contain our test text
        user_entries = [e for e in entries if e["role"] == "user"]
        assert any("transcriptテスト" in e["content"] for e in user_entries)

    def test_transcript_sessions_list(self, client):
        """GET /transcript/sessions returns session summaries."""
        r = client.get("/transcript/sessions?user_id=e2e_user")
        assert r.status_code == 200
        data = r.json()
        assert "sessions" in data

    def test_history_limit_and_pagination(self, client):
        """History endpoint respects limit parameter."""
        # First create a session with messages
        r = client.post("/agent/run", json={
            "user_id": "e2e_hist",
            "message": "historyテスト"
        })
        session_id = r.json()["session_id"]

        r2 = client.get(f"/sessions/e2e_hist/{session_id}/history?limit=1")
        assert r2.status_code == 200
        assert len(r2.json()["entries"]) <= 1


# ---------------------------------------------------------------------------
# Cron API tests (platform features)
# ---------------------------------------------------------------------------

class TestCronApi:
    def test_cron_list_empty_or_ok(self, client):
        r = client.get("/cron")
        assert r.status_code == 200
        assert "jobs" in r.json()

    def test_cron_add_and_delete(self, client):
        """ジョブ追加 → 一覧確認 → 削除"""
        r = client.post("/cron", json={
            "name": "e2e-test-job",
            "cron_expr": "0 9 * * *",
            "task": "e2e テストジョブです",
            "agent_id": "web_researcher",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        job_id = data["job"]["id"]
        assert data["job"]["name"] == "e2e-test-job"
        assert data["job"]["cron_expr"] == "0 9 * * *"
        assert data["job"]["next_run"] is not None

        # 一覧に含まれる
        r2 = client.get("/cron")
        ids = [j["id"] for j in r2.json()["jobs"]]
        assert job_id in ids

        # 削除
        r3 = client.delete(f"/cron/{job_id}")
        assert r3.status_code == 200
        assert r3.json()["ok"] is True

        # 削除後は一覧に含まれない
        r4 = client.get("/cron")
        ids_after = [j["id"] for j in r4.json()["jobs"]]
        assert job_id not in ids_after

    def test_cron_invalid_expr(self, client):
        """不正な cron 式はエラー"""
        r = client.post("/cron", json={
            "name": "bad",
            "cron_expr": "not-a-cron",
            "task": "test",
        })
        assert r.status_code == 400

    def test_cron_toggle(self, client):
        """enable / disable が動作すること"""
        r = client.post("/cron", json={
            "name": "toggle-test",
            "cron_expr": "*/10 * * * *",
            "task": "toggle test",
        })
        job_id = r.json()["job"]["id"]

        r2 = client.patch(f"/cron/{job_id}", json={"enabled": False})
        assert r2.status_code == 200
        assert r2.json()["job"]["enabled"] is False

        # 後片付け
        client.delete(f"/cron/{job_id}")

    def test_cron_platform_fields(self, client):
        """Platform fields: delivery_target, max_retries, system_event."""
        r = client.post("/cron", json={
            "name": "platform-test",
            "cron_expr": "0 0 * * *",
            "task": "platform test",
            "delivery_target": "main",
            "session_id": "sess-platform",
            "max_retries": 3,
            "retry_delay": 60,
        })
        assert r.status_code == 200
        job = r.json()["job"]
        assert job["delivery_target"] == "session:sess-platform"
        assert job["max_retries"] == 3
        assert job["retry_delay"] == 60

        client.delete(f"/cron/{job['id']}")

    def test_cron_system_event_job(self, client):
        """System event triggered job creation."""
        r = client.post("/cron", json={
            "name": "on-connect-test",
            "cron_expr": "",
            "task": "greet user on connect",
            "system_event": "connect",
        })
        assert r.status_code == 200
        job = r.json()["job"]
        assert job["system_event"] == "connect"

        client.delete(f"/cron/{job['id']}")

    def test_cron_invalid_system_event(self, client):
        """Invalid system_event should be rejected."""
        r = client.post("/cron", json={
            "name": "bad-event",
            "cron_expr": "",
            "task": "test",
            "system_event": "invalid_event",
        })
        assert r.status_code == 400

    def test_cron_invalid_delivery_target(self, client):
        """Invalid delivery_target should be rejected."""
        r = client.post("/cron", json={
            "name": "bad-target",
            "cron_expr": "0 0 * * *",
            "task": "test",
            "delivery_target": "invalid_target",
        })
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Tool Policy API tests
# ---------------------------------------------------------------------------

class TestToolPolicyApi:
    def test_policy_list(self, client):
        """GET /tools/policy returns policy structure."""
        r = client.get("/tools/policy")
        assert r.status_code == 200
        data = r.json()
        assert "default" in data
        assert "agents" in data
        assert "rules" in data["default"]
        assert "fallback" in data["default"]

    def test_approvals_list_empty(self, client):
        """GET /tools/approvals returns empty list initially."""
        r = client.get("/tools/approvals")
        assert r.status_code == 200
        data = r.json()
        assert "approvals" in data
        assert isinstance(data["approvals"], list)


# ---------------------------------------------------------------------------
# WebSocket typed protocol tests
# ---------------------------------------------------------------------------

class TestWebSocketProtocol:
    @pytest.mark.asyncio
    async def test_ws_connected_event_with_protocol_version(self):
        """WS 接続直後に connected イベント(protocol_version付き)が届く"""
        url = f"{WS_URL}/ws/e2e_ws"
        async with websockets.connect(url) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            payload = json.loads(raw)
            assert payload["event"] == "connected"
            assert payload["session_id"]
            assert payload["user_id"] == "e2e_ws"
            assert payload["protocol_version"] >= 1
            assert "v" in payload  # envelope version field
            assert "ts" in payload  # timestamp field

    @pytest.mark.asyncio
    async def test_ws_chat_done(self):
        """chat.send に対して chat.done が返る"""
        url = f"{WS_URL}/ws/e2e_ws"
        payload = await _ws_collect(
            url,
            send={"event": "chat.send", "text": "一言で答えて: 1+1は?"},
            collect_event="chat.done",
        )
        assert payload["aborted"] is False
        assert len(payload["text"]) > 0
        assert "v" in payload  # envelope
        assert "ts" in payload

    @pytest.mark.asyncio
    async def test_ws_chat_abort(self):
        """chat.abort 後に chat.done(aborted=True) が返る"""
        url = f"{WS_URL}/ws/e2e_ws_abort"
        async with websockets.connect(url) as ws:
            # connected 受信
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            assert json.loads(raw)["event"] == "connected"

            # メッセージ送信
            await ws.send(json.dumps({
                "event": "chat.send",
                "text": "日本の歴史を詳しく1000文字で説明して"
            }))

            # すぐ abort
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"event": "chat.abort"}))

            # chat.done(aborted=True) を待つ
            deadline = asyncio.get_event_loop().time() + 30
            done_payload = None
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                p = json.loads(raw)
                if p.get("event") == "chat.done":
                    done_payload = p
                    break

            assert done_payload is not None
            assert done_payload["aborted"] is True

    @pytest.mark.asyncio
    async def test_ws_presence_ping(self):
        """presence.ping に対して health.tick が返る"""
        url = f"{WS_URL}/ws/e2e_ws_ping"
        payload = await _ws_collect(
            url,
            send={"event": "presence.ping"},
            collect_event="health.tick",
            timeout=10,
        )
        assert "active_sessions" in payload
        assert "ts" in payload
        assert "v" in payload

    @pytest.mark.asyncio
    async def test_ws_legacy_message_type(self):
        """旧プロトコル type=message も動作すること（後方互換）"""
        url = f"{WS_URL}/ws/e2e_ws_legacy"
        payload = await _ws_collect(
            url,
            send={"type": "message", "message": "一言で答えて: 空の色は?"},
            collect_event="chat.done",
        )
        assert payload["aborted"] is False
        assert len(payload["text"]) > 0

    @pytest.mark.asyncio
    async def test_ws_chat_history(self):
        """chat.history request returns transcript entries."""
        url = f"{WS_URL}/ws/e2e_ws_hist"
        async with websockets.connect(url) as ws:
            # connected
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            connected = json.loads(raw)
            assert connected["event"] == "connected"

            # Send a message first
            await ws.send(json.dumps({
                "event": "chat.send",
                "text": "historyテスト: hello"
            }))

            # Wait for chat.done
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                p = json.loads(raw)
                if p.get("event") == "chat.done":
                    break

            # Now request history
            await ws.send(json.dumps({"event": "chat.history", "limit": 50}))

            # Wait for chat.history response
            deadline = asyncio.get_event_loop().time() + 10
            history_payload = None
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                p = json.loads(raw)
                if p.get("event") == "chat.history":
                    history_payload = p
                    break

            assert history_payload is not None
            assert "entries" in history_payload
            assert len(history_payload["entries"]) >= 2  # user + assistant

    @pytest.mark.asyncio
    async def test_ws_control_ui_chat_operator_uses_dedicated_tool(self):
        """Control UI chat flow should use the dedicated operator instead of generic browser exploration."""
        url = f"{WS_URL}/ws/e2e_ws_control_ui_chat"
        done_payload, events = await _ws_run_with_auto_approvals(
            url,
            send={
                "event": "chat.send",
                "text": "http://localhost:18789/chat にアクセスして Hello World と送って返答を教えて",
            },
            timeout=90.0,
        )

        tool_starts = [event for event in events if event.get("event") == "tool.start"]
        tool_results = [event for event in events if event.get("event") == "tool.result"]
        tool_names = {event.get("tool_name") for event in tool_starts}

        assert "control_ui_chat_send_message" in tool_names
        assert "browser_extract_text" not in tool_names
        assert "browser_screenshot" not in tool_names
        assert "browser_fill" not in tool_names
        assert "browser_click" not in tool_names

        host_control_ui_start = next(
            (event for event in tool_starts if event.get("tool_name") == "host.control_ui_chat.send_message"),
            None,
        )
        assert host_control_ui_start is not None
        assert host_control_ui_start["args"].get("visible") is True

        control_ui_result = next(
            (
                event
                for event in tool_results
                if event.get("tool_name") in {"control_ui_chat_send_message", "host.control_ui_chat.send_message"}
            ),
            None,
        )
        assert control_ui_result is not None

        if control_ui_result["result"].get("ok") is False or control_ui_result["result"].get("success") is False:
            error_text = json.dumps(control_ui_result["result"], ensure_ascii=False)
            assert (
                "Playwright is not installed" in error_text
                or "Host Bridge" in error_text
                or "has been closed" in error_text
            )
            # LLM may phrase the failure differently depending on the error source
            done_text = done_payload.get("text", "")
            assert done_text, "chat.done should contain some text even on tool failure"
        else:
            result_payload = control_ui_result["result"]
            assistant_reply = result_payload.get("assistant_reply") or ""
            assert assistant_reply.strip()
            assert done_payload["aborted"] is False
            assert len(done_payload["text"]) > 0

    @pytest.mark.asyncio
    async def test_ws_computer_operator_observes_before_answering(self):
        """Browser-first computer-use flow should route to computer_operator and call computer_observe."""
        url = f"{WS_URL}/ws/e2e_ws_computer_observe"
        done_payload, events = await _ws_run_with_auto_approvals(
            url,
            send={
                "event": "chat.send",
                "text": "computer use としてこのブラウザの画面を見て押せるボタンを教えて",
            },
            timeout=90.0,
        )

        route_selected = next(
            (
                event for event in events
                if event.get("event") == "system.event" and event.get("source") == "router"
            ),
            None,
        )
        assert route_selected is not None
        assert route_selected["agent_name"] == "computer_operator"

        tool_starts = [event for event in events if event.get("event") == "tool.start"]
        tool_results = [event for event in events if event.get("event") == "tool.result"]
        tool_names = {event.get("tool_name") for event in tool_starts}

        assert "computer_observe" in tool_names

        computer_observe_result = next(
            (event for event in tool_results if event.get("tool_name") == "computer_observe"),
            None,
        )
        assert computer_observe_result is not None
        observe_payload = computer_observe_result["result"]
        assert observe_payload.get("mode") == "browser_first"
        assert observe_payload.get("surface_order") == ["current_tab", "desktop"]
        assert "preferred_surface" in observe_payload
        assert "available_surfaces" in observe_payload

        assert done_payload["aborted"] is False
        assert len(done_payload["text"]) > 0

    @pytest.mark.asyncio
    async def test_ws_chat_inject(self):
        """chat.inject should be recorded and acknowledged."""
        url = f"{WS_URL}/ws/e2e_ws_inject"
        async with websockets.connect(url) as ws:
            # connected
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            assert json.loads(raw)["event"] == "connected"

            # Inject a system message
            await ws.send(json.dumps({
                "event": "chat.inject",
                "text": "This is an injected context message",
                "role": "system",
            }))

            # Should get system.event acknowledgment
            deadline = asyncio.get_event_loop().time() + 10
            ack = None
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                p = json.loads(raw)
                if p.get("event") == "system.event" and p.get("source") == "inject":
                    ack = p
                    break

            assert ack is not None
            assert ack["status"] == "ok"

    @pytest.mark.asyncio
    async def test_ws_tools_approval(self):
        """tools.approval response should be acknowledged via system.event."""
        url = f"{WS_URL}/ws/e2e_ws_approval"
        async with websockets.connect(url) as ws:
            # connected
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            assert json.loads(raw)["event"] == "connected"

            # Send approval for a non-existent request (should get not_found)
            await ws.send(json.dumps({
                "event": "tools.approval",
                "request_id": "nonexistent_req_123",
                "approved": True,
            }))

            # Should get system.event with status=not_found
            deadline = asyncio.get_event_loop().time() + 10
            response = None
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                p = json.loads(raw)
                if p.get("event") == "system.event" and p.get("source") == "tools.approval":
                    response = p
                    break

            assert response is not None
            assert response["status"] == "not_found"
