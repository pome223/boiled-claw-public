"""
tests for sessions_spawn_dynamic and _build_mcp_toolsets
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.subagents import (
    _build_mcp_toolsets,
    sessions_spawn_dynamic,
    reset_subagent_manager_for_tests,
)


# ---------------------------------------------------------------------------
# _build_mcp_toolsets
# ---------------------------------------------------------------------------

class TestBuildMcpToolsets:
    def test_empty_list(self):
        assert _build_mcp_toolsets([]) == []

    def test_sse_server(self):
        toolsets = _build_mcp_toolsets([{"type": "sse", "url": "http://localhost:8080/sse"}])
        assert len(toolsets) == 1

    def test_http_server(self):
        toolsets = _build_mcp_toolsets([{"type": "http", "url": "http://localhost:8080/mcp"}])
        assert len(toolsets) == 1

    def test_stdio_server(self):
        toolsets = _build_mcp_toolsets([{"type": "stdio", "command": "npx", "args": ["-y", "some-mcp"]}])
        assert len(toolsets) == 1

    def test_multiple_servers(self):
        toolsets = _build_mcp_toolsets([
            {"type": "sse", "url": "http://a/sse"},
            {"type": "stdio", "command": "python", "args": ["server.py"]},
        ])
        assert len(toolsets) == 2

    def test_default_type_is_sse(self):
        # type を省略した場合は sse として扱う
        toolsets = _build_mcp_toolsets([{"url": "http://localhost/sse"}])
        assert len(toolsets) == 1

    def test_sse_missing_url_raises(self):
        with pytest.raises(ValueError, match="'url' is required"):
            _build_mcp_toolsets([{"type": "sse"}])

    def test_http_missing_url_raises(self):
        with pytest.raises(ValueError, match="'url' is required"):
            _build_mcp_toolsets([{"type": "http"}])

    def test_stdio_missing_command_raises(self):
        with pytest.raises(ValueError, match="'command' is required"):
            _build_mcp_toolsets([{"type": "stdio"}])

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            _build_mcp_toolsets([{"type": "grpc", "url": "http://x"}])


# ---------------------------------------------------------------------------
# sessions_spawn_dynamic
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def reset_manager():
    yield
    await reset_subagent_manager_for_tests()


@pytest.mark.asyncio
async def test_spawn_dynamic_invalid_json():
    result = await sessions_spawn_dynamic(
        task="hello",
        instruction="you are helpful",
        mcp_servers="not-json",
    )
    assert result["status"] == "error"
    assert "not valid JSON" in result["error"]


@pytest.mark.asyncio
async def test_spawn_dynamic_non_array_json():
    result = await sessions_spawn_dynamic(
        task="hello",
        instruction="you are helpful",
        mcp_servers='{"type": "sse"}',
    )
    assert result["status"] == "error"
    assert "JSON array" in result["error"]


@pytest.mark.asyncio
async def test_spawn_dynamic_invalid_mcp_server():
    result = await sessions_spawn_dynamic(
        task="hello",
        instruction="you are helpful",
        mcp_servers=json.dumps([{"type": "sse"}]),  # url 欠落
    )
    assert result["status"] == "error"
    assert "'url' is required" in result["error"]


@pytest.mark.asyncio
async def test_spawn_dynamic_no_mcp_servers():
    """MCP サーバーなしの純粋カスタム指示エージェントを spawn できる。"""
    mock_runner = MagicMock()
    mock_runner.run_async = AsyncMock(return_value=aiter([]))

    with patch("src.tools.subagents.Runner", return_value=mock_runner), \
         patch("src.tools.subagents.create_session_service") as mock_svc_factory:
        mock_svc = AsyncMock()
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_factory.return_value = mock_svc

        result = await sessions_spawn_dynamic(
            task="自己紹介して",
            instruction="あなたはテスト専用エージェントです。",
            mcp_servers="[]",
        )

    assert result["status"] == "accepted"
    assert result["run_id"].startswith("dyn_")
    assert result["task_id"].startswith("task_")
    assert result["mode"] == "run"


@pytest.mark.asyncio
async def test_spawn_dynamic_sets_dynamic_flag():
    """subagents_list で dynamic=True が返る。"""
    from src.tools.subagents import subagents_list, get_subagent_manager

    mock_runner = MagicMock()
    mock_runner.run_async = AsyncMock(return_value=aiter([]))

    with patch("src.tools.subagents.Runner", return_value=mock_runner), \
         patch("src.tools.subagents.create_session_service") as mock_svc_factory:
        mock_svc = AsyncMock()
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_factory.return_value = mock_svc

        spawn_result = await sessions_spawn_dynamic(
            task="test task",
            instruction="custom instruction",
            mcp_servers="[]",
        )

    manager = get_subagent_manager()
    state = manager._runs.get(spawn_result["run_id"])
    assert state is not None
    assert state.dynamic_instruction == "custom instruction"
    assert state.to_view()["dynamic"] is True


# ---------------------------------------------------------------------------
# helper: async iterator from list
# ---------------------------------------------------------------------------

async def aiter(items):
    for item in items:
        yield item
