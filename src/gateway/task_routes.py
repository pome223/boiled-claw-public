from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from src.gateway.api_schema import (
    TaskCompareResponse,
    TaskEnvelope,
    TaskQueryResponse,
    TaskReplayAcceptedResponse,
    TaskReplayRequest,
    TaskTimelineResponse,
)
from src.gateway.route_utils import normalize_constraints
from src.gateway.task_replay import build_partial_replay_seed, build_task_compare_payload

if TYPE_CHECKING:
    from src.gateway.server import GatewayServer


def build_task_router(server: "GatewayServer") -> APIRouter:
    router = APIRouter(tags=["tasks"])

    @router.get("/tasks", response_model=TaskQueryResponse)
    async def task_list_endpoint(
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: Optional[int] = None,
        limit: int = 20,
    ):
        resolved_page_size = max(1, min(int(page_size or limit or 20), 100))
        return server.task_store.query(
            owner_session_id=session_id,
            kind=kind,
            status=status,
            parent_task_id=parent_task_id,
            q=q,
            page=page,
            page_size=resolved_page_size,
        )

    @router.get("/tasks/{task_id}", response_model=TaskEnvelope)
    async def task_get_endpoint(task_id: str):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return {"task": task}

    @router.get("/tasks/{task_id}/timeline", response_model=TaskTimelineResponse)
    async def task_timeline_endpoint(
        task_id: str,
        page: int = 1,
        page_size: Optional[int] = None,
        limit: int = 50,
    ):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        resolved_page_size = max(1, min(int(page_size or limit or 50), 200))
        return server._build_task_timeline_payload(
            task,
            page=page,
            page_size=resolved_page_size,
        )

    @router.post("/tasks/{task_id}/replay", response_model=TaskReplayAcceptedResponse)
    async def task_replay_endpoint(
        request: Request,
        task_id: str,
        replay_request: TaskReplayRequest | None = Body(default=None),
    ):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if str(task.get("kind") or "") != "control_loop":
            raise HTTPException(status_code=400, detail="task replay currently supports control_loop tasks only")
        artifacts = task.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        resume_context = artifacts.get("resume_context")
        resume_context = resume_context if isinstance(resume_context, dict) else {}
        goal = str(resume_context.get("goal") or task.get("title") or "").strip()
        constraints = normalize_constraints(resume_context.get("constraints"))
        session_id = str(task.get("owner_session_id") or "").strip()
        user_id = str(task.get("owner_user_id") or "").strip()
        if not goal or not session_id or not user_id:
            raise HTTPException(status_code=400, detail="task is missing replay context")
        effective_user_id = server._resolve_http_user_id(
            request,
            user_id,
            default_user_id=user_id,
        )
        if effective_user_id != user_id:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        replay_request = replay_request or TaskReplayRequest()
        replay_from_step = str(replay_request.from_step or "").strip()
        replay_mode = "tail" if replay_from_step else "full"
        initial_state = None
        if replay_from_step:
            initial_state = build_partial_replay_seed(
                task,
                from_step=replay_from_step,
            )

        replay_task = server._create_control_loop_task_record(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=constraints,
            request_id=None,
            source="http",
            parent_task_id=task_id,
            replay_of_task_id=task_id,
            compare_to_task_id=task_id,
            replay_from_step=replay_from_step or None,
            replay_mode=replay_mode,
        )
        replay_task_id = str(replay_task["task_id"])
        await server._start_control_loop_run(
            session_id=session_id,
            user_id=user_id,
            goal=goal,
            constraints=constraints,
            task_id=replay_task_id,
            parent_task_id=task_id,
            replay_of_task_id=task_id,
            compare_to_task_id=task_id,
            initial_state=initial_state,
            reset_if_terminal=True,
        )
        return {
            "accepted": True,
            "task": replay_task,
            "replay_of_task_id": task_id,
            "compare_to_task_id": task_id,
            "replay_from_step": replay_from_step or None,
            "replay_mode": replay_mode,
        }

    @router.get("/tasks/{task_id}/compare", response_model=TaskCompareResponse)
    async def task_compare_endpoint(task_id: str, other_task_id: Optional[str] = None):
        left_task = server.task_store.get(task_id)
        if left_task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

        candidate_task_id = str(other_task_id or "").strip()
        if not candidate_task_id:
            candidate_task_id = str(left_task.get("parent_task_id") or "").strip()
        if not candidate_task_id:
            children = server.task_store.query(
                owner_session_id=left_task.get("owner_session_id"),
                parent_task_id=left_task.get("task_id"),
                page=1,
                page_size=1,
            )
            child_tasks = children.get("tasks")
            child_tasks = child_tasks if isinstance(child_tasks, list) else []
            if child_tasks:
                candidate_task_id = str(child_tasks[0].get("task_id") or "").strip()
        if not candidate_task_id:
            raise HTTPException(status_code=400, detail="comparison task could not be determined")

        right_task = server.task_store.get(candidate_task_id)
        if right_task is None:
            raise HTTPException(status_code=404, detail=f"comparison task not found: {candidate_task_id}")
        return build_task_compare_payload(
            left_task,
            right_task,
            build_task_timeline_payload=server._build_task_timeline_payload,
        )

    return router
