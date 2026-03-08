"""
E2E smoke tests against a running boiled-claw gateway.

前提条件:
  - Gateway が http://127.0.0.1:18789 で起動していること
  - GOOGLE_API_KEY が設定されていること

実行:
  pytest tests/test_e2e.py -v -m e2e --timeout=60

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


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------

class TestHttpApi:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

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
# Cron API tests
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


# ---------------------------------------------------------------------------
# WebSocket typed protocol tests
# ---------------------------------------------------------------------------

class TestWebSocketProtocol:
    @pytest.mark.asyncio
    async def test_ws_connected_event(self):
        """WS 接続直後に connected イベントが届く"""
        url = f"{WS_URL}/ws/e2e_ws"
        async with websockets.connect(url) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            payload = json.loads(raw)
            assert payload["event"] == "connected"
            assert payload["session_id"]
            assert payload["user_id"] == "e2e_ws"

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
