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
from typing import AbstractSet, Optional

from google.adk.agents.callback_context import CallbackContext
from src.control_loop.constants import DEFAULT_MAX_REPAIR_ATTEMPTS
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import (
    CURRENT_BROWSER_KEYWORDS,
    SPREADSHEET_KEYWORDS,
)
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

_HOTKEY_HINT_KEYWORDS: set[str] = {
    "hotkey",
    "shortcut",
    "space key",
    "spacebar",
    "enter key",
    "return key",
    "keyboard shortcut",
    "スペースキー",
    "スペース",
    "ショートカット",
    "ホットキー",
    "enter",
    "return",
}

_PLAYBACK_HINT_KEYWORDS: set[str] = {
    "play music",
    "playback",
    "play song",
    "play track",
    "music",
    "song",
    "track",
    "audio",
    "media",
    "dj",
    "djay",
    "再生",
    "楽曲",
    "曲をかけて",
    "曲を再生",
    "音楽",
}

_PLAYBACK_ACTION_STEP_KEYWORDS: set[str] = {
    "play",
    "playback",
    "start",
    "resume",
    "再生",
    "開始",
    "スタート",
}

_VISUAL_EVIDENCE_KEYWORDS: set[str] = {
    "waveform",
    "indicator",
    "visual",
    "visually",
    "visible",
    "screen",
    "screenshot",
    "playing",
    "playback",
    "wave form",
    "波形",
    "インジケーター",
    "視覚",
    "画面",
    "スクリーンショット",
    "再生中",
    "動いている",
}

_DESKTOP_MODE_BY_CAPABILITY: dict[str, str] = {
    "desktop.view.windows": "read",
    "desktop.view.frontmost_app": "read",
    "desktop.view.screenshot": "read",
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


def _contains_any(text: str, keywords: AbstractSet[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _targets_current_browser(goal: str) -> bool:
    return _contains_any(goal, CURRENT_BROWSER_KEYWORDS)


def _needs_text_entry(goal: str) -> bool:
    return _contains_any(goal, SPREADSHEET_KEYWORDS | _TEXT_ENTRY_KEYWORDS)


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


def _step_has_capability(step: dict[str, object], capability_name: str) -> bool:
    for capability in step.get("capabilities", []):
        if isinstance(capability, dict) and str(capability.get("name", "")).strip() == capability_name:
            return True
    return False


def _step_has_any_capability(step: dict[str, object], capability_names: AbstractSet[str]) -> bool:
    return any(_step_has_capability(step, name) for name in capability_names)


def _ensure_step_capability(step: dict[str, object], capability_name: str) -> None:
    capabilities = step.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
        step["capabilities"] = capabilities
    if any(
        isinstance(capability, dict)
        and str(capability.get("name", "")).strip() == capability_name
        for capability in capabilities
    ):
        return
    capabilities.append(
        {
            "name": capability_name,
            "mode": _DESKTOP_MODE_BY_CAPABILITY.get(capability_name, "execute"),
        }
    )


def _step_capability_names(plan: dict) -> set[str]:
    names: set[str] = set()
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for capability in step.get("capabilities", []):
            if not isinstance(capability, dict):
                continue
            name = str(capability.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def _next_step_id(existing_ids: set[str], base: str) -> str:
    if base not in existing_ids:
        return base
    index = 2
    while f"{base}_{index}" in existing_ids:
        index += 1
    return f"{base}_{index}"


def _normalize_desktop_plan_steps(plan: dict, goal: str) -> None:
    raw_steps = plan.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        return
    steps = [step for step in raw_steps if isinstance(step, dict)]
    if not steps:
        return

    top_level_cap_names = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    step_cap_names = _step_capability_names(plan)
    cap_names = top_level_cap_names | step_cap_names
    ui_capability_names = {
        "desktop.ax.find",
        "desktop.wait.element",
        "desktop.control.click",
        "desktop.control.type",
        "desktop.control.drag",
        "desktop.control.hotkey",
        "desktop.control.scroll",
    }

    existing_ids = {
        str(step.get("step_id", "")).strip()
        for step in steps
        if str(step.get("step_id", "")).strip()
    }
    launch_index = next(
        (index for index, step in enumerate(steps) if _step_has_capability(step, "desktop.control.launch_app")),
        None,
    )

    has_desktop_ui_plan = bool(cap_names & ui_capability_names)
    if launch_index is not None and has_desktop_ui_plan and not any(
        _step_has_capability(step, "desktop.control.focus_window") for step in steps
    ):
        launch_step_id = str(steps[launch_index].get("step_id") or "").strip()
        if launch_step_id:
            focus_step_id = _next_step_id(existing_ids, f"{launch_step_id}_focus")
            focus_step = {
                "step_id": focus_step_id,
                "title": "アプリを前面にする",
                "description": "起動したアプリのウィンドウを前面にして操作対象を確定する。",
                "depends_on": [launch_step_id],
                "capabilities": [
                    {"name": "desktop.control.focus_window", "mode": "execute"},
                    {"name": "desktop.wait.window", "mode": "read"},
                ],
                "expected_outputs": ["対象アプリのウィンドウが前面で操作可能になっていること"],
                "retryable": True,
            }
            steps.insert(launch_index + 1, focus_step)
            existing_ids.add(focus_step_id)
            for step in steps[launch_index + 2 :]:
                depends_on = step.get("depends_on", [])
                if not isinstance(depends_on, list):
                    continue
                normalized_deps = [str(dep) for dep in depends_on]
                if launch_step_id in normalized_deps and focus_step_id not in normalized_deps:
                    step["depends_on"] = [
                        focus_step_id if dep == launch_step_id else dep
                        for dep in normalized_deps
                    ]

    playback_index = next(
        (
            index
            for index, step in enumerate(steps)
            if _step_is_playback_action_step(step)
            and _step_has_any_capability(
                step,
                {"desktop.control.click", "desktop.control.hotkey"},
            )
        ),
        None,
    )
    if playback_index is not None:
        has_pre_playback_capture = any(
            _step_has_capability(step, "desktop.view.screenshot")
            for step in steps[:playback_index]
        )
        if not has_pre_playback_capture:
            playback_step = steps[playback_index]
            depends_on = playback_step.get("depends_on", [])
            dependency_step_id = ""
            if isinstance(depends_on, list) and depends_on:
                dependency_step_id = str(depends_on[-1] or "").strip()
            if not dependency_step_id and playback_index > 0:
                dependency_step_id = str(
                    steps[playback_index - 1].get("step_id") or ""
                ).strip()
            capture_step_id = _next_step_id(
                existing_ids,
                "capture_pre_playback_state",
            )
            capture_step = {
                "step_id": capture_step_id,
                "title": "再生前の状態を記録",
                "description": "再生操作の前にUIの状態をスクリーンショットで記録する。",
                "depends_on": [dependency_step_id] if dependency_step_id else [],
                "capabilities": [
                    {"name": "desktop.view.screenshot", "mode": "read"},
                ],
                "expected_outputs": [
                    "再生前のUI状態のスクリーンショットが取得できていること",
                ],
                "retryable": True,
            }
            steps.insert(playback_index, capture_step)
            existing_ids.add(capture_step_id)
            if isinstance(depends_on, list) and depends_on:
                playback_step["depends_on"] = [
                    capture_step_id if str(dep or "").strip() == dependency_step_id else dep
                    for dep in depends_on
                ]
            else:
                playback_step["depends_on"] = [capture_step_id]

    playback_index = next(
        (
            index
            for index, step in enumerate(steps)
            if _step_is_playback_action_step(step)
            and _step_has_any_capability(
                step,
                {"desktop.control.click", "desktop.control.hotkey"},
            )
        ),
        None,
    )
    if _plan_needs_visual_evidence_capture(plan, goal) and not any(
        _step_has_any_capability(step, {"desktop.ax.snapshot", "desktop.view.screenshot"})
        for index, step in enumerate(steps)
        if playback_index is None or index > playback_index
    ):
        dependency_step_id = str(steps[-1].get("step_id") or "").strip()
        verify_step_id = _next_step_id(existing_ids, "verify_visual_state")
        verify_step = {
            "step_id": verify_step_id,
            "title": "再生状態を確認",
            "description": "UIの再生インジケーターや波形を読み取り、必要ならスクリーンショットも残して再生状態を確認する。",
            "depends_on": [dependency_step_id] if dependency_step_id else [],
            "capabilities": [
                {"name": "desktop.ax.find", "mode": "read"},
                {"name": "desktop.wait.element", "mode": "read"},
                {"name": "desktop.ax.snapshot", "mode": "read"},
                {"name": "desktop.view.screenshot", "mode": "read"},
            ],
            "expected_outputs": ["再生中であることの視覚的証拠が取得できていること"],
            "retryable": True,
        }
        steps.append(verify_step)

    if _plan_text_has_playback_hint(plan, goal):
        for step in reversed(steps):
            if not _step_has_playback_hint(step):
                continue
            if not _step_has_capability(step, "desktop.control.click"):
                continue
            _ensure_step_capability(step, "desktop.control.hotkey")
            description = str(step.get("description") or "")
            lowered = description.lower()
            if "スペースキー" not in description and "space" not in lowered:
                step["description"] = (
                    f"{description} 必要ならスペースキーなどのホットキーで再生開始も試みる。".strip()
                )
            break

    plan["steps"] = steps


def _plan_text_chunks(plan: dict, goal: str) -> list[str]:
    chunks: list[str] = [str(goal or plan.get("goal") or "")]
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        chunks.append(str(step.get("title") or ""))
        chunks.append(str(step.get("description") or ""))
        expected = step.get("expected_outputs", [])
        if isinstance(expected, list):
            chunks.extend(str(item) for item in expected)
    for criterion in plan.get("success_criteria", []):
        if not isinstance(criterion, dict):
            continue
        chunks.append(str(criterion.get("description") or ""))
    return chunks


def _plan_text_has_hotkey_hint(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _HOTKEY_HINT_KEYWORDS)


def _plan_text_has_playback_hint(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _PLAYBACK_HINT_KEYWORDS)


def _plan_needs_visual_evidence_capture(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    if _contains_any(haystack, _VISUAL_EVIDENCE_KEYWORDS | _PLAYBACK_HINT_KEYWORDS):
        return True
    for criterion in plan.get("success_criteria", []):
        if not isinstance(criterion, dict):
            continue
        if str(criterion.get("criterion_type") or "") in {"evidence", "custom"}:
            return True
    return False


def _step_has_playback_hint(step: dict[str, object]) -> bool:
    chunks = [str(step.get("title") or ""), str(step.get("description") or "")]
    expected = step.get("expected_outputs", [])
    if isinstance(expected, list):
        chunks.extend(str(item) for item in expected)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _PLAYBACK_HINT_KEYWORDS)


def _step_is_playback_action_step(step: dict[str, object]) -> bool:
    chunks = [str(step.get("title") or ""), str(step.get("description") or "")]
    expected = step.get("expected_outputs", [])
    if isinstance(expected, list):
        chunks.extend(str(item) for item in expected)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _PLAYBACK_ACTION_STEP_KEYWORDS)


def _normalize_required_capabilities(plan: dict, goal: str) -> dict:
    _normalize_desktop_plan_steps(plan, goal)
    required_caps = [
        cap if isinstance(cap, dict) else {"name": str(cap)}
        for cap in plan.get("required_capabilities", [])
    ]
    for step_capability_name in _step_capability_names(plan):
        _ensure_capability(required_caps, step_capability_name)
    normalized_goal = (goal or plan.get("goal") or "").strip().lower()
    is_current_browser_goal = _targets_current_browser(normalized_goal)

    if is_current_browser_goal:
        required_caps = [
            cap
            for cap in required_caps
            if str(cap.get("name", "")) != "desktop.control.launch_app"
        ]
    cap_names = {str(cap.get("name", "")) for cap in required_caps}

    if is_current_browser_goal:
        # Current-browser tasks should reuse the browser the user already has
        # open, so we remove launch-app and expand the read/focus capabilities
        # needed to verify and steer that existing window safely.
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
            _ensure_capability(required_caps, "desktop.view.frontmost_app")
            _ensure_capability(required_caps, "desktop.control.focus_window")
            _ensure_capability(required_caps, "desktop.control.click")
            _ensure_capability(required_caps, "desktop.control.hotkey")
            _ensure_capability(required_caps, "desktop.control.scroll")
            _ensure_capability(required_caps, "desktop.ax.find")
            _ensure_capability(required_caps, "desktop.wait.element")
            _ensure_capability(required_caps, "desktop.view.screenshot")
            _ensure_capability(required_caps, "desktop.ax.snapshot")

            if _needs_text_entry(normalized_goal):
                _ensure_capability(required_caps, "desktop.control.type")
    else:
        has_desktop_ui_plan = bool(
            cap_names
            & {
                "desktop.ax.find",
                "desktop.wait.element",
                "desktop.control.click",
                "desktop.control.type",
                "desktop.control.drag",
                "desktop.control.hotkey",
                "desktop.control.scroll",
            }
        )
        if cap_names & {"desktop.control.launch_app", "desktop.control.focus_window"}:
            _ensure_capability(required_caps, "desktop.view.windows")
            _ensure_capability(required_caps, "desktop.wait.window")
        if "desktop.control.launch_app" in cap_names and has_desktop_ui_plan:
            _ensure_capability(required_caps, "desktop.control.focus_window")
        if has_desktop_ui_plan:
            _ensure_capability(required_caps, "desktop.ax.find")
            _ensure_capability(required_caps, "desktop.wait.element")
            _ensure_capability(required_caps, "desktop.ax.snapshot")
        if _plan_needs_visual_evidence_capture(plan, normalized_goal):
            _ensure_capability(required_caps, "desktop.view.screenshot")
        if _plan_text_has_hotkey_hint(plan, normalized_goal) or _plan_text_has_playback_hint(
            plan, normalized_goal
        ):
            _ensure_capability(required_caps, "desktop.control.hotkey")
        if _plan_text_has_playback_hint(plan, normalized_goal) and (
            "desktop.control.focus_window" in cap_names
            or "desktop.wait.window" in cap_names
            or "desktop.view.windows" in cap_names
        ):
            # Media-app transport tasks often begin with a focus step, but the
            # executor may still need to reopen the app when the window is gone
            # or hidden. Surface launch_app in approval so that fallback is
            # explicit instead of failing mid-run on an unapproved capability.
            _ensure_capability(required_caps, "desktop.control.launch_app")

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
