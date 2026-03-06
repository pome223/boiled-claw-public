---
name: e2e-test
description: Run end-to-end smoke tests against the running boiled-claw gateway via HTTP API.
version: 1.0.0
author: boiled-claw
tags:
  - testing
  - e2e
  - curl
---

# E2E Test Skill

Smoke-test the boiled-claw gateway by firing curl requests against `/agent/run`.

## Prerequisites

- Gateway is running at `http://127.0.0.1:18789` (or `GATEWAY_URL` env var)
- `GATEWAY_API_KEY` is set in the environment if auth is enabled

## Test Cases

Run each case in order. Check `ok: true` in every response.

### 1. Basic response

```bash
curl -sS -X POST ${GATEWAY_URL:-http://127.0.0.1:18789}/agent/run \
  -H "Content-Type: application/json" \
  ${GATEWAY_API_KEY:+-H "Authorization: Bearer $GATEWAY_API_KEY"} \
  -d '{"user_id":"e2e","message":"こんにちは。一言で自己紹介して"}'
```

Expected: `ok: true`, `response` contains agent name or description.

### 2. Session continuity

Capture `session_id` from case 1, then:

```bash
curl -sS -X POST ${GATEWAY_URL:-http://127.0.0.1:18789}/agent/run \
  -H "Content-Type: application/json" \
  ${GATEWAY_API_KEY:+-H "Authorization: Bearer $GATEWAY_API_KEY"} \
  -d "{\"user_id\":\"e2e\",\"session_id\":\"<SESSION_ID>\",\"message\":\"さっき何を話しましたか？\"}"
```

Expected: response references the previous turn.

### 3. Stock price shortcut (is_direct_stock_price_query = true)

```bash
curl -sS -X POST ${GATEWAY_URL:-http://127.0.0.1:18789}/agent/run \
  -H "Content-Type: application/json" \
  ${GATEWAY_API_KEY:+-H "Authorization: Bearer $GATEWAY_API_KEY"} \
  -d '{"user_id":"e2e","message":"NVIDIAの株価"}'
```

Expected: OHLC data returned immediately (始値/高値/安値/終値/出来高).

### 4. Stock price news (is_direct_stock_price_query = false → LLM routing)

```bash
curl -sS -X POST ${GATEWAY_URL:-http://127.0.0.1:18789}/agent/run \
  -H "Content-Type: application/json" \
  ${GATEWAY_API_KEY:+-H "Authorization: Bearer $GATEWAY_API_KEY"} \
  -d '{"user_id":"e2e","message":"NVIDIAの株価ニュースを2つ教えて"}'
```

Expected: LLM performs web search and returns news items, not raw OHLC.

## Pass Criteria

| Case | `ok` | Response shape |
|------|------|----------------|
| 1    | true | free text      |
| 2    | true | references prior turn |
| 3    | true | contains 始値/終値 |
| 4    | true | news articles, no raw OHLC |

If any case returns `ok: false` or HTTP 4xx/5xx, the gateway is not ready to merge.
