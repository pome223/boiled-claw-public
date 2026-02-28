"""
モデル設定管理
Gemini 3.0 Flash をデフォルトとする
"""

from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class GeminiModelConfig:
    """Gemini モデル設定"""

    name: str
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None

    def to_generation_config(self) -> Dict[str, Any]:
        """ADK の generation_config 形式に変換"""
        config = {
            "temperature": self.temperature,
        }
        if self.max_tokens:
            config["max_output_tokens"] = self.max_tokens
        if self.top_p:
            config["top_p"] = self.top_p
        if self.top_k:
            config["top_k"] = self.top_k
        return config


# デフォルトモデル設定
DEFAULT_MODEL = GeminiModelConfig(
    name="gemini-3.0-flash",
    temperature=0.7,
)

# 高精度モデル設定
PRECISE_MODEL = GeminiModelConfig(
    name="gemini-3.0-flash",
    temperature=0.2,
    top_k=20,
)

# 創造的モデル設定
CREATIVE_MODEL = GeminiModelConfig(
    name="gemini-3.0-flash",
    temperature=1.2,
)


def get_model_config(name: str = "default") -> GeminiModelConfig:
    """モデル設定を取得"""
    configs = {
        "default": DEFAULT_MODEL,
        "precise": PRECISE_MODEL,
        "creative": CREATIVE_MODEL,
    }
    return configs.get(name, DEFAULT_MODEL)
