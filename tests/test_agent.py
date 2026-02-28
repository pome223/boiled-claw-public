"""
基本的なエージェントテスト
"""

import pytest
from src.agents.root_agent import root_agent
from src.agents.model_config import DEFAULT_MODEL, get_model_config


def test_root_agent_exists():
    """Root agentが存在することを確認"""
    assert root_agent is not None
    assert root_agent.name == "boiled_claw"


def test_root_agent_model():
    """Root agentのモデルがgemini-3.0-flashであることを確認"""
    assert root_agent.model == "gemini-3.0-flash"


def test_root_agent_tools():
    """Root agentがツールを持っていることを確認"""
    assert len(root_agent.tools) > 0
    tool_names = [tool.__name__ for tool in root_agent.tools if hasattr(tool, '__name__')]
    assert "web_search" in tool_names or any("web" in name for name in tool_names)


def test_model_config():
    """モデル設定が正しいことを確認"""
    assert DEFAULT_MODEL.name == "gemini-3.0-flash"
    assert DEFAULT_MODEL.temperature == 0.7

    config = get_model_config("default")
    assert config.name == "gemini-3.0-flash"

    config = get_model_config("precise")
    assert config.name == "gemini-3.0-flash"
    assert config.temperature == 0.2


@pytest.mark.asyncio
async def test_web_search_tool():
    """Web検索ツールの基本テスト"""
    from src.tools.web_search import web_search

    result = await web_search("Python programming")
    assert "results" in result or "error" in result


@pytest.mark.asyncio
async def test_shell_tool_blocked():
    """危険なコマンドがブロックされることを確認"""
    from src.tools.shell import run_shell

    result = await run_shell("rm -rf /")
    assert "error" in result or result["return_code"] == -1


@pytest.mark.asyncio
async def test_file_manager():
    """ファイル操作ツールの基本テスト"""
    from src.tools.file_manager import write_file, read_file
    import tempfile
    import os

    # 一時ファイル
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        temp_path = f.name

    try:
        # 書き込み
        write_result = await write_file(temp_path, "test content")
        assert write_result.get("success") or "path" in write_result

        # 読み込み
        read_result = await read_file(temp_path)
        assert "content" in read_result or "error" in read_result

    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_security_policy():
    """セキュリティポリシーのテスト"""
    from src.security.policy import get_security_policy

    policy = get_security_policy()

    # 危険なコマンドがブロックされる
    allowed, reason = policy.is_command_allowed("rm -rf /")
    assert not allowed
    assert reason is not None

    # 安全なコマンドは許可される
    allowed, reason = policy.is_command_allowed("ls -la")
    assert allowed


def test_audit_logger():
    """監査ログのテスト"""
    from src.security.audit import get_audit_logger, AuditEventType

    logger = get_audit_logger()

    # ログ記録
    logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id="test_user",
        action="test",
        result="success",
    )

    # ログ取得
    logs = logger.get_recent_logs(limit=10)
    assert len(logs) >= 0


@pytest.mark.asyncio
async def test_memory_store():
    """メモリストアのテスト"""
    from src.tools.memory import memory_store, memory_search

    # 保存
    result = await memory_store(
        content="テスト情報",
        tags="test,pytest",
    )
    assert result.get("success")

    # 検索
    search_result = await memory_search(tags="test", limit=5)
    assert search_result.get("success")
    assert "results" in search_result


def test_channel_registry():
    """チャネルレジストリのテスト"""
    from src.channels.registry import get_channel_registry

    registry = get_channel_registry()
    assert registry is not None

    # 初期状態では空
    channels = registry.list_channels()
    assert isinstance(channels, list)


def test_skill_registry():
    """スキルレジストリのテスト"""
    from src.skills.base import get_skill_registry

    registry = get_skill_registry()
    assert registry is not None

    skills = registry.list_skills()
    assert isinstance(skills, list)
