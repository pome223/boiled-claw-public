import asyncio

import pytest

from src.cron.scheduler import CronScheduler


@pytest.mark.asyncio
async def test_system_event_job_routes_to_triggering_session(tmp_path):
    scheduler = CronScheduler(tmp_path / "cron.db")
    calls = []

    async def fake_spawn(**kwargs):
        calls.append(kwargs)
        return {"run_id": "run-connect"}

    scheduler.set_spawn_fn(fake_spawn)
    scheduler.add_job(
        name="on-connect",
        cron_expr="",
        task="summarize latest updates",
        delivery_target="main",
        system_event="connect",
    )

    fired = await scheduler.fire_system_event("connect", {"session_id": "sess-main"})
    assert fired == 1

    await asyncio.sleep(0.05)
    assert calls
    assert calls[0]["requester_session_id"] == "sess-main"
    assert calls[0]["agent_name"] == "web_researcher"


@pytest.mark.asyncio
async def test_bound_session_delivery_uses_explicit_session_target(tmp_path):
    scheduler = CronScheduler(tmp_path / "cron.db")
    calls = []

    async def fake_spawn(**kwargs):
        calls.append(kwargs)
        return {"run_id": "run-bound"}

    scheduler.set_spawn_fn(fake_spawn)
    job = scheduler.add_job(
        name="bound-session",
        cron_expr="0 9 * * *",
        task="deliver to bound session",
        delivery_target="session:session-42",
    )

    await scheduler._run_job(job)

    assert calls
    assert calls[0]["requester_session_id"] == "session-42"
