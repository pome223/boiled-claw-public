---
name: e2e-test
description: Run end-to-end smoke tests against the boiled-claw gateway from the Docker dev container.
version: 1.1.0
author: boiled-claw
tags:
  - testing
  - e2e
  - docker
  - pytest
---

# E2E Test Skill

Run the automated e2e suite from the Docker dev container. Do not rely on a host `.venv`.

## Prerequisites

- Docker daemon is running
- `.env` exists and `GOOGLE_API_KEY` is set
- Gateway is started with `docker compose up -d --build boiled-claw-gateway`

## Standard Run

```bash
docker compose --profile dev run --rm boiled-claw-dev pytest tests/test_e2e.py -v -m e2e
```

`boiled-claw-dev` injects `GATEWAY_URL=http://boiled-claw-gateway:18789`, so the test container talks to the gateway over the Compose network.

## Quick Smoke Check

If you need a fast readiness check before the full suite:

```bash
docker compose --profile dev run --rm boiled-claw-dev curl -sS http://boiled-claw-gateway:18789/health
```

Expected: JSON with `"status": "ok"`.

## Pass Criteria

- `pytest` exits with code `0`
- No test is skipped because the gateway is unreachable
- If the quick smoke check is used, `/health` returns HTTP `200`
