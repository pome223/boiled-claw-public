import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import uvicorn

from src.config.settings import reset_settings
from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import build_maintenance_goal
from src.gateway.server import create_gateway
from src.runtime.durable_execution_schema import (
    SchedulerQueueEntry,
    SchedulerQueueKind,
)
from src.runtime.mission_contract import build_mission_contract
from src.runtime.task_store import reset_task_store


pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


async def _start_gateway(
    run_control_loop_with_task: Callable[..., Awaitable[tuple[ExecutionResult, str]]],
) -> tuple[Any, asyncio.Task[None], str]:
    gateway = create_gateway()
    gateway.control_supervisor._run_control_loop_with_task = run_control_loop_with_task
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        gateway.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _wait_for_health(base_url)
    return server, task, base_url


async def _stop_gateway(server: Any, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10.0)


async def _wait_for_task_status(
    client: httpx.AsyncClient,
    task_id: str,
    *statuses: str,
) -> dict[str, Any]:
    expected = set(statuses)
    last_task: dict[str, Any] | None = None
    for _ in range(120):
        response = await client.get(f"/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()["task"]
        last_task = task
        if task["status"] in expected:
            return task
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Task {task_id} did not reach one of {sorted(expected)}; last={last_task}"
    )


@pytest.mark.asyncio
async def test_e2e_control_supervisor_resumes_multi_node_graph_after_gateway_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv(
        "COMPUTER_TRAJECTORY_DB_PATH",
        str(tmp_path / "computer_trajectories.db"),
    )
    monkeypatch.setenv(
        "PHYSICAL_AI_VALIDATION_DB_PATH",
        str(tmp_path / "physical_ai_validation.db"),
    )
    reset_settings()
    reset_task_store()

    calls: list[str] = []
    repair_started = asyncio.Event()

    async def _first_gateway_child(**kwargs):
        goal_first_line = kwargs["goal"].splitlines()[0]
        calls.append(f"first:{goal_first_line}")
        if goal_first_line == "Repair state":
            repair_started.set()
            await asyncio.Event().wait()
        task = kwargs["parent_task_id"] + f"/child-{len(calls)}"
        return (
            ExecutionResult(
                request_id=f"req-first-{len(calls)}",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok",
                success=True,
            ),
            task,
        )

    first_server, first_server_task, first_base_url = await _start_gateway(
        _first_gateway_child,
    )
    async with httpx.AsyncClient(base_url=first_base_url, timeout=10.0) as client:
        response = await client.post(
            "/tasks/supervisors/control-loop",
            json={
                "user_id": "e2e_supervisor",
                "mission_contract": {
                    "contract_id": "mission-e2e-multi-resume",
                    "objective": "Run staged browser health checks",
                    "allowed_actions": ["current_tab.info"],
                    "completion_criteria": ["each node passed"],
                    "evidence_requirements": ["child control loop result"],
                    "task_nodes": [
                        {
                            "node_id": "inspect",
                            "description": "Inspect state",
                            "completion_criteria": ["inspect passed"],
                        },
                        {
                            "node_id": "repair",
                            "description": "Repair state",
                            "depends_on": ["inspect"],
                        },
                        {
                            "node_id": "verify",
                            "description": "Verify state",
                            "depends_on": ["repair"],
                        },
                    ],
                },
                "duration_seconds": 60,
                "interval_seconds": 5,
            },
        )
        response.raise_for_status()
        accepted = response.json()
        task_id = accepted["task"]["task_id"]
        await asyncio.wait_for(repair_started.wait(), timeout=10.0)
        running_task = await _wait_for_task_status(client, task_id, "running")
        durable = running_task["artifacts"]["durable_execution"]
        assert [run["node_id"].split("/")[-1] for run in durable["job_runs"]] == [
            "inspect"
        ]
        assert durable["resume_state"]["next_actionable_task_node_id"].endswith(
            "/repair"
        )

    await _stop_gateway(first_server, first_server_task)

    async def _resumed_gateway_child(**kwargs):
        goal_first_line = kwargs["goal"].splitlines()[0]
        calls.append(f"resumed:{goal_first_line}")
        task = kwargs["parent_task_id"] + f"/resumed-child-{len(calls)}"
        return (
            ExecutionResult(
                request_id=f"req-resumed-{len(calls)}",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok after resume",
                success=True,
            ),
            task,
        )

    second_server, second_server_task, second_base_url = await _start_gateway(
        _resumed_gateway_child,
    )
    try:
        async with httpx.AsyncClient(base_url=second_base_url, timeout=10.0) as client:
            completed_task = await _wait_for_task_status(client, task_id, "completed")
            durable = completed_task["artifacts"]["durable_execution"]
            assert [run["node_id"].split("/")[-1] for run in durable["job_runs"]] == [
                "inspect",
                "repair",
                "verify",
            ]
            assert {node["status"] for node in durable["task_graph"]["nodes"]} == {
                "done"
            }
            assert durable["resume_state"]["reason"] == "graph_complete"
            assert durable["supervisor_health"]["active_node_id"].endswith("/verify")
            assert durable["mission_scorecard"]["objective_progress"] == "satisfied"
            assert completed_task["artifacts"]["mission_review"]["final_status"] == "completed"
            assert completed_task["artifacts"]["mission_review"]["schema_version"] == (
                "mission_review.v1"
            )
            assert completed_task["artifacts"]["progress"]["heartbeat"][
                "last_heartbeat_at"
            ]

            timeline_response = await client.get(
                f"/tasks/{task_id}/timeline",
                params={"limit": 120},
            )
            timeline_response.raise_for_status()
            event_types = [
                entry["event_type"] for entry in timeline_response.json()["entries"]
            ]
            assert "supervisor_shutdown_deferred" in event_types
            assert "supervisor_resumed" in event_types
            assert "scheduler_worker_decision" in event_types
            assert "supervisor_heartbeat" in event_types
            assert "post_mission_review_recorded" in event_types
            assert "supervisor_completed" in event_types
    finally:
        await _stop_gateway(second_server, second_server_task)

    assert calls == [
        "first:Inspect state",
        "first:Repair state",
        "resumed:Repair state",
        "resumed:Verify state",
    ]


@pytest.mark.asyncio
async def test_e2e_control_supervisor_startup_watchdog_and_stale_queue_skip(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv(
        "COMPUTER_TRAJECTORY_DB_PATH",
        str(tmp_path / "computer_trajectories.db"),
    )
    monkeypatch.setenv(
        "PHYSICAL_AI_VALIDATION_DB_PATH",
        str(tmp_path / "physical_ai_validation.db"),
    )
    reset_settings()
    reset_task_store()

    setup_gateway = create_gateway()
    seeded_now = time.time()
    mission_contract = build_mission_contract(
        contract_id="mission-e2e-stale-queue",
        objective="Resume stale scheduler queue safely",
        allowed_actions=["current_tab.info"],
        completion_criteria=["valid scheduler entry executed"],
    )
    loop_goal = build_maintenance_goal(mission_contract.objective, mission_contract)
    runtime_state = setup_gateway.control_supervisor._initial_runtime_state(
        objective=mission_contract.objective,
        loop_goal=loop_goal,
        control_session_id="ctrlsup_e2e_stale",
        created_at=seeded_now,
        next_run_at=seeded_now,
        mission_contract=mission_contract,
    )
    runtime_state["queue_state"].ready_queue.insert(
        0,
        SchedulerQueueEntry(
            entry_id="stale-entry",
            node_id="missing-node",
            queue=SchedulerQueueKind.READY,
            metadata={"expires_at": "1970-01-01T00:00:02+00:00"},
        ),
    )
    durable_execution = setup_gateway.control_supervisor._serialize_runtime_state(
        runtime_state,
        objective=mission_contract.objective,
    )
    seeded_task = setup_gateway.task_store.create(
        kind="control_supervisor",
        title=mission_contract.objective,
        status="running",
        owner_session_id="sess-e2e-stale",
        owner_user_id="e2e_supervisor",
        artifacts={
            "mission_contract": mission_contract.model_dump(mode="json"),
            "supervisor": {
                "objective": mission_contract.objective,
                "loop_goal": loop_goal,
                "constraints": [],
                "mission_contract_id": mission_contract.contract_id,
                "duration_seconds": 60,
                "interval_seconds": 5,
                "control_session_id": "ctrlsup_e2e_stale",
                "started_at": seeded_now,
                "ends_at": seeded_now + 60,
                "max_iterations": 1,
            },
            "progress": {
                "iteration": 0,
                "completed_iterations": 0,
                "next_run_at": seeded_now,
                "child_task_ids": [],
                "stop_requested": False,
                "heartbeat": {
                    "last_heartbeat_at": 1.0,
                    "status": "running",
                    "reason": "seeded_stale_heartbeat",
                },
            },
            "durable_execution": durable_execution,
        },
        metadata={
            "type": "control_supervisor",
            "control_session_id": "ctrlsup_e2e_stale",
            "mission_contract_id": mission_contract.contract_id,
        },
    )

    calls: list[str] = []

    async def _resumed_gateway_child(**kwargs):
        calls.append(kwargs["goal"].splitlines()[0])
        task = kwargs["parent_task_id"] + "/resumed-valid-child"
        return (
            ExecutionResult(
                request_id="req-stale-resume",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="valid entry executed",
                success=True,
            ),
            task,
        )

    server, server_task, base_url = await _start_gateway(_resumed_gateway_child)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            completed_task = await _wait_for_task_status(
                client,
                seeded_task["task_id"],
                "completed",
            )
            assert completed_task["artifacts"]["progress"]["watchdog"][
                "reason"
            ] == "stale_heartbeat"
            durable = completed_task["artifacts"]["durable_execution"]
            assert durable["scheduler_state"]["completed_queue"][0]["node_id"].endswith(
                "/maintain-objective"
            )

            timeline_response = await client.get(
                f"/tasks/{seeded_task['task_id']}/timeline",
                params={"limit": 120},
            )
            timeline_response.raise_for_status()
            event_types = [
                entry["event_type"] for entry in timeline_response.json()["entries"]
            ]
            assert "supervisor_watchdog_warning" in event_types
            assert "supervisor_resumed" in event_types
            assert "scheduler_worker_stale_entry" in event_types
            assert "scheduler_worker_decision" in event_types
    finally:
        await _stop_gateway(server, server_task)

    assert calls == [loop_goal.splitlines()[0]]
