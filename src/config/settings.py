"""
設定管理 - Pydantic Settings
環境変数から設定を読み込む
"""

from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション設定"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Google AI / ADK
    google_api_key: str = Field(..., description="Google AI API Key")
    google_genai_use_vertexai: bool = Field(default=False, description="Use Vertex AI")

    # Agent settings
    agent_name: str = Field(default="boiled-claw", description="Agent name")
    agent_model: str = Field(default="gemini-3-flash-preview", description="Default model")

    # Gateway settings
    gateway_host: str = Field(default="127.0.0.1", description="Gateway host")
    gateway_port: int = Field(default=18789, description="Gateway port")
    gateway_ws_path: str = Field(default="/ws", description="WebSocket path")

    # Channels
    telegram_bot_token: Optional[str] = Field(default=None, description="Telegram bot token")
    discord_bot_token: Optional[str] = Field(default=None, description="Discord bot token")
    slack_bot_token: Optional[str] = Field(default=None, description="Slack bot token")
    slack_app_token: Optional[str] = Field(default=None, description="Slack app token")

    # Memory settings
    memory_db_path: Path = Field(default=Path("data/memory.db"), description="Memory DB path")
    memory_vector_dim: int = Field(default=768, description="Vector dimension")
    memory_embedding_model: str = Field(
        default="text-embedding-004",
        description="Embedding model for memory vectors",
    )

    # Security settings
    audit_log_path: Path = Field(default=Path("data/audit.log"), description="Audit log path")
    shell_enabled: bool = Field(default=True, description="Enable shell execution")

    # Redis settings (for future session store)
    redis_url: Optional[str] = Field(default=None, description="Redis URL")

    # Browser settings
    browser_headless: bool = Field(default=True, description="Headless browser mode")
    browser_timeout: int = Field(default=30000, description="Browser timeout (ms)")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # ディレクトリ作成
        self.memory_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)


# グローバルインスタンス
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """設定インスタンスを取得"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
