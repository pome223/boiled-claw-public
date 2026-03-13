"""
Root Workflow — boiled-claw v2

ADK Runner を中心に置き、
Planner → PolicyJudge (callback) → Executor → Verifier → Repair (callback)
のループを Runner.run_async() で回す。

Runner が session.state / event history / output_key の保存を管理する。
Session への直接書き込みは行わない。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.genai.types import Content, Part

from src.control_loop.callbacks import (
    curator_callback,
    policy_judge_callback,
    repair_callback,
)
from src.control_loop.constants import DEFAULT_MAX_REPAIR_ATTEMPTS
from src.control_loop.executor_agent import executor_agent
from src.control_loop.guarded_tools import (
    guarded_browser_extract_text,
    guarded_browser_navigate,
    guarded_memory_read,
    guarded_read_file,
    guarded_web_search,
    guarded_write_file,
    guarded_desktop_view_windows,
    guarded_desktop_view_frontmost_app,
    guarded_desktop_view_screenshot,
    guarded_desktop_ax_snapshot,
    guarded_desktop_control_click,
    guarded_desktop_control_type,
    guarded_desktop_control_hotkey,
    guarded_desktop_control_drag,
)
from src.control_loop.planner_agent import planner_agent
from src.control_loop.verifier_agent import verifier_agent
from src.runtime.state_keys import StateKeys

logger = logging.getLogger(__name__)

_APP_NAME = "boiled_claw_v2"
_MAX_REPAIR_ATTEMPTS = DEFAULT_MAX_REPAIR_ATTEMPTS
_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}
_CONTROL_LOOP_AUTHOR = "control_loop"


# ── Callback helpers ───────────────────────────────────────────────────────

def _chain_after_callbacks(
    *cbs: Callable[[CallbackContext, Content], Optional[Content]],
) -> Callable[[CallbackContext, Content], Optional[Content]]:
    """複数の after_agent_callback を順番に呼ぶ合成関数を返す。"""
    def chained(ctx: CallbackContext, response: Content) -> Optional[Content]:
        for cb in cbs:
            cb(ctx, response)
        return None
    return chained


# ── Agents with callbacks ──────────────────────────────────────────────────

# Planner + PolicyJudge (after callback)
planner_with_policy = LlmAgent(
    name="planner",
    model="gemini-3-flash-preview",
    instruction=planner_agent.instruction,
    output_key=StateKeys.TEMP_PLANNER_DRAFT,
    after_agent_callback=policy_judge_callback,
    description="Produces a structured plan, then auto-evaluates via policy_judge_callback.",
)

# Verifier + Repair + Curator (chained after callbacks)
verifier_with_hooks = LlmAgent(
    name="verifier",
    model="gemini-3-flash-preview",
    instruction=verifier_agent.instruction,
    output_key=StateKeys.VERIFY_LAST_REPORT,
    after_agent_callback=_chain_after_callbacks(repair_callback, curator_callback),
    description=(
        "Evaluates execution results, then triggers repair or memory curation "
        "via chained after_agent_callbacks."
    ),
)

# Executor (with guarded tools, no callbacks)
executor_with_tools = LlmAgent(
    name="executor",
    model="gemini-3-flash-preview",
    instruction=executor_agent.instruction,
    tools=[
        guarded_web_search,
        guarded_read_file,
        guarded_write_file,
        guarded_memory_read,
        guarded_browser_navigate,
        guarded_browser_extract_text,
        guarded_desktop_view_windows,
        guarded_desktop_view_frontmost_app,
        guarded_desktop_view_screenshot,
        guarded_desktop_ax_snapshot,
        guarded_desktop_control_click,
        guarded_desktop_control_type,
        guarded_desktop_control_hotkey,
        guarded_desktop_control_drag,
    ],
    output_key=StateKeys.TEMP_EXECUTOR_OUTPUTS,
    description="Executes the approved plan using policy-gated tools.",
)


# ── Execution result ───────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """制御ループの最終実行結果。"""

    request_id: str
    session_id: str
    user_id: str
    final_text: str
    plan_id: str | None = None
    verification_report_id: str | None = None
    promoted_memory_ids: list[str] = field(default_factory=list)
    success: bool = False
    repair_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Control Loop ───────────────────────────────────────────────────────────

class ControlLoop:
    """
    ADK Runner を使って Planner → Executor → Verifier のループを実行する。

    session_service: 外部から注入可能（デフォルトは InMemorySessionService）。
    """

    def __init__(
        self,
        session_service=None,
        memory_service=None,
        max_repair_attempts: int = _MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self._session_service = session_service or InMemorySessionService()
        if memory_service is None:
            from src.memory_lifecycle.adk_memory_service import (
                get_promoted_memory_service,
            )

            memory_service = get_promoted_memory_service()
        self._memory_service = memory_service
        self._max_repair = max_repair_attempts

    async def run(
        self,
        goal: str,
        user_id: str,
        *,
        constraints: list[str] | None = None,
        session_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """
        制御ループを実行して ExecutionResult を返す。

        1. session 作成（または再利用）
        2. task:goal 等を initial state に設定
        3. repair 上限まで Planner → Executor → Verifier を反復
        4. ExecutionResult を返す
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"

        init_state: dict[str, Any] = {
            StateKeys.TASK_GOAL: goal,
            StateKeys.TASK_CONSTRAINTS: constraints or [],
            StateKeys.REPAIR_COUNT: 0,
            **(initial_state or {}),
        }
        session, created = await self._get_or_create_session(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            init_state=init_state,
        )
        session_id = session.id

        result = ExecutionResult(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            final_text="",
        )
        result.metadata["session_created"] = created

        for attempt in range(self._max_repair + 1):
            logger.info(
                "ControlLoop: attempt=%d, session=%s", attempt, session_id
            )

            state = await self._get_state(user_id, session_id)
            approval = state.get(StateKeys.APPROVAL_STATUS, "")
            has_approved_plan = bool(state.get(StateKeys.PLAN_APPROVED))
            resume_existing_plan = (
                attempt == 0
                and has_approved_plan
                and approval in _APPROVED_STATUSES
            )

            if not resume_existing_plan:
                # ── Step 1: Planner + PolicyJudge callback ─────────────────
                plan_message = (
                    goal
                    if attempt == 0
                    else f"[repair attempt {attempt}] {goal}"
                )
                await self._run_agent(
                    planner_with_policy,
                    session_id=session_id,
                    user_id=user_id,
                    message=plan_message,
                )

                # approval:status を確認
                state = await self._get_state(user_id, session_id)
                approval = state.get(StateKeys.APPROVAL_STATUS, "")
            else:
                logger.info(
                    "ControlLoop: resuming approved plan for session=%s", session_id
                )

            if approval == "denied":
                result.final_text = "Plan was denied by policy judge."
                result.success = False
                result.plan_id = _extract_plan_id(state)
                break

            if approval == "needs_human":
                result.final_text = (
                    "Plan requires human approval. "
                    "Please review plan:approved in session state."
                )
                result.success = False
                result.plan_id = _extract_plan_id(state)
                result.metadata["needs_human"] = True
                result.metadata["approval_request"] = state.get(
                    StateKeys.APPROVAL_REQUEST
                )
                break

            # ── Step 2: Executor ───────────────────────────────────────────
            await self._run_agent(
                executor_with_tools,
                session_id=session_id,
                user_id=user_id,
                message="Execute the approved plan.",
            )

            # ── Step 3: Verifier + Repair/Curator callbacks ────────────────
            await self._run_agent(
                verifier_with_hooks,
                session_id=session_id,
                user_id=user_id,
                message="Verify execution results.",
            )

            # verify:last_report を確認
            state = await self._get_state(user_id, session_id)
            raw_report = state.get(StateKeys.VERIFY_LAST_REPORT)
            report = _parse_json(raw_report) or {}
            verify_status = report.get("status", "error")

            result.repair_count = state.get(StateKeys.REPAIR_COUNT, 0)
            result.plan_id = _extract_plan_id(state)
            result.verification_report_id = report.get("report_id")
            candidate_ids = state.get(StateKeys.MEMORY_LAST_CANDIDATE_IDS, [])
            if candidate_ids:
                result.metadata["memory_candidate_ids"] = candidate_ids

            if verify_status == "pass":
                result.promoted_memory_ids = await self._promote_memories(
                    user_id=user_id,
                    session_id=session_id,
                )
                result.success = True
                result.final_text = _build_final_text(state, report)
                break

            # fail / partial_pass → repair_callback が repair:count を更新済み
            repair_patch = state.get(StateKeys.TEMP_REPAIR_PATCH)
            if not repair_patch or result.repair_count >= self._max_repair:
                result.success = False
                result.final_text = (
                    f"Verification failed after {result.repair_count} repair attempt(s). "
                    f"Status: {verify_status}."
                )
                break

            logger.info(
                "ControlLoop: repair triggered (attempt=%d)", result.repair_count
            )

        return result

    async def _run_agent(
        self,
        agent: LlmAgent,
        *,
        session_id: str,
        user_id: str,
        message: str,
    ) -> None:
        """指定 agent を Runner 経由で一度実行する。"""
        runner = Runner(
            agent=agent,
            app_name=_APP_NAME,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )
        user_content = Content(role="user", parts=[Part(text=message)])
        async for _event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_content,
        ):
            pass  # 結果は session.state の output_key 経由で取得

    async def resolve_human_approval(
        self,
        *,
        user_id: str,
        session_id: str,
        approved: bool,
        request_id: str | None = None,
    ) -> bool:
        """Record a human approval decision via ADK state_delta."""
        session = await self._get_session(user_id, session_id)
        if session is None or not session.state.get(StateKeys.PLAN_APPROVED):
            return False
        pending_request = session.state.get(StateKeys.APPROVAL_REQUEST) or {}
        if request_id and pending_request.get("request_id") != request_id:
            return False

        await self._append_state_delta(
            session=session,
            author=_CONTROL_LOOP_AUTHOR,
            invocation_prefix="approval",
            state_delta={
                StateKeys.APPROVAL_STATUS: (
                    "human_approved" if approved else "denied"
                ),
                StateKeys.APPROVAL_REQUEST: None,
            },
        )
        return True

    async def get_pending_approval(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        session = await self._get_session(user_id, session_id)
        if session is None:
            return None
        approval = session.state.get(StateKeys.APPROVAL_STATUS)
        request = session.state.get(StateKeys.APPROVAL_REQUEST)
        if approval != "needs_human" or not isinstance(request, dict):
            return None
        return request

    async def _get_or_create_session(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        init_state: dict[str, Any],
    ) -> tuple[Session, bool]:
        session = await self._get_session(user_id, session_id)
        if session is None:
            session = await self._session_service.create_session(
                app_name=_APP_NAME,
                user_id=user_id,
                session_id=session_id,
                state=init_state,
            )
            return session, True

        current_goal = session.state.get(StateKeys.TASK_GOAL)
        if current_goal and current_goal != goal:
            raise ValueError(
                "Session already has a different task goal. "
                "Use a new session_id for a new workflow."
            )

        missing_state = {
            key: value
            for key, value in init_state.items()
            if key not in session.state
        }
        if missing_state:
            await self._append_state_delta(
                session=session,
                author=_CONTROL_LOOP_AUTHOR,
                invocation_prefix="bootstrap",
                state_delta=missing_state,
            )
            session = await self._get_session(user_id, session_id)
            assert session is not None

        return session, False

    async def _append_state_delta(
        self,
        *,
        session: Session,
        author: str,
        invocation_prefix: str,
        state_delta: dict[str, Any],
    ) -> None:
        """Persist state updates through ADK session events."""
        event = Event(
            invocation_id=f"{invocation_prefix}:{uuid.uuid4().hex[:12]}",
            author=author,
            actions=EventActions(state_delta=state_delta),
        )
        await self._session_service.append_event(session, event)

    async def _promote_memories(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[str]:
        """Curate session candidates and sync promoted memories to ADK memory."""
        from src.memory_lifecycle.candidate_store import get_candidate_store
        from src.memory_lifecycle.curator import Curator
        from src.memory_lifecycle.promoted_store import get_promoted_store

        store = get_candidate_store()
        existing_promoted = get_promoted_store().list_memories(
            app_name=_APP_NAME,
            user_id=user_id,
        )
        curation = await Curator(
            store,
            existing_promoted=existing_promoted,
        ).curate_session(
            session_id=session_id,
            user_id=user_id,
        )
        promoted_ids = curation.promoted_ids
        if not curation.persisted_memories:
            return []

        if hasattr(self._memory_service, "store_promoted_memories"):
            await self._memory_service.store_promoted_memories(
                app_name=_APP_NAME,
                memories=curation.persisted_memories,
            )

        session = await self._get_session(user_id, session_id)
        if session is not None:
            await self._append_state_delta(
                session=session,
                author=_CONTROL_LOOP_AUTHOR,
                invocation_prefix="memory_promotion",
                state_delta={StateKeys.MEMORY_LAST_PROMOTED_IDS: promoted_ids},
            )

        return promoted_ids

    async def _get_session(
        self, user_id: str, session_id: str
    ) -> Session | None:
        return await self._session_service.get_session(
            app_name=_APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )

    async def _get_state(self, user_id: str, session_id: str) -> dict[str, Any]:
        """session state を dict として返す。"""
        session = await self._get_session(user_id, session_id)
        return session.state if session and session.state else {}


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_json(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_plan_id(state: dict) -> str | None:
    plan = _parse_json(state.get(StateKeys.PLAN_APPROVED))
    return plan.get("plan_id") if plan else None


def _build_final_text(state: dict, report: dict) -> str:
    goal = state.get(StateKeys.TASK_GOAL, "")
    score = report.get("overall_score", 0.0)
    summary = report.get("summary", "")
    return (
        f"Task completed: {goal}\n"
        f"Score: {score:.2f}\n"
        f"{summary}"
    ).strip()


# ── Default singleton ──────────────────────────────────────────────────────

_default_loop: ControlLoop | None = None


def get_control_loop() -> ControlLoop:
    global _default_loop
    if _default_loop is None:
        _default_loop = ControlLoop()
    return _default_loop
