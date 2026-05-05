# Mission OS Shared Observation Milestone

This note summarizes the public-safe architecture of a private
intra-mission shared-observation milestone. It describes the principle and
result shape without publishing implementation code, runtime scripts, simulator
setup, transport details, exact artifact schemas, command lines, raw logs, or
reproduction steps.

## Core Rule

```text
Vehicles may share observations, but shared observations are never command authority.
```

A vehicle may cite another vehicle's observed fact as decision context. That
citation must preserve provenance, timing, and session membership. It must not
become a command channel, dispatch permission, scorecard shortcut, or success
proof.

## What Changed

After mission-to-mission memory, the next private milestone moved the same
governance pattern inside a single mission. The private milestone demonstrates
this shape:

```text
mission session
  -> vehicle session A
  -> vehicle session B
  -> vehicle A source observation
  -> append-only shared observation
  -> vehicle B decision context citation
  -> ref and causality validation
  -> shared-observation epic-exit evidence
```

The important result is not that one vehicle can control another. It cannot.
The result is that a mission-level runtime can let vehicles share observed facts
while keeping those facts advisory, attributable, time-bounded, and reviewable.

## Observation, Not Authority

Shared observations are useful because mission participants do not all see the
same world at the same time. A blocked route, stale telemetry, battery concern,
payload state, or local hazard may matter to another vehicle's planning
context.

The allowed scope is:

- decision context
- route and risk awareness
- operator-visible evidence
- replay and regression coverage
- provenance-preserving citations

The forbidden scope is:

- direct command authority
- approval-free dispatch
- approval-free stronger recovery
- scorecard or success-proof shortcuts
- hardware execution
- physical execution
- mission upload
- unbounded setpoint streams
- arbitrary simulator mutation

This keeps shared observation aligned with the broader Mission OS principle:
facts can inform a decision, but facts do not grant power.

## Provenance and Causality

The load-bearing checks are not the data fields by themselves. They are the
validators around the references.

At the public architecture level, a shared observation must prove:

- it belongs to the same mission session
- its source vehicle session belongs to that mission
- the cited source observation exists in the source vehicle session
- the shared payload does not contradict the source observation
- the event source and observation kind are allowlisted
- command-like payloads are rejected
- a decision that uses the observation cites it explicitly
- observation time, receive time, and decision time preserve causality

The temporal rule is:

```text
observed_at <= received_at <= decision_at
```

Freshness is measured from the observation time, not merely from the time another
vehicle received it. A very old observation does not become fresh just because
it was forwarded recently.

## Append-Only Shared Log

The milestone treats shared observations as an append-only evidence chain, not
as mutable global state. That distinction matters.

Mutable global state makes it easy to lose who saw what, when, and from which
vehicle session. An append-only observation chain makes later review possible:

```text
who observed it
  -> when it was observed
  -> when it was received
  -> which mission it belongs to
  -> which decision cited it
  -> which validator accepted or rejected it
```

This is the same pattern used elsewhere in Mission OS: durable evidence first,
authority later only if a separate policy explicitly permits it.

## Negative Cases

The private regression layer includes the cases that matter for shared
observation safety:

- a future observation cannot justify an earlier decision
- stale observations fail closed
- cross-mission observations are rejected
- missing source observations are rejected
- timestamp rewrites are rejected
- contradictory payloads are rejected
- command-like payloads are rejected
- shared observations are not copied into verifier success facts

These negative cases are part of the result. A shared-observation layer is only
safe if it knows when not to share, when not to cite, and when not to treat a
citation as proof.

## Relationship to Fleet Memory

Fleet memory and shared observation use the same governance principle at
different time scales.

```text
Cross-mission memory:
  prior missions may inform future mission planning

Intra-mission shared observation:
  one vehicle's current-mission observation may inform another vehicle's
  decision context
```

Both remain advisory. Neither grants command authority.

## What Is Not Published

This note does not publish:

- implementation code
- runtime scripts
- exact execution commands
- simulator/container setup
- transport details
- low-level message details
- ports or sockets
- exact artifact schemas
- private issue or pull request details
- raw logs
- output paths
- reproduction steps

The public value is the architecture and safety policy. The private value is the
working implementation and validation chain.
