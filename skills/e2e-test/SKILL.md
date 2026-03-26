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

## AI CLI Skills Smoke Test

Verify that the external AI CLI integration skills load correctly and the utility works.

### Skill Loading

```bash
python3 -c "
import asyncio
from src.skills.runtime import ensure_skills_loaded
from src.skills.base import get_skill_registry

async def main():
    await ensure_skills_loaded()
    registry = get_skill_registry()
    names = [m.name for m in registry.list_skills()]
    expected = {'coding-agent', 'e2e-test', 'code-review', 'multi-llm-judge', 'auto-fix'}
    missing = expected - set(names)
    if missing:
        print(f'FAIL: missing skills: {missing}')
        exit(1)
    print(f'PASS: {len(names)} skills loaded — {sorted(names)}')

asyncio.run(main())
"
```

- Exit code `0`
- All 5 skills present: `coding-agent`, `e2e-test`, `code-review`, `multi-llm-judge`, `auto-fix`

### CLI Detection Utility

```bash
python3 skills/_utils/run_ai_cli.py --detect
```

- Exit code `0`
- At least 1 CLI found (environment-dependent; all 3 if fully configured)

### Basic CLI Invocation (per available CLI)

For each CLI reported as available, run a trivial prompt to confirm stdin transport works:

```bash
python3 skills/_utils/run_ai_cli.py --cli claude --prompt "Say only: ok" --timeout 30
python3 skills/_utils/run_ai_cli.py --cli codex --prompt "Say only: ok" --timeout 60
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt "Say only: ok" --timeout 120
```

- Each available CLI returns exit code `0` and non-empty stdout
- Unavailable CLIs are skipped (not a failure)

### Codex Review Mode

```bash
python3 skills/_utils/run_ai_cli.py --cli codex --mode review --review-base main --timeout 60
```

- Exit code `0` (may report "no diff" if branch is up to date — that is still a pass)
- No argument parsing error

## Pass Criteria

- `pytest` exits with code `0`
- No test is skipped because the gateway is unreachable
- If the quick smoke check is used, `/health` returns HTTP `200`
- All 5 skills load via `ensure_skills_loaded()`
- CLI detection utility exits `0` with at least 1 CLI found
- Each available CLI responds to a trivial stdin prompt
