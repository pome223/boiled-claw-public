"""Physical AI adapters for simulation-first validation flows."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import httpx
from google.adk.agents.context import Context as ToolContext

from src.config.settings import get_settings
from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context

_validation_runs: dict[str, dict[str, Any]] = {}


def reset_physical_ai_validation_runs() -> None:
    _validation_runs.clear()


def _adapter_url(adapter: str) -> str | None:
    settings = get_settings()
    if adapter == "isaac_sim":
        return settings.physical_ai_isaac_sim_url
    if adapter == "osmo":
        return settings.physical_ai_osmo_url
    return None


async def _post_adapter_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.physical_ai_timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def _record_validation_run(run: dict[str, Any]) -> None:
    _validation_runs[run["run_id"]] = run


def _validation_status_payload(run_id: str) -> dict[str, Any] | None:
    return _validation_runs.get(run_id)


def _ros2_topics(namespace: str, action_name: str) -> dict[str, str]:
    prefix = f"/{namespace.strip('/')}/{action_name.strip('/')}".replace("//", "/")
    return {
        "send_goal": f"{prefix}/_action/send_goal",
        "feedback": f"{prefix}/_action/feedback",
        "get_result": f"{prefix}/_action/get_result",
        "cancel_goal": f"{prefix}/_action/cancel_goal",
        "status": f"{prefix}/_action/status",
    }


async def physical_ai_submit_simulation(
    adapter: str,
    workflow: str,
    scenario: str,
    robot: Optional[str] = None,
    task: Optional[str] = None,
    parameters_json: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Submit an Isaac Sim or OSMO simulation run for validation."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    adapter_name = (adapter or "").strip().lower()
    url = _adapter_url(adapter_name)
    if adapter_name not in {"isaac_sim", "osmo"}:
        return {"success": False, "error": "adapter must be isaac_sim or osmo"}
    if not url:
        return {"success": False, "error": f"{adapter_name} adapter URL is not configured"}

    parameters = json.loads(parameters_json) if parameters_json else {}
    request = {
        "workflow": workflow,
        "scenario": scenario,
        "robot": robot,
        "task": task,
        "parameters": parameters,
        "validation_mode": "simulation_first",
    }
    response = await _post_adapter_json(url, request)
    run_id = str(response.get("run_id") or response.get("id") or f"sim-{uuid.uuid4().hex[:12]}")
    status = str(response.get("status") or "queued")
    validated = bool(response.get("validated")) or status in {"pass", "validated", "ready"}
    payload = {
        "success": True,
        "adapter": adapter_name,
        "run_id": run_id,
        "status": status,
        "validated": validated,
        "response": response,
    }
    _record_validation_run(
        {
            **payload,
            "workflow": workflow,
            "scenario": scenario,
            "robot": robot,
            "task": task,
            "created_at": time.time(),
        }
    )
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="physical_ai_submit_simulation",
        resource=run_id,
        result=status,
        metadata={"adapter": adapter_name, "validated": validated, "workflow": workflow},
    )
    return payload


async def physical_ai_build_ros2_action(
    robot_namespace: str,
    action_name: str,
    action_type: str,
    goal_json: str,
    frame_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a ROS2-friendly action envelope for downstream adapters."""

    goal = json.loads(goal_json)
    action = {
        "namespace": robot_namespace.strip("/") or "robot",
        "action_name": action_name.strip("/"),
        "action_type": action_type,
        "frame_id": frame_id,
        "goal": goal,
    }
    action["topics"] = _ros2_topics(action["namespace"], action["action_name"])
    return {
        "success": True,
        "simulation_first_required": True,
        "ros2_action": action,
    }


async def physical_ai_dispatch_ros2_action(
    validation_run_id: str,
    ros2_action_json: str,
    allow_real_hardware: bool = False,
    dry_run: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Dispatch a ROS2 action only after a validated simulation run exists."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    validation = _validation_status_payload(validation_run_id)
    if validation is None:
        return {"success": False, "error": f"Unknown validation run: {validation_run_id}"}
    if not validation.get("validated"):
        return {
            "success": False,
            "error": f"Simulation-first validation has not passed for run {validation_run_id}",
            "validation": validation,
        }

    ros2_action = json.loads(ros2_action_json)
    dispatch_payload = {
        "validation_run_id": validation_run_id,
        "validation": validation,
        "ros2_action": ros2_action,
    }
    if dry_run or not allow_real_hardware:
        return {
            "success": True,
            "dispatched": False,
            "dry_run": True,
            "dispatch_payload": dispatch_payload,
        }

    settings = get_settings()
    if not settings.physical_ai_ros2_bridge_url:
        return {"success": False, "error": "physical_ai_ros2_bridge_url is not configured"}

    response = await _post_adapter_json(settings.physical_ai_ros2_bridge_url, dispatch_payload)
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="physical_ai_dispatch_ros2_action",
        resource=validation_run_id,
        result="success",
        metadata={"action_name": ros2_action.get("action_name"), "namespace": ros2_action.get("namespace")},
    )
    return {
        "success": True,
        "dispatched": True,
        "dry_run": False,
        "response": response,
        "validation_run_id": validation_run_id,
    }
