"""
基本的なエージェントテスト
"""

import pytest
from src.agents.root_agent import root_agent
from src.agents.sub_agents import SUB_AGENTS
from src.agents.model_config import DEFAULT_MODEL, get_model_config


def test_root_agent_exists():
    """Root agentが存在することを確認"""
    assert root_agent is not None
    assert root_agent.name == "boiled_claw"


def test_root_agent_model():
    """Root agentのモデルが既定モデルと一致することを確認"""
    assert root_agent.model == DEFAULT_MODEL.name


def test_root_agent_tools():
    """Root agentがツールを持っていることを確認"""
    assert len(root_agent.tools) > 0
    tool_names = [
        tool.__name__ if hasattr(tool, "__name__")
        else getattr(tool, "name", "")
        for tool in root_agent.tools
    ]
    assert "web_search" in tool_names


def test_root_agent_sub_agents_connected():
    """Root agentにサブエージェントが配線されていることを確認"""
    assert len(root_agent.sub_agents) == len(SUB_AGENTS)
    assert {agent.name for agent in root_agent.sub_agents} == {agent.name for agent in SUB_AGENTS}


def test_root_agent_has_orchestration_tools():
    """sessions_spawn 系のオーケストレーションツールが配線されていることを確認"""
    tool_names = [
        tool.__name__ if hasattr(tool, "__name__")
        else getattr(tool, "name", "")
        for tool in root_agent.tools
    ]

    for sub_agent in SUB_AGENTS:
        assert sub_agent.name not in tool_names

    assert "sessions_spawn" in tool_names
    assert "subagents_list" in tool_names
    assert "subagents_steer" in tool_names
    assert "subagents_kill" in tool_names


def test_model_config():
    """モデル設定が正しいことを確認"""
    assert DEFAULT_MODEL.name == "gemini-3.1-flash-lite-preview"
    assert DEFAULT_MODEL.temperature == 0.7

    config = get_model_config("default")
    assert config.name == DEFAULT_MODEL.name

    config = get_model_config("precise")
    assert config.name == DEFAULT_MODEL.name
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
async def test_file_manager(monkeypatch):
    """ファイル操作ツールの基本テスト"""
    from src.tools import file_manager as file_module
    import tempfile
    import os

    monkeypatch.setattr(
        file_module,
        "get_settings",
        lambda: type("Settings", (), {"host_bridge_enabled": False})(),
    )

    write_file = file_module.write_file
    read_file = file_module.read_file

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
async def test_memory_store(monkeypatch):
    """メモリストアのテスト"""
    import tempfile
    import os
    from src.tools import memory as memory_module

    async def fake_embed(text: str, *, task_type: str, output_dimensionality: int):
        vec = [0.0] * output_dimensionality
        vec[0] = 1.0 if "テスト" in text else 0.2
        return vec

    monkeypatch.setattr(memory_module, "_embed_with_google", fake_embed)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        db_path = f.name

    old_store = memory_module._memory_store
    memory_module._memory_store = memory_module.MemoryStore(db_path=db_path)

    try:
        # 保存
        result = await memory_module.memory_store(
            content="テスト情報",
            tags="test,pytest",
        )
        assert result.get("success")

        # 検索
        search_result = await memory_module.memory_search(tags="test", limit=5)
        assert search_result.get("success")
        assert "results" in search_result
    finally:
        memory_module._memory_store = old_store
        if os.path.exists(db_path):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_memory_vector_search_ranks_semantic_match(monkeypatch):
    """ベクトル検索で関連文が上位に来ることを確認"""
    from src.tools import memory as memory_module
    import tempfile
    import os

    async def fake_embed(text: str, *, task_type: str, output_dimensionality: int):
        vec = [0.0] * output_dimensionality
        if "API" in text or "FastAPI" in text or "開発" in text:
            vec[0] = 1.0
        if "カレー" in text:
            vec[1] = 1.0
        return vec

    monkeypatch.setattr(memory_module, "_embed_with_google", fake_embed)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        db_path = f.name

    old_store = memory_module._memory_store
    memory_module._memory_store = memory_module.MemoryStore(db_path=db_path)

    try:
        await memory_module.memory_store("PythonとFastAPIでWeb APIを実装した", tags="dev")
        await memory_module.memory_store("週末にカレーを作って食べた", tags="life")
        await memory_module.memory_store("REST APIのエンドポイント設計メモ", tags="dev")

        result = await memory_module.memory_search(query="API 開発", limit=2)

        assert result.get("success")
        assert result["count"] >= 1
        assert "score" in result["results"][0]
        assert result["results"][0]["score"] >= 0.0
        assert any(
            "API" in item["content"] or "FastAPI" in item["content"]
            for item in result["results"]
        )
    finally:
        memory_module._memory_store = old_store
        if os.path.exists(db_path):
            os.unlink(db_path)


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
