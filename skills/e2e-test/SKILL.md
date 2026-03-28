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

## AI CLI Skills Smoke Test (Docker container)

These checks run inside the Docker dev container and do **not** require external
AI CLIs (claude/codex/gemini) to be installed.

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
    expected = {'coding-agent', 'e2e-test', 'code-review', 'multi-llm-judge', 'auto-fix', 'computer-use'}
    missing = expected - set(names)
    if missing:
        print(f'FAIL: missing skills: {missing}')
        exit(1)
    print(f'PASS: {len(names)} skills loaded — {sorted(names)}')

asyncio.run(main())
"
```

- Exit code `0`
- All 6 skills present: `coding-agent`, `e2e-test`, `code-review`, `multi-llm-judge`, `auto-fix`, `computer-use`

### Utility argv Construction (unit test, no CLI required)

Verify that `run_ai_cli.py` builds correct argument lists without invoking any CLI:

```bash
python3 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('run_ai_cli', 'skills/_utils/run_ai_cli.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

# claude: stdin transport, no prompt in argv
args, stdin = mod._build_args_and_input('claude', 'test prompt', 'default', [], None, False)
assert args == ['claude', '-p'], f'claude args: {args}'
assert stdin == 'test prompt', f'claude stdin: {stdin}'

# codex exec: stdin via '-'
args, stdin = mod._build_args_and_input('codex', 'test prompt', 'default', [], None, False)
assert args == ['codex', 'exec', '-'], f'codex exec args: {args}'
assert stdin == 'test prompt', f'codex exec stdin: {stdin}'

# codex review --base: no prompt in argv (mutually exclusive)
args, stdin = mod._build_args_and_input('codex', 'ignored', 'review', [], 'main', False)
assert args == ['codex', 'review', '--base', 'main'], f'codex review args: {args}'
assert stdin is None, f'codex review stdin should be None: {stdin}'

# codex review --uncommitted: no prompt in argv
args, stdin = mod._build_args_and_input('codex', '', 'review', [], None, True)
assert args == ['codex', 'review', '--uncommitted'], f'codex uncommitted args: {args}'
assert stdin is None

# gemini: stdin transport
args, stdin = mod._build_args_and_input('gemini', 'test prompt', 'default', [], None, False)
assert args == ['gemini'], f'gemini args: {args}'
assert stdin == 'test prompt'

print('PASS: all argv construction checks passed')
"
```

- Exit code `0`
- All 5 assertions pass

## Pass Criteria

- `pytest` exits with code `0`
- No test is skipped because the gateway is unreachable
- If the quick smoke check is used, `/health` returns HTTP `200`
- All 5 skills load via `ensure_skills_loaded()`
- Utility argv construction test passes (no external CLI needed)
