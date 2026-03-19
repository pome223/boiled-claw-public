---
name: local-runtime-smoke
description: Run local gateway, dynamic-agent, host bridge, current-tab, and desktop bridge smoke checks for this repository.
version: 1.0.0
author: boiled-claw
tags:
  - testing
  - smoke
  - gateway
  - host-bridge
  - current-tab
  - desktop
---

# Local Runtime Smoke Skill

Use this skill when you need to verify the local boiled-claw runtime after gateway, bridge, browser, or desktop changes.

## Prerequisites

- Docker Desktop is running
- `.env` exists with a valid `GOOGLE_API_KEY`
- Host `.venv` exists
- For host-visible browser checks: host environment has Playwright and Chromium installed
- For desktop checks on macOS: Accessibility and screen-capture permissions are granted as needed

## Core Gateway Smoke

1. Start the gateway and sample MCP server:

```bash
docker compose build --build-arg INSTALL_BROWSER=true boiled-claw-gateway
docker compose up -d boiled-claw-gateway boiled-claw-mcp-sample
```

2. Verify basic endpoints:

```bash
curl -s http://127.0.0.1:18789/health
curl -s http://127.0.0.1:18789/protocol
curl -s http://127.0.0.1:18789/chat
```

3. Run a WebSocket chat smoke:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "1+1を短く答えて"
```

4. Run a dynamic-agent smoke:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/dynamic_agent_smoke.py
```

5. Run a browser automation smoke through the gateway:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "ブラウザで http://127.0.0.1:18789/protocol を開いて version を教えて"
```

Expect `browser_navigate` and `browser_extract_text` in tool events and a final answer mentioning `version 1`.

## Host Bridge Visible Browser Smoke

1. Start Host Bridge on the host:

```bash
CURRENT_TAB_BRIDGE_ENABLED=true \
CURRENT_TAB_BRIDGE_HOST=127.0.0.1 \
CURRENT_TAB_BRIDGE_PORT=8768 \
BROWSER_ALLOW_LOOPBACK=true \
.venv/bin/python -m src.main host-bridge --host 127.0.0.1 --port 8766
```

2. Restart the gateway with Host Bridge enabled:

```bash
HOST_BRIDGE_ENABLED=true \
HOST_BRIDGE_URL=http://host.docker.internal:8766/sse \
BROWSER_ALLOW_LOOPBACK=true \
docker compose up -d boiled-claw-gateway
```

3. Verify a visible browser run:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "見えるブラウザで http://127.0.0.1:18789/protocol を開いて version を教えて"
```

Expect `host.browser.navigate` and a final answer that reports the protocol version.

## Current Tab Adapter Smoke

1. Keep Host Bridge running with `CURRENT_TAB_BRIDGE_ENABLED=true`.
2. Launch Chrome with the unpacked extension:

```bash
open -na "Google Chrome" --args \
  --user-data-dir=/tmp/boiled-claw-current-tab-profile \
  --disable-extensions-except=/Users/manabu/works/repositories/boiled-claw/chrome_extension/current_tab_adapter \
  --load-extension=/Users/manabu/works/repositories/boiled-claw/chrome_extension/current_tab_adapter \
  http://127.0.0.1:18789/chat
```

3. After Chrome is visible, verify the current tab:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "このブラウザで http://127.0.0.1:18789/protocol に移動して version を教えて"
```

Expect `host.current_tab.navigate` and `host.current_tab.extract_text` in tool events.

## Desktop Bridge Smoke

1. Start Desktop Bridge on the host:

```bash
.venv/bin/python -m src.main desktop-bridge --host 127.0.0.1 --port 8767
```

2. Restart the gateway with Desktop Bridge enabled:

```bash
DESKTOP_BRIDGE_ENABLED=true \
DESKTOP_BRIDGE_URL=http://host.docker.internal:8767/sse \
docker compose up -d boiled-claw-gateway
```

3. Verify desktop view/control on macOS:

```bash
.venv/bin/python skills/local-runtime-smoke/scripts/ws_smoke.py \
  --message "Google Chrome を前面にして、前面アプリ名を教えて"
```

Expect `desktop.control.focus_window` and `desktop.view.frontmost_app` unless the run is blocked by missing macOS permissions.

## Pass Criteria

- Gateway health is `healthy`
- WebSocket chat returns `chat.done`
- Dynamic agent completes with the sample MCP server
- Host Bridge visible browser smoke uses `host.browser.*`
- Current Tab smoke uses `host.current_tab.*`
- Desktop Bridge smoke uses `desktop.*` or fails with a clear macOS permission error
