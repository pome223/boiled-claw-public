from __future__ import annotations

from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.base_session_service import GetSessionConfig, State
from google.genai import types

from src.runtime import session_service as session_service_module


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str):
        self.values[key] = value

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)

    async def zadd(self, key: str, mapping: dict[str, float]):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrevrange(self, key: str, start: int, stop: int):
        entries = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )
        if stop == -1:
            sliced = entries[start:]
        else:
            sliced = entries[start : stop + 1]
        return [member for member, _score in sliced]

    async def zrem(self, key: str, *values: str):
        bucket = self.zsets.setdefault(key, {})
        for value in values:
            bucket.pop(value, None)


async def _append_stateful_event(service, session, *, text: str, timestamp: float):
    event = Event(
        invocation_id=f"invoke-{timestamp}",
        author="tester",
        content=types.Content(role="user", parts=[types.Part(text=text)]),
        actions=EventActions(
            state_delta={
                "demo": text,
                State.APP_PREFIX + "mode": "redis",
                State.USER_PREFIX + "theme": "dark",
            }
        ),
        timestamp=timestamp,
    )
    await service.append_event(session, event)
    return event


async def test_redis_session_service_round_trips_events_and_state():
    service = session_service_module.RedisSessionService(
        client=FakeRedisClient(),
        namespace="test:sessions",
    )
    session = await service.create_session(
        app_name="boiled-claw",
        user_id="alice",
        state={"local": "yes"},
        session_id="sess-1",
    )
    await _append_stateful_event(service, session, text="hello", timestamp=5.0)

    loaded = await service.get_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-1",
    )

    assert loaded is not None
    assert loaded.id == "sess-1"
    assert loaded.state["local"] == "yes"
    assert loaded.state["demo"] == "hello"
    assert loaded.state["app:mode"] == "redis"
    assert loaded.state["user:theme"] == "dark"
    assert len(loaded.events) == 1

    second = await service.create_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-2",
    )
    assert second.state["app:mode"] == "redis"
    assert second.state["user:theme"] == "dark"
    assert "demo" not in second.state


async def test_redis_session_service_lists_recent_sessions_without_state_or_events():
    service = session_service_module.RedisSessionService(
        client=FakeRedisClient(),
        namespace="test:sessions",
    )
    first = await service.create_session(app_name="boiled-claw", user_id="alice", session_id="sess-1")
    second = await service.create_session(app_name="boiled-claw", user_id="alice", session_id="sess-2")
    await _append_stateful_event(service, first, text="older", timestamp=10.0)
    await _append_stateful_event(service, second, text="newer", timestamp=20.0)

    listed = await service.list_sessions(app_name="boiled-claw", user_id="alice")
    limited = await service.get_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-2",
        config=GetSessionConfig(num_recent_events=1),
    )

    assert [session.id for session in listed.sessions] == ["sess-2", "sess-1"]
    assert listed.sessions[0].events == []
    assert listed.sessions[0].state == {}
    assert limited is not None
    assert len(limited.events) == 1
    assert limited.events[0].content.parts[0].text == "newer"


async def test_redis_session_service_delete_session_removes_it():
    service = session_service_module.RedisSessionService(
        client=FakeRedisClient(),
        namespace="test:sessions",
    )
    await service.create_session(app_name="boiled-claw", user_id="alice", session_id="sess-1")

    await service.delete_session(app_name="boiled-claw", user_id="alice", session_id="sess-1")

    loaded = await service.get_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-1",
    )
    assert loaded is None


async def test_redis_session_service_recovers_when_stored_session_is_missing():
    service = session_service_module.RedisSessionService(
        client=FakeRedisClient(),
        namespace="test:sessions",
    )
    session = await service.create_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-1",
    )
    await service._client.delete(service._session_key("boiled-claw", "alice", "sess-1"))

    await _append_stateful_event(service, session, text="hello", timestamp=5.0)

    loaded = await service.get_session(
        app_name="boiled-claw",
        user_id="alice",
        session_id="sess-1",
    )
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0].content.parts[0].text == "hello"


def test_create_session_service_returns_inmemory_without_redis_url():
    settings = type(
        "Settings",
        (),
        {"redis_url": None, "redis_session_namespace": "boiled-claw:sessions"},
    )()

    service = session_service_module.create_session_service(settings)

    assert isinstance(service, InMemorySessionService)


def test_create_session_service_returns_redis_when_configured(monkeypatch):
    fake_client = FakeRedisClient()
    settings = type(
        "Settings",
        (),
        {"redis_url": "redis://localhost:6379/0", "redis_session_namespace": "demo:sessions"},
    )()
    monkeypatch.setattr(session_service_module, "_create_redis_client", lambda url: fake_client)

    service = session_service_module.create_session_service(settings)

    assert isinstance(service, session_service_module.RedisSessionService)
    assert service._client is fake_client
