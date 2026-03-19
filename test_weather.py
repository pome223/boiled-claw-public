import asyncio
import json
import sys
import websockets


async def test():
    uri = "ws://127.0.0.1:18789/ws/test_user"
    print("Connecting...", flush=True)
    async with websockets.connect(uri, ping_interval=30, ping_timeout=60) as ws:
        msg = json.loads(await ws.recv())
        event = msg["event"]
        sid = msg.get("session_id", "?")
        print(f"[{event}] session={sid}", flush=True)

        await ws.send(json.dumps({
            "event": "chat.send",
            "text": "browser_navigateツールを使ってhttps://tenki.jp/forecast/3/16/にアクセスし、browser_extract_textで東京都の天気予報のテキストを取得してください。web_searchは使わないでください。",
            "request_id": "weather-test-1"
        }))

        full_text = ""
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                ev = json.loads(raw)
                event_type = ev.get("event", "")

                if event_type == "chat.token":
                    token = ev.get("token", "")
                    full_text += token
                    sys.stdout.write(token)
                    sys.stdout.flush()
                elif event_type == "tool.start":
                    tool = ev.get("tool_name", ev.get("name", "?"))
                    print(f"\n--- [TOOL START] {tool} ---")
                elif event_type == "tool.result":
                    tool = ev.get("tool_name", ev.get("name", "?"))
                    result_text = str(ev.get("result", ""))[:500]
                    print(f"\n--- [TOOL RESULT] {tool}: {result_text} ---")
                elif event_type == "tools.approval_request":
                    tool = ev.get("tool_name", "?")
                    print(f"\n--- [APPROVAL NEEDED] {tool} ---")
                    await ws.send(json.dumps({
                        "event": "tools.approval",
                        "request_id": ev.get("request_id", ""),
                        "approved": True
                    }))
                    print("  -> Auto-approved")
                elif event_type == "chat.done":
                    print("\n\n=== DONE ===")
                    break
                elif event_type == "system.event":
                    msg_text = ev.get("message", str(ev))
                    print(f"\n[SYSTEM] {msg_text}")
                elif event_type in ("health.tick", "presence.pong"):
                    pass
                else:
                    summary = json.dumps(ev, ensure_ascii=False)[:200]
                    print(f"\n[{event_type}] {summary}")
            except asyncio.TimeoutError:
                print("\n[TIMEOUT after 120s]")
                break


asyncio.run(test())
