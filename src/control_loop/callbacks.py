"""
Callbacks — boiled-claw v2

ADK callback_context.state 経由で session state を更新する。
Session を直接書き換えない（ADK 推奨の context 経由のみ）。

含まれる callback:
  - policy_judge_callback  : planner_agent の after_agent_callback
  - repair_callback        : verifier_agent の after_agent_callback
  - curator_callback       : verifier_agent pass 後の memory candidate 抽出
"""

from __future__ import annotations

import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from src.control_loop.constants import DEFAULT_MAX_REPAIR_ATTEMPTS
from src.runtime.state_keys import StateKeys
from src.tools.context import resolve_callback_context

logger = logging.getLogger(__name__)

# ── Policy Judge callback ──────────────────────────────────────────────────

# human approval が必要な capability (= 自動承認不可)
_HUMAN_REQUIRED_CAPS: set[str] = {
    "file.write",
    "shell.exec",
    "spawn.agent",
    "memory.delete",
    "desktop.view.screenshot",
    "desktop.ax.snapshot",
    "desktop.control.click",
    "desktop.control.type",
    "desktop.control.launch_app",
    "desktop.control.focus_window",
    "desktop.control.hotkey",
    "desktop.control.scroll",
    "desktop.control.drag",
}

# 常に拒否する capability
_ALWAYS_DENIED_CAPS: set[str] = {"admin"}

_CURRENT_BROWSER_KEYWORDS: set[str] = {
    "このブラウザ",
    "今のブラウザ",
    "いまのブラウザ",
    "current browser",
    "existing browser",
    "このタブ",
    "今のタブ",
    "このページ",
    "今のページ",
    "このウィンドウ",
}

_SPREADSHEET_KEYWORDS: set[str] = {
    "spreadsheet",
    "spread sheet",
    "sheet",
    "google sheet",
    "google sheets",
    "スプレッド",
    "すぷれっど",
    "スプシ",
    "スプレッドシート",
    "スプレッドsーと",
    "スプレッドシーート",
}

_TEXT_ENTRY_KEYWORDS: set[str] = {
    "入力",
    "記入",
    "書いて",
    "書き込",
    "貼り付",
    "ペースト",
    "まとめて",
    "まとめる",
    "追加",
    "更新",
    "fill",
    "enter",
    "paste",
    "type",
    "write",
}

_DESKTOP_MODE_BY_CAPABILITY: dict[str, str] = {
    "desktop.view.windows": "read",
    "desktop.view.frontmost_app": "read",
    "desktop.wait.window": "read",
    "desktop.ax.find": "read",
    "desktop.wait.element": "read",
    "desktop.ax.snapshot": "read",
    "desktop.control.click": "execute",
    "desktop.control.type": "execute",
    "desktop.control.launch_app": "execute",
    "desktop.control.focus_window": "execute",
    "desktop.control.hotkey": "execute",
    "desktop.control.scroll": "execute",
    "desktop.control.drag": "execute",
}


def _contains_any(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _targets_current_browser(goal: str) -> bool:
    return _contains_any(goal, _CURRENT_BROWSER_KEYWORDS)


def _needs_text_entry(goal: str) -> bool:
    return _contains_any(goal, _SPREADSHEET_KEYWORDS | _TEXT_ENTRY_KEYWORDS)


def _ensure_capability(
    required_caps: list[dict[str, object]],
    capability_name: str,
) -> None:
    if any(cap.get("name") == capability_name for cap in required_caps):
        return
    required_caps.append(
        {
            "name": capability_name,
            "mode": _DESKTOP_MODE_BY_CAPABILITY.get(capability_name, "execute"),
        }
    )


def _normalize_required_capabilities(plan: dict, goal: str) -> dict:
    required_caps = [
        cap if isinstance(cap, dict) else {"name": str(cap)}
        for cap in plan.get("required_capabilities", [])
    ]
    normalized_goal = (goal or plan.get("goal") or "").strip().lower()
    cap_names = {str(cap.get("name", "")) for cap in required_caps}

    if _targets_current_browser(normalized_goal):
        has_desktop_browser_plan = bool(
            cap_names
            & {
                "desktop.view.windows",
                "desktop.view.frontmost_app",
                "desktop.ax.snapshot",
                "desktop.control.focus_window",
                "desktop.ax.find",
                "desktop.wait.element",
                "desktop.control.click",
                "desktop.control.type",
            }
        )
        if has_desktop_browser_plan or "browser.navigate" in cap_names:
            _ensure_capability(required_caps, "desktop.view.windows")
            _ensure_capability(required_caps, "desktop.control.focus_window")
            _ensure_capability(required_caps, "desktop.control.click")
            _ensure_capability(required_caps, "desktop.ax.find")
            _ensure_capability(required_caps, "desktop.wait.element")

            if _needs_text_entry(normalized_goal):
                _ensure_capability(required_caps, "desktop.control.type")

    plan["required_capabilities"] = required_caps
    return plan

def policy_judge_callback(
    callback_context: CallbackContext,
) -> None:
    """
    planner_agent の after_agent_callback。
    temp:planner_draft を読み、capability と risk_level を評価して:
      - plan:approved
      - approval:status
      - plan:risk_level
    を session.state に書き込む。
    """
    raw_draft = callback_context.state.get(StateKeys.TEMP_PLANNER_DRAFT)
    if not raw_draft:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        logger.warning("policy_judge_callback: temp:planner_draft is empty")
        return

    try:
        plan = (
            raw_draft
            if isinstance(raw_draft, dict)
            else json.loads(raw_draft)
        )
    except (json.JSONDecodeError, TypeError) as e:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        logger.error("policy_judge_callback: JSON parse error: %s", e)
        return

    original_goal = callback_context.state.get(StateKeys.TASK_GOAL) or plan.get("goal", "")
    plan = _normalize_required_capabilities(plan, original_goal)
    required_caps: list[dict] = plan.get("required_capabilities", [])
    cap_names = {c.get("name", "") for c in required_caps}
    risk_level: str = plan.get("risk_level", "low")

    # 常に拒否
    denied = cap_names & _ALWAYS_DENIED_CAPS
    if denied:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        callback_context.state[StateKeys.APPROVAL_REQUEST] = None
        callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
        logger.warning("policy_judge_callback: denied caps=%s", denied)
        return

    # Human approval が必要な capability
    needs_human = bool(cap_names & _HUMAN_REQUIRED_CAPS) or risk_level == "critical"
    if needs_human:
        approval_request = {
            "request_id": f"plan_{uuid.uuid4().hex[:12]}",
            "plan_id": plan.get("plan_id", ""),
            "goal": original_goal,
            "risk_level": risk_level,
            "required_capabilities": sorted(cap_names),
            "reason": (
                "Human approval required due to capability or risk level."
            ),
            "plan": plan,
        }
        callback_context.state[StateKeys.APPROVAL_STATUS] = "needs_human"
        callback_context.state[StateKeys.APPROVAL_REQUEST] = approval_request
        callback_context.state[StateKeys.PLAN_APPROVED] = plan
        callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
        logger.info(
            "policy_judge_callback: needs_human (caps=%s, risk=%s)",
            cap_names & _HUMAN_REQUIRED_CAPS,
            risk_level,
        )
        return

    # 自動承認
    callback_context.state[StateKeys.PLAN_APPROVED] = plan
    callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
    callback_context.state[StateKeys.APPROVAL_STATUS] = "policy_approved"
    callback_context.state[StateKeys.APPROVAL_REQUEST] = None
    logger.info(
        "policy_judge_callback: policy_approved (risk=%s)", risk_level
    )
    return


# ── Repair callback ────────────────────────────────────────────────────────

_MAX_REPAIR_ATTEMPTS = DEFAULT_MAX_REPAIR_ATTEMPTS
_REPAIR_THRESHOLD_SCORE = 0.85


def repair_callback(
    callback_context: CallbackContext,
) -> None:
    """
    verifier_agent の after_agent_callback。
    verify:last_report を読み、repair が必要かどうかを判断して:
      - repair:count をインクリメント
      - temp:repair_patch を設定
    を session.state に書き込む。

    pass の場合は何もしない（curator_callback が後続処理する）。
    repair 上限に達した場合も何もしない。
    """
    raw_report = callback_context.state.get(StateKeys.VERIFY_LAST_REPORT)
    if not raw_report:
        return

    try:
        report = (
            raw_report
            if isinstance(raw_report, dict)
            else json.loads(raw_report)
        )
    except (json.JSONDecodeError, TypeError):
        return

    status = report.get("status", "error")
    if status == "pass":
        # 検証通過: repair 不要、repair:count をリセット
        callback_context.state[StateKeys.REPAIR_COUNT] = 0
        callback_context.state[StateKeys.TEMP_REPAIR_PATCH] = None
        return

    # fail / partial_pass → repair 判断
    repair_count = callback_context.state.get(StateKeys.REPAIR_COUNT, 0)

    if repair_count >= _MAX_REPAIR_ATTEMPTS:
        logger.warning(
            "repair_callback: max repair attempts (%d) reached", _MAX_REPAIR_ATTEMPTS
        )
        return

    repair_actions = report.get("repair_actions", [])
    failed_criteria = [
        r["name"] for r in report.get("criterion_results", []) if not r.get("passed")
    ]

    patch = {
        "note": f"Re-plan required. Failed criteria: {failed_criteria}. "
                f"Repair attempt {repair_count + 1}/{_MAX_REPAIR_ATTEMPTS}.",
        "failed_criteria": failed_criteria,
        "repair_actions": repair_actions,
        "previous_plan_id": (
            report.get("plan_id") or
            (callback_context.state.get(StateKeys.PLAN_APPROVED) or {}).get("plan_id")
        ),
    }

    callback_context.state[StateKeys.REPAIR_COUNT] = repair_count + 1
    callback_context.state[StateKeys.TEMP_REPAIR_PATCH] = patch
    logger.info(
        "repair_callback: repair triggered (attempt=%d, status=%s)",
        repair_count + 1,
        status,
    )
    return


# ── Curator callback ───────────────────────────────────────────────────────


def curator_callback(
    callback_context: CallbackContext,
) -> None:
    """
    verifier_agent pass 後の memory candidate 抽出 callback。
    verify:last_report が pass のとき、session の情報から memory candidate を
    非同期で抽出して candidate store に登録する。

    Note: ここでは候補の ID を memory:last_candidate_ids に書くにとどめる。
    実際の promote は Curator クラスが別途実行する。
    """
    raw_report = callback_context.state.get(StateKeys.VERIFY_LAST_REPORT)
    if not raw_report:
        return

    try:
        report = (
            raw_report
            if isinstance(raw_report, dict)
            else json.loads(raw_report)
        )
    except (json.JSONDecodeError, TypeError):
        return

    if report.get("status") != "pass":
        return

    # approved plan から memory candidates を生成
    raw_plan = callback_context.state.get(StateKeys.PLAN_APPROVED)
    if not raw_plan:
        return

    try:
        plan = raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
    except (json.JSONDecodeError, TypeError):
        return

    candidate_ids = _extract_and_register_candidates(
        plan=plan,
        report=report,
        callback_context=callback_context,
    )

    if candidate_ids:
        callback_context.state[StateKeys.MEMORY_LAST_CANDIDATE_IDS] = candidate_ids
        logger.info(
            "curator_callback: %d candidate(s) registered", len(candidate_ids)
        )

    return


def _extract_and_register_candidates(
    plan: dict,
    report: dict,
    callback_context: CallbackContext,
) -> list[str]:
    """
    plan / report の情報から MemoryCandidate を生成して CandidateStore に登録する。
    登録した candidate_id のリストを返す。
    """
    try:
        from src.memory_lifecycle.candidate_store import get_candidate_store
        from src.memory_lifecycle.memory_schema import (
            MemoryCandidate,
            MemoryType,
            OriginatorType,
            Provenance,
            SensitivityLevel,
        )
    except ImportError:
        logger.warning("curator_callback: memory_lifecycle not available")
        return []

    store = get_candidate_store()
    now = datetime.now(tz=timezone.utc)

    runtime_context = resolve_callback_context(callback_context)
    session_id = runtime_context["session_id"] or "unknown"
    user_id = runtime_context["user_id"] or "unknown"

    candidate_ids: list[str] = []

    # 1. goal → procedural memory candidate
    goal = plan.get("goal", "")
    if goal:
        cid = f"cand_{uuid.uuid4().hex[:10]}"
        candidate = MemoryCandidate(
            candidate_id=cid,
            session_id=session_id,
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            content=f"Task completed successfully: {goal}",
            subject=goal[:80],
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="control_loop_completion",
                captured_at=now,
            ),
            confidence=report.get("overall_score", 0.8),
            trust_score=report.get("confidence", 0.8),
            sensitivity=SensitivityLevel.INTERNAL,
        )
        store.save(candidate)
        candidate_ids.append(cid)

    # 2. success criteria → episodic memory candidate
    criteria = report.get("criterion_results", [])
    passed = [c["name"] for c in criteria if c.get("passed")]
    if passed:
        cid = f"cand_{uuid.uuid4().hex[:10]}"
        candidate = MemoryCandidate(
            candidate_id=cid,
            session_id=session_id,
            user_id=user_id,
            memory_type=MemoryType.EPISODIC,
            content=(
                f"Successfully completed '{goal}'. "
                f"Passed criteria: {', '.join(passed)}."
            ),
            subject=goal[:80],
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="control_loop_completion",
                captured_at=now,
            ),
            confidence=0.85,
            trust_score=0.85,
            sensitivity=SensitivityLevel.INTERNAL,
        )
        store.save(candidate)
        candidate_ids.append(cid)

    return candidate_ids
