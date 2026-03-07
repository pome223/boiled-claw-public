"""
サンプル MCP サーバーのテスト

- ツール一覧・各ツールの動作を直接検証するユニットテスト
- stdio 経由の MCP プロトコル通信を検証する統合テスト
"""

import asyncio
import json
import sys
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# ユニットテスト: FastMCP ツールを直接呼び出す
# ---------------------------------------------------------------------------

class TestSampleServerTools:
    @pytest.fixture
    def mcp(self):
        from src.mcp_servers.sample_server import create_server
        return create_server()

    @pytest.mark.asyncio
    async def test_tools_list(self, mcp):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {"echo", "add", "current_time", "reverse_text"}

    def _text(self, result) -> str:
        """call_tool の戻り値 (contents, meta) タプルからテキストを取り出す。"""
        contents, _ = result
        return "".join(c.text for c in contents if hasattr(c, "text"))

    @pytest.mark.asyncio
    async def test_echo(self, mcp):
        result = await mcp.call_tool("echo", {"text": "hello"})
        assert "hello" in self._text(result)

    @pytest.mark.asyncio
    async def test_add_integers(self, mcp):
        result = await mcp.call_tool("add", {"a": 3, "b": 4})
        assert "7" in self._text(result)

    @pytest.mark.asyncio
    async def test_add_floats(self, mcp):
        result = await mcp.call_tool("add", {"a": 1.5, "b": 2.5})
        assert "4" in self._text(result)

    @pytest.mark.asyncio
    async def test_current_time_is_iso(self, mcp):
        import datetime
        result = await mcp.call_tool("current_time", {})
        text = self._text(result)
        # ISO 8601: YYYY-MM-DDTHH:MM:SS.ffffff
        datetime.datetime.fromisoformat(text.strip())

    @pytest.mark.asyncio
    async def test_reverse_text(self, mcp):
        result = await mcp.call_tool("reverse_text", {"text": "abcde"})
        assert "edcba" in self._text(result)

    @pytest.mark.asyncio
    async def test_reverse_text_empty(self, mcp):
        result = await mcp.call_tool("reverse_text", {"text": ""})
        assert result is not None

    @pytest.mark.asyncio
    async def test_echo_returns_echo_prefix(self, mcp):
        result = await mcp.call_tool("echo", {"text": "world"})
        assert self._text(result).startswith("echo:")


# ---------------------------------------------------------------------------
# 統合テスト: stdio MCP プロトコルで通信する
# ---------------------------------------------------------------------------

async def _send_stdio_requests(messages: list[dict]) -> list[dict]:
    """
    sample_server を stdio サブプロセスとして起動し、
    JSON-RPC メッセージを送って応答を受け取る。
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.mcp_servers.sample_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=ROOT,
    )

    # 初期化シーケンス + 追加メッセージを一括送信
    init_messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]
    all_messages = init_messages + messages
    payload = "\n".join(json.dumps(m) for m in all_messages) + "\n"
    proc.stdin.write(payload.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    responses = []
    try:
        async with asyncio.timeout(10):
            raw = await proc.stdout.read()
    except asyncio.TimeoutError:
        proc.kill()
        raise

    await proc.wait()

    for line in raw.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "id" in obj:   # notification は id を持たない
                responses.append(obj)
        except json.JSONDecodeError:
            pass

    return responses


@pytest.mark.asyncio
async def test_stdio_tools_list():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    tools_resp = next((r for r in responses if r.get("id") == 1), None)
    assert tools_resp is not None
    names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert names == {"echo", "add", "current_time", "reverse_text"}


@pytest.mark.asyncio
async def test_stdio_call_echo():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"text": "ping"}}},
    ])
    resp = next((r for r in responses if r.get("id") == 2), None)
    assert resp is not None
    content = resp["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "ping" in text


@pytest.mark.asyncio
async def test_stdio_call_add():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "add", "arguments": {"a": 10, "b": 32}}},
    ])
    resp = next((r for r in responses if r.get("id") == 3), None)
    assert resp is not None
    content = resp["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "42" in text


@pytest.mark.asyncio
async def test_stdio_call_reverse_text():
    responses = await _send_stdio_requests([
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "reverse_text", "arguments": {"text": "hello"}}},
    ])
    resp = next((r for r in responses if r.get("id") == 4), None)
    assert resp is not None
    content = resp["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "olleh" in text
