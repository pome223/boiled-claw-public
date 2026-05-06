# Mission OS Prompt-to-SITL Delivery Milestone

This note summarizes the public-safe architecture of a private Mission Designer
execution milestone. It describes the result, safety boundary, and governance
pattern without publishing implementation code, runtime scripts, exact artifact
schemas, command lines, private issue or pull request details, raw logs, hashes,
local paths, simulator setup, transport details, or reproduction steps.

## Core Rule

```text
Mission Designer may drive simulated delivery execution,
but observed evidence is not command authority.
```

Mission Designer can now move from prompt-derived scenario design to an
operator-gated PX4/Gazebo SITL delivery chain. The important result is not just
that a scenario can be proposed. The system can also enter a simulated execution
path and return a durable evidence chain for review.

That evidence remains separate from authority. Observed upload, flight,
payload-release, and dropoff facts can support delivery verification, but they
do not grant stronger command authority, hardware authority, or physical
execution authority.

## What Changed

At the public architecture level, the milestone demonstrates this shape:

```text
operator prompt
  -> scenario proposal
  -> bounded request approval
  -> SITL execution preparation
  -> explicit operator execution
  -> observed mission upload
  -> observed flight evidence
  -> payload-release observation
  -> dropoff verification
  -> delivery exit evidence
```

The chain is intentionally staged. Proposal generation is not execution.
Preparation is not execution. Execution is an explicit operator-gated step, and
the resulting delivery claim must be backed by observed facts.

## Evidence Chain, Not Action Surface

The milestone adds a product-like path from Mission Designer into simulated
delivery execution while preserving the read-only evidence discipline.

The UI can show:

- scenario proposal state
- bounded approval state
- prepared execution state
- execution status
- observed mission upload evidence
- observed flight evidence
- payload-release observation
- dropoff verification
- final delivery exit evidence
- safety-boundary flags

The UI must not turn those evidence records into extra controls. Payload release
is not a button. Dropoff verification is not a button. The delivery exit record
is not a button. The evidence chain is there so an operator can inspect what
happened, not so the UI can bypass the policy gate.

## Safety Boundary

The public boundary remains simulation-first and operator-gated.

Allowed:

- prompt-derived scenario design
- operator review of the proposed scenario
- explicit operator approval before stronger simulated execution
- observed simulated mission upload evidence
- observed simulated flight evidence
- observed payload-release evidence
- observed dropoff verification
- read-only evidence-chain rendering
- fail-closed blocked results when policy gates are not satisfied

Not allowed:

- hardware targeting
- physical execution
- approval-free stronger execution
- synthetic delivery success
- hidden payload-release success
- upload-only delivery success
- flight-only delivery success
- payload-only delivery success
- direct ROS or actuator control surfaces
- simulator mutation buttons in the evidence panel
- re-run or dispatch controls from evidence artifacts

The authority boundary lives at the policy and operator-gate layer. A UI label
or prompt-derived scenario cannot claim delivery success on its own.

## Observed-Fact Success

The delivery milestone separates several facts that are easy to conflate:

- mission upload was observed
- flight evidence was observed
- payload release was observed
- dropoff was verified
- the delivery exit evidence was produced

The delivery exit depends on the chain, not on any single fact alone. Upload
without flight is not delivery success. Flight without payload release is not
delivery success. Payload release without dropoff verification is not delivery
success.

This keeps the Mission OS delivery path aligned with the broader rule:

```text
Evidence can prove what happened.
Evidence does not become command authority.
```

## Publication Boundary

This note is intentionally non-reproducible. It publishes the safety model,
architecture shape, and milestone semantics. It does not publish the operational
details required to reproduce the private simulator run.

That boundary is deliberate. The public artifact should make the design
reviewable without turning the repository into an execution recipe for
physical-adjacent systems.
