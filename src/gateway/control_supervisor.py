"""Opt-in long-running supervisor for repeated control-loop maintenance runs."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.control_loop.root_workflow import ExecutionResult
from src.tools.tasks import (
    append_task_event_record,
    create_task_record,
    update_task_record,
)

_SUPERVISOR_AGENT_NAME = "control_supervisor"


def build_maintenance_goal(objective: str) -> str:
    normalized = str(objective or "").strip()
    if not normalized:
        raise ValueError("objective is required")
    return (
        "Maintain the following long-running objective for the active session.\n"
        f"Objective: {normalized}\n\n"
        "Inspect the current state, keep the objective satisfied, and perform only the "
        "next minimal action required. If the objective already looks healthy, prefer "
        "verification over disruptive changes."
    )


RunControlLoopWithTaskFn = Callable[..., Awaitable[tuple[ExecutionResult, str]]]
EmitSessionEventFn = Callable[..., Awaitable[None]]
TaskCreateFn = Callable[..., dict[str, Any]]
TaskUpdateFn = Callable[..., dict[str, Any] | None]
TaskAppendEventFn = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class SupervisorStartResult:
    task: dict[str, Any]
    control_session_id: str
    max_iterations: int
    ends_at: float
    next_run_at: float


@dataclass
class _SupervisorHandle:
    task_id: str
    owner_session_id: str
    user_id: str
    stop_requested: asyncio.Event
    task: asyncio.Task[None]


class ControlLoopSupervisor:
    def __init__(
        self,
        *,
        run_control_loop_with_task: RunControlLoopWithTaskFn,
        emit_session_event: EmitSessionEventFn,
        create_task_record_fn: TaskCreateFn = create_task_record,
        update_task_record_fn: TaskUpdateFn = update_task_record,
        append_task_event_record_fn: TaskAppendEventFn = append_task_event_record,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._run_control_loop_with_task = run_control_loop_with_task
        self._emit_session_event = emit_session_event
        self._create_task_record = create_task_record_fn
        self._update_task_record = update_task_record_fn
        self._append_task_event_record = append_task_event_record_fn
        self._now = now_fn
        self._handles: dict[str, _SupervisorHandle] = {}

    async def start(
        self,
        *,
        user_id: str,
        owner_session_id: str,
        objective: str,
        constraints: list[str],
        duration_seconds: int,
        interval_seconds: int,
        source: str,
        maintenance_goal: Optional[str] = None,
        request_id: Optional[str] = None,
        max_iterations: Optional[int] = None,
    ) -> SupervisorStartResult:
        started_at = self._now()
        resolved_duration = max(1, int(duration_seconds))
        resolved_interval = max(0, int(interval_seconds))
        resolved_max_iterations = max_iterations or max(
            1,
            math.ceil(resolved_duration / max(resolved_interval, 1)),
        )
        control_session_id = f"ctrlsup_{uuid.uuid4().hex[:12]}"
        loop_goal = (
            str(maintenance_goal or "").strip()
            or build_maintenance_goal(objective)
        )
        ends_at = started_at + float(resolved_duration)
        task = self._create_task_record(
            kind="control_supervisor",
            title=objective,
            status="running",
            owner_session_id=owner_session_id,
            owner_user_id=user_id,
            artifacts={
                "supervisor": {
                    "objective": objective,
                    "loop_goal": loop_goal,
                    "constraints": list(constraints),
                    "duration_seconds": resolved_duration,
                    "interval_seconds": resolved_interval,
                    "control_session_id": control_session_id,
                    "started_at": started_at,
                    "ends_at": ends_at,
                    "max_iterations": resolved_max_iterations,
                },
                "progress": {
                    "iteration": 0,
                    "completed_iterations": 0,
                    "next_run_at": started_at,
                    "child_task_ids": [],
                    "last_child_task_id": None,
                    "last_result": None,
                    "stop_requested": False,
                },
            },
            metadata={
                "source": source,
                "request_id": request_id,
                "type": "control_supervisor",
                "control_session_id": control_session_id,
            },
        )
        task_id = str(task["task_id"])
        stop_requested = asyncio.Event()
        runner_task = asyncio.create_task(
            self._run_supervisor(
                task_id=task_id,
                owner_session_id=owner_session_id,
                user_id=user_id,
                objective=objective,
                loop_goal=loop_goal,
                constraints=list(constraints),
                control_session_id=control_session_id,
                interval_seconds=resolved_interval,
                max_iterations=resolved_max_iterations,
                ends_at=ends_at,
                stop_requested=stop_requested,
            ),
            name=f"control-supervisor:{task_id}",
        )
        self._handles[task_id] = _SupervisorHandle(
            task_id=task_id,
            owner_session_id=owner_session_id,
            user_id=user_id,
            stop_requested=stop_requested,
            task=runner_task,
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="accepted",
            message=(
                "Started long-running control supervisor "
                f"for {resolved_duration}s (interval {resolved_interval}s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return SupervisorStartResult(
            task=task,
            control_session_id=control_session_id,
            max_iterations=resolved_max_iterations,
            ends_at=ends_at,
            next_run_at=started_at,
        )

    async def request_stop(self, task_id: str) -> dict[str, Any] | None:
        handle = self._handles.get(task_id)
        if handle is None:
            return None
        handle.stop_requested.set()
        updated = self._update_task_record(
            task_id,
            artifacts={
                "progress": {
                    "stop_requested": True,
                    "stop_requested_at": self._now(),
                }
            },
            metadata={"stop_requested": True},
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_stop_requested",
            status="running",
            title="Stop requested",
            payload={
                "summary": "Graceful stop requested; the supervisor will stop after the current iteration.",
            },
        )
        await self._emit_session_event(
            handle.owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="stop_requested",
            message=(
                "Graceful stop requested; the supervisor will stop after the current iteration."
            ),
            user_id=handle.user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return updated

    async def shutdown(self) -> None:
        handles = list(self._handles.values())
        for handle in handles:
            handle.stop_requested.set()
        if handles:
            await asyncio.gather(
                *(handle.task for handle in handles),
                return_exceptions=True,
            )

    async def _run_supervisor(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        objective: str,
        loop_goal: str,
        constraints: list[str],
        control_session_id: str,
        interval_seconds: int,
        max_iterations: int,
        ends_at: float,
        stop_requested: asyncio.Event,
    ) -> None:
        child_task_ids: list[str] = []
        completed_iterations = 0
        self._append_task_event_record(
            task_id,
            event_type="supervisor_started",
            status="running",
            title="Supervisor started",
            payload={
                "summary": (
                    f"Maintaining objective for up to {max_iterations} iteration(s)."
                ),
                "supervisor": {
                    "control_session_id": control_session_id,
                    "max_iterations": max_iterations,
                    "ends_at": ends_at,
                },
            },
        )
        try:
            try:
                for iteration in range(1, max_iterations + 1):
                    if stop_requested.is_set():
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                    now = self._now()
                    if now >= ends_at and completed_iterations > 0:
                        break

                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "next_run_at": now,
                                "child_task_ids": child_task_ids,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_started",
                        status="running",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": f"Starting iteration {iteration}.",
                            "iteration": iteration,
                        },
                    )

                    result, child_task_id = await self._run_control_loop_with_task(
                        user_id=user_id,
                        session_id=control_session_id,
                        owner_session_id=owner_session_id,
                        goal=loop_goal,
                        constraints=constraints,
                        request_id=None,
                        source="supervisor",
                        preserve_control_ui_tab=False,
                        parent_task_id=task_id,
                        reset_if_terminal=False,
                    )
                    child_task_ids.append(child_task_id)
                    completed_iterations = iteration
                    result_summary = {
                        "success": result.success,
                        "final_text": result.final_text,
                        "plan_id": result.plan_id,
                        "verification_report_id": result.verification_report_id,
                        "needs_human": bool(result.metadata.get("needs_human")),
                        "child_task_id": child_task_id,
                    }
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "completed_iterations": completed_iterations,
                                "child_task_ids": child_task_ids,
                                "last_child_task_id": child_task_id,
                                "last_result": result_summary,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_completed",
                        status="completed" if result.success else "failed",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": result.final_text,
                            "iteration": iteration,
                            "child_task_id": child_task_id,
                            "result": result_summary,
                        },
                    )

                    if result.metadata.get("needs_human"):
                        await self._finish_blocked(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                        )
                        return
                    if not result.success:
                        await self._finish_failed(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                        )
                        return

                    if iteration >= max_iterations or self._now() >= ends_at:
                        break

                    next_run_at = min(ends_at, self._now() + float(interval_seconds))
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "next_run_at": next_run_at,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_waiting",
                        status="running",
                        title="Waiting for next iteration",
                        payload={
                            "summary": f"Waiting until the next interval before iteration {iteration + 1}.",
                            "iteration": iteration,
                            "next_run_at": next_run_at,
                        },
                    )
                    if await self._wait_for_stop_or_timeout(
                        stop_requested=stop_requested,
                        timeout_seconds=max(0.0, next_run_at - self._now()),
                    ):
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                self._update_task_record(
                    task_id,
                    status="completed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        }
                    },
                    metadata={"completed_iterations": completed_iterations},
                    error=None,
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_completed",
                    status="completed",
                    title="Supervisor completed",
                    payload={
                        "summary": (
                            f"Completed long-running supervision after {completed_iterations} successful iteration(s)."
                        ),
                        "completed_iterations": completed_iterations,
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="completed",
                    message=(
                        f"Long-running control supervisor completed after {completed_iterations} successful iteration(s)."
                    ),
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
            except Exception as exc:
                self._update_task_record(
                    task_id,
                    status="failed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        },
                        "result": {
                            "success": False,
                            "final_text": f"Supervisor crashed: {exc}",
                        },
                    },
                    error=f"supervisor_error:{exc}",
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_error",
                    status="failed",
                    title="Supervisor crashed",
                    payload={
                        "summary": f"Supervisor crashed: {exc}",
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="failed",
                    message=f"Long-running control supervisor crashed: {exc}",
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
                raise
        finally:
            self._handles.pop(task_id, None)

    async def _finish_blocked(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
    ) -> None:
        approval_request = result.metadata.get("approval_request")
        self._update_task_record(
            task_id,
            status="pending",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "approval_request": approval_request,
                },
            },
            metadata={
                "needs_human": True,
                "blocking_child_task_id": child_task_id,
            },
            error="supervisor_needs_human",
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_blocked",
            status="pending",
            title="Supervisor blocked",
            payload={
                "summary": "Supervisor stopped because a child control-loop task requires human approval.",
                "child_task_id": child_task_id,
                "approval_request": approval_request,
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="blocked",
            message=(
                "Supervisor stopped because a child control-loop task requires human approval."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_failed(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
    ) -> None:
        self._update_task_record(
            task_id,
            status="failed",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                },
            },
            metadata={"blocking_child_task_id": child_task_id},
            error=result.final_text or "control supervisor failed",
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_failed",
            status="failed",
            title="Supervisor failed",
            payload={
                "summary": result.final_text,
                "child_task_id": child_task_id,
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="failed",
            message=result.final_text or "Long-running control supervisor failed.",
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_cancelled(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
    ) -> None:
        self._update_task_record(
            task_id,
            status="cancelled",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "next_run_at": None,
                    "stop_requested": True,
                }
            },
            metadata={"stop_requested": True},
            error=None,
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_cancelled",
            status="cancelled",
            title="Supervisor stopped",
            payload={
                "summary": (
                    f"Supervisor stopped after {completed_iterations} completed iteration(s)."
                ),
                "completed_iterations": completed_iterations,
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="cancelled",
            message=(
                f"Long-running control supervisor stopped after {completed_iterations} completed iteration(s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _wait_for_stop_or_timeout(
        self,
        *,
        stop_requested: asyncio.Event,
        timeout_seconds: float,
    ) -> bool:
        if stop_requested.is_set():
            return True
        if timeout_seconds <= 0:
            return stop_requested.is_set()
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            return False
