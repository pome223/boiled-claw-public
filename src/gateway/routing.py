from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_TARGETS = {"root_agent", "control_loop", "specialist", "dynamic_agent"}
VALID_SPECIALISTS = {
    "web_researcher",
    "file_manager",
    "browser_automator",
    "desktop_operator",
    "system_operator",
    "memory_keeper",
}
VALID_HANDOFF_MODES = {"direct", "preflight_then_root"}

_RESEARCH_KEYWORDS = {
    "最新",
    "最近",
    "ニュース",
    "調べて",
    "調査",
    "リサーチ",
    "噂",
    "今年",
    "来日",
    "予定",
    "公演",
    "ライブ",
    "フェス",
    "開催",
    "話題",
    "発表",
    "gtc",
    "report",
    "research",
}

_LONGFORM_KEYWORDS = {
    "詳細",
    "詳しく",
    "レポート",
    "report",
    "分析",
    "analyze",
    "analysis",
    "比較",
    "まとめて",
    "提案",
    "計画",
    "plan",
    "戦略",
    "多角的",
    "多面的",
}

_BROWSER_KEYWORDS = {
    "url",
    "http://",
    "https://",
    "ブラウザ",
    "ページ",
    "サイト",
    "スクレイプ",
    "スクレイピング",
    "抽出",
    "navigate",
    "browse",
}

_DESKTOP_VIEW_KEYWORDS = {
    "画面",
    "スクリーン",
    "スクショ",
    "スクリーンショット",
    "ウィンドウ",
    "前面アプリ",
    "frontmost",
    "window",
    "screen",
    "desktop",
}

_DESKTOP_CONTROL_KEYWORDS = {
    "クリック",
    "click",
    "drag",
    "ドラッグ",
    "type",
    "入力",
    "打って",
    "押して",
    "hotkey",
    "ショートカット",
    "アプリを開いて",
    "focus",
}

_FILE_KEYWORDS = {
    "ファイル",
    "readme",
    "コード",
    "差分",
    "diff",
    "編集",
    "修正",
    "書き換え",
    "refactor",
}

_SYSTEM_KEYWORDS = {
    "shell",
    "コマンド",
    "ターミナル",
    "terminal",
    "docker",
    "git",
    "bash",
    "zsh",
    "cli",
}

_MEMORY_KEYWORDS = {
    "覚えて",
    "remember",
    "記憶",
    "メモリ",
    "memory",
    "過去の会話",
    "嗜好",
}

_DYNAMIC_AGENT_KEYWORDS = {
    "mcp",
    "カスタムエージェント",
    "専用エージェント",
    "dynamic agent",
    "custom agent",
    "このサーバーを使って",
}

_SKILL_KEYWORDS = {
    "skill",
    "スキル",
}


@dataclass
class DynamicAgentRequest:
    instruction: str = ""
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "run"


@dataclass
class RoutingDecision:
    target: str = "root_agent"
    specialist: str | None = None
    handoff_mode: str = "direct"
    reason: str = ""
    confidence: float = 0.0
    dynamic_agent: DynamicAgentRequest = field(default_factory=DynamicAgentRequest)

    @property
    def preflight_specialist(self) -> bool:
        return self.target == "specialist" and self.handoff_mode == "preflight_then_root"

    @property
    def route_label(self) -> str:
        if self.target == "specialist" and self.specialist:
            return self.specialist
        return self.target


def coerce_decision(payload: dict[str, Any] | None) -> RoutingDecision:
    if not isinstance(payload, dict):
        return heuristic_decision("")

    target = str(payload.get("target") or "root_agent").strip()
    if target not in VALID_TARGETS:
        target = "root_agent"

    specialist = payload.get("specialist")
    if specialist is not None:
        specialist = str(specialist).strip()
    if specialist not in VALID_SPECIALISTS:
        specialist = None

    handoff_mode = str(payload.get("handoff_mode") or "direct").strip()
    if handoff_mode not in VALID_HANDOFF_MODES:
        handoff_mode = "direct"

    confidence = _coerce_confidence(payload.get("confidence"))
    reason = str(payload.get("reason") or "").strip()

    dynamic_payload = payload.get("dynamic_agent")
    dynamic_request = DynamicAgentRequest()
    if isinstance(dynamic_payload, dict):
        instruction = str(dynamic_payload.get("instruction") or "").strip()
        mode = str(dynamic_payload.get("mode") or "run").strip().lower()
        if mode not in {"run", "session"}:
            mode = "run"
        mcp_servers = dynamic_payload.get("mcp_servers") or []
        if not isinstance(mcp_servers, list):
            mcp_servers = []
        normalized_servers = [
            item for item in mcp_servers if isinstance(item, dict)
        ]
        dynamic_request = DynamicAgentRequest(
            instruction=instruction,
            mcp_servers=normalized_servers,
            mode=mode,
        )

    if target == "specialist" and specialist is None:
        target = "root_agent"
        handoff_mode = "direct"
    if target != "specialist":
        specialist = None
        handoff_mode = "direct"
    if target != "dynamic_agent":
        dynamic_request = DynamicAgentRequest()

    return RoutingDecision(
        target=target,
        specialist=specialist,
        handoff_mode=handoff_mode,
        reason=reason,
        confidence=confidence,
        dynamic_agent=dynamic_request,
    )


def heuristic_decision(message: str) -> RoutingDecision:
    normalized = (message or "").strip().lower()
    if not normalized:
        return RoutingDecision()

    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_browser = _contains_any(normalized, _BROWSER_KEYWORDS)
    has_desktop_view = _contains_any(normalized, _DESKTOP_VIEW_KEYWORDS)
    has_desktop_control = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS)
    has_file = _contains_any(normalized, _FILE_KEYWORDS)
    has_system = _contains_any(normalized, _SYSTEM_KEYWORDS)
    has_memory = _contains_any(normalized, _MEMORY_KEYWORDS)
    has_dynamic = _contains_any(normalized, _DYNAMIC_AGENT_KEYWORDS)
    has_skill = _contains_any(normalized, _SKILL_KEYWORDS)

    if has_dynamic:
        return RoutingDecision(
            target="dynamic_agent",
            reason="request explicitly asks for a custom or MCP-backed agent",
            confidence=0.9,
        )

    if has_research and has_longform:
        return RoutingDecision(
            target="control_loop",
            reason="latest or research-heavy request with long-form output",
            confidence=0.82,
        )

    if has_desktop_control:
        return RoutingDecision(
            target="specialist",
            specialist="desktop_operator",
            handoff_mode="direct",
            reason="desktop control request",
            confidence=0.83,
        )

    if has_desktop_view:
        return RoutingDecision(
            target="specialist",
            specialist="desktop_operator",
            handoff_mode="preflight_then_root",
            reason="desktop state inspection request",
            confidence=0.8,
        )

    if has_browser:
        return RoutingDecision(
            target="specialist",
            specialist="browser_automator",
            handoff_mode="preflight_then_root",
            reason="browser or page interaction request",
            confidence=0.8,
        )

    if has_research:
        return RoutingDecision(
            target="specialist",
            specialist="web_researcher",
            handoff_mode="preflight_then_root",
            reason="latest or research-sensitive request",
            confidence=0.84,
        )

    if has_system:
        return RoutingDecision(
            target="specialist",
            specialist="system_operator",
            reason="shell or system task",
            confidence=0.78,
        )

    if has_file:
        return RoutingDecision(
            target="specialist",
            specialist="file_manager",
            reason="file or code-oriented request",
            confidence=0.76,
        )

    if has_memory:
        return RoutingDecision(
            target="specialist",
            specialist="memory_keeper",
            reason="memory-oriented request",
            confidence=0.72,
        )

    if has_skill:
        return RoutingDecision(
            target="root_agent",
            reason="skill requests should be handled by root_agent unless a dynamic agent is required",
            confidence=0.7,
        )

    return RoutingDecision(
        target="root_agent",
        reason="default conversational handling",
        confidence=0.6,
    )


def decision_from_payload(
    payload: dict[str, Any] | None,
    *,
    fallback_message: str,
) -> RoutingDecision:
    decision = coerce_decision(payload)
    if decision.confidence > 0.0 or decision.reason:
        return decision
    return heuristic_decision(fallback_message)


def _contains_any(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _coerce_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))
