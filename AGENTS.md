# Agent Instructions

These instructions apply to all automated coding agents working in this repository.

## Pull Request Verification

Before opening or updating a pull request, the agent must verify the change with
an end-to-end or runtime smoke test that exercises the affected production path.
Unit tests alone are not sufficient for PR readiness.

The E2E check should be scoped to the change, but it must run the real boundary
that the PR claims to affect. Examples:

- Gateway/API changes: start the Gateway on a loopback port and call the HTTP or
  WebSocket route with a real client.
- Control supervisor or mission runtime changes: create a supervisor mission
  through `/tasks/supervisors/control-loop`, inspect the persisted task record,
  durable artifacts, and timeline.
- Browser/current-tab changes: verify the actual browser or current-tab bridge
  state with the relevant tool call, and capture evidence when the workflow
  permits it.
- CLI changes: run the command line entrypoint with a minimal real invocation.

Every PR body must include an `E2E / Runtime Verification` section listing:

- the exact command or script that was run
- the scenario exercised
- the production boundary covered
- the observed result, including key task IDs, artifact fields, HTTP status
  codes, timeline events, or screenshots when relevant
- any warnings, skipped parts, or environment limitations

If an E2E check cannot be run in the current environment, keep the PR as Draft
and document the blocker and the exact manual verification still required. Do
not mark a PR ready for review with only unit tests unless the change is purely
documentation and has no runtime behavior.

Use the repository virtual environment for local validation:

```bash
PYTHONPATH=. .venv/bin/python ...
PYTHONPATH=. .venv/bin/pytest ...
```

Also run the relevant targeted tests and formatting/lint checks defined by the
repository, but treat those as additional evidence rather than a replacement for
the E2E/runtime check.
