#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.request


GATEWAY = "http://127.0.0.1:18789"
USER_ID = "skill_dynamic_user"
MESSAGE = (
    'sessions_spawn_dynamic でエージェントを起動して。'
    'instruction="あなたは計算エージェントです"、'
    'mcp_servers=[{"type":"sse","url":"http://boiled-claw-mcp-sample:8765/sse"}]、'
    'task="100 + 200 を計算して"'
)


def _request(url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    start = _request(
        f"{GATEWAY}/agent/run",
        {"user_id": USER_ID, "message": MESSAGE},
    )
    session_id = start.get("session_id")
    if not session_id:
        raise SystemExit(f"dynamic agent did not return a session id: {start}")

    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        last = _request(f"{GATEWAY}/subagents/{session_id}")
        runs = last.get("runs") or []
        if runs and runs[0].get("status") == "completed":
            print(json.dumps(last, ensure_ascii=False, indent=2))
            return
        time.sleep(1)

    raise SystemExit(f"dynamic agent did not complete in time: {json.dumps(last, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
