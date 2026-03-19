#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable

import websockets


def _extract_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts).strip()


async def run(args: argparse.Namespace) -> dict:
    url = f"{args.gateway.rstrip('/')}/ws/{args.user_id}"
    tool_events: list[dict] = []
    approvals: list[str] = []
    final_text = ""
    connected = None

    async with websockets.connect(url, max_size=10_000_000) as websocket:
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
            message = json.loads(raw)
            event_type = message.get("type")

            if event_type == "connected":
                connected = message
                break

        await websocket.send(json.dumps({
            "type": "chat.send",
            "message": args.message,
        }))

        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
            message = json.loads(raw)
            event_type = message.get("type")

            if event_type == "tools.approval_request":
                approvals.append(str(message.get("tool_name") or ""))
                await websocket.send(json.dumps({
                    "type": "tools.approval_response",
                    "request_id": message["request_id"],
                    "approved": True,
                }))
                continue

            if event_type in {"tool.start", "tool.result"}:
                tool_events.append({
                    "type": event_type,
                    "tool_name": message.get("tool_name"),
                    "ok": message.get("ok"),
                    "error": message.get("error"),
                })
                continue

            if event_type == "chat.done":
                final_text = _extract_text(message)
                return {
                    "connected": connected,
                    "approvals": approvals,
                    "tool_events": tool_events,
                    "final_text": final_text,
                }


def _has_any_tool(events: Iterable[dict], prefixes: list[str]) -> bool:
    for event in events:
        name = str(event.get("tool_name") or "")
        if any(name.startswith(prefix) for prefix in prefixes):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a gateway WebSocket smoke test with auto-approval.")
    parser.add_argument("--gateway", default="ws://127.0.0.1:18789", help="Gateway base WS URL without /ws/<user_id>")
    parser.add_argument("--user-id", default="skill_smoke_user")
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--expect-tool-prefix",
        action="append",
        default=[],
        help="Assert that at least one tool.start/tool.result uses this prefix.",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args))
    if args.expect_tool_prefix and not _has_any_tool(result["tool_events"], args.expect_tool_prefix):
        raise SystemExit(
            f"Expected a tool event with one of prefixes {args.expect_tool_prefix}, got {json.dumps(result, ensure_ascii=False)}"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
