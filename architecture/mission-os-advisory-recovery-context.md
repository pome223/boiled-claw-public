# Mission OS Advisory Recovery Context Milestone

This note summarizes the public-safe architecture of a private advisory
recovery-context milestone. It describes the result, safety boundary, and
governance pattern without publishing implementation code, runtime scripts,
exact artifact schemas, command lines, private issue or pull request details,
raw logs, or reproduction steps.

## Core Rule

```text
Advisory context may shape recovery proposals, but recovery outcomes remain observed-facts only.
```

Completed-mission lessons and same-mission shared observations may help a
runtime decide which recovery option to propose or suppress. They must not
rewrite observed facts, change recovery predicates, become scorecard evidence,
serve as success proof, or grant stronger recovery authority.

## What Changed

After fleet memory and shared observation, the next private milestone connected
those advisory inputs to recovery selection while preserving the existing
recovery boundary. The milestone demonstrates this shape:

```text
recovery request
  -> promoted lesson and validated shared observation
  -> advisory recovery context
  -> recovery proposal surface
  -> advisory ref validation
  -> recovery outcome computed with advisory context available
  -> recovery outcome computed without advisory context
  -> canonical outcome equality
  -> negative authority cases fail closed
```

The important result is not that memory or shared observation can decide
whether recovery succeeded. They cannot. The result is that Mission OS can use
experience and current-mission context to shape recovery proposals while keeping
the outcome verifier tied to observed facts only.

## Proposal Context, Not Outcome Authority

Advisory recovery context is useful because recovery selection often depends on
information outside the immediate failure event. Prior reviewed missions may
show which bounded recovery proposals were useful. A peer vehicle's current
observation may make one proposal more appropriate than another. That context
belongs in the proposal layer.

The allowed scope is:

- recovery proposal context
- visible used and ignored advisory references
- visible suppressed recovery candidates
- operator-visible review evidence
- replay and regression coverage
- proposal selection hints

The forbidden scope is:

- observed-fact modification
- recovery outcome predicate changes
- verifier input shortcuts
- scorecard evidence
- success proof
- direct command authority
- approval-free dispatch
- approval-free stronger recovery
- MAVLink or ROS dispatch
- mission upload
- setpoint streams
- actuator commands
- simulator mutation
- hardware or physical execution

This keeps advisory recovery aligned with the broader Mission OS rule: context
can influence what the system proposes, but context does not decide what
happened.

## Architecture Shape

At the public architecture level, the milestone adds an advisory layer beside
the existing recovery chain:

```text
observed recovery facts
  -> recovery run
  -> recovery outcome verifier

advisory lessons and shared observations
  -> advisory recovery context
  -> recovery proposal surface
```

The two branches meet only at the proposal boundary. The outcome branch remains
driven by the same recovery run and observed facts. That separation is the
load-bearing design decision.

The proposal surface records which advisory inputs were used, ignored, or used
to suppress a candidate. Hidden filtering is not the goal. If advisory context
changes the proposal set, the runtime should leave a durable, reviewable trace
of that influence.

## Validator Spine

The core of this milestone is validation around references, not a new recovery
algorithm.

At the public boundary, advisory recovery validation must prove:

- lesson references are promoted and operator-approved before use
- shared observations have already passed provenance and causality checks
- shared observations belong to the same mission context they are cited from
- advisory references are recorded when proposal logic uses them
- ignored and suppressed advisory inputs remain visible
- command-like advisory content is rejected
- advisory references are blocked from observed facts
- advisory references are blocked from scorecard evidence
- advisory references are blocked from success proof
- advisory references are blocked from recovery outcome input
- advisory context cannot override recovery outcome predicates

The validator is intentionally fail-closed. Missing validation evidence,
ambiguous references, hidden authority paths, and command-shaped payloads are
treated as errors rather than warnings.

## Load-Bearing Regression

The private regression proof follows a simple rule:

```text
same recovery run + same observed facts
  with advisory context empty
  with advisory context full
=> recovery outcome canonical output is equal
```

Recovery proposals may differ. Recovery outcomes must not. That distinction is
the milestone.

This regression is paired with negative cases that intentionally attempt to
make advisory context act like authority. Those attempts must fail closed.

## Negative Cases

The private regression layer includes the cases that matter for preserving the
boundary:

- advisory context cannot become observed facts
- advisory context cannot become scorecard evidence
- advisory context cannot become success proof
- advisory context cannot become verifier or outcome input
- advisory context cannot change recovery outcome predicates
- command-like advisory payloads are rejected
- proposal suppression must cite a visible advisory reference
- recovery outcome output remains equal with and without advisory context

These cases are part of the achievement. A recovery system that learns from
context is useful only if it also proves when not to trust that context.

## Relationship to Fleet Memory and Shared Observation

The advisory recovery milestone composes two earlier Mission OS ideas.

```text
Fleet memory:
  previous missions may inform later planning

Shared observation:
  current-mission observations may inform another decision context

Advisory recovery context:
  promoted lessons and validated shared observations may inform recovery
  proposal selection
```

All three remain advisory. None grants command authority, success authority, or
approval-free stronger execution.

## Why This Matters

Recovery is where the temptation to blur boundaries is strongest. When a system
is blocked, stale, uncertain, or partially failed, it is easy to let a plausible
memory or contextual hint stand in for an observed fact.

Mission OS avoids that shortcut by separating proposal influence from outcome
judgement:

```text
context can shape the next proposal
observed facts decide the outcome
policy gates decide authority
```

That separation lets the runtime become more context-aware without becoming
less accountable.

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
