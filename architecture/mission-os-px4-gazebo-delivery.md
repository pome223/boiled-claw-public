# Mission OS for PX4/Gazebo Drone Delivery

This note describes a private Mission OS milestone: a simulated drone delivery
mission in PX4 SITL and Gazebo, managed as an operator-approved mission runtime.

It is intentionally a **public architecture note**, not a reproduction guide.
The implementation, exact runtime scripts, transport details, orchestration
setup, and validation chain remain private.

This note is not operational guidance for real-world robotics deployment. It
describes a simulation-only research milestone and does not provide instructions
for controlling real vehicles.

## Positioning

boiled-claw treats physical AI work as mission-level control, not as direct
actuator control. The Mission OS layer is responsible for:

- mission contracts
- durable evidence
- operator approval
- bounded command authorization
- telemetry and simulator observation
- fail-closed diagnostics
- task status and audit history

The low-level controller remains outside the Mission OS. In this milestone that
controller is PX4 SITL, and the environment is Gazebo.

## What Was Demonstrated

The private milestone demonstrated this end-to-end shape:

```text
Mission OS durable approval and allowlist
  -> bounded simulation-only command path
  -> PX4 SITL in Gazebo
  -> vehicle state changes in simulation
  -> pickup -> enroute -> dropoff -> completed
  -> durable task completion evidence
```

The important result is not "an agent sent a command." The important result is
that the mission runtime can require evidence at each layer before treating a
physical-adjacent task as complete.

At a high level, completion required all of the following:

- an explicit operator-approved mission boundary
- a bounded simulation-only command surface
- telemetry and simulator observation
- PX4-side acceptance evidence
- Gazebo-side vehicle state evidence
- phase evidence for pickup, enroute, dropoff, and completed
- fail-closed handling for missing, mismatched, or unsafe evidence

## Safety Boundary

The milestone stayed simulation-only.

It did not claim or permit:

- real drone control
- physical execution
- hardware target execution
- approval-free stronger execution
- arbitrary command dispatch
- unbounded mission upload
- direct motor / actuator control
- production robotics safety certification

The intended boundary is:

```text
simulation actuator effect: allowed only inside PX4 SITL / Gazebo
physical actuator effect: not allowed
hardware target: not allowed
real-world authority: not granted
operator approval: required before bounded command dispatch
evidence: required before task completion
```

## Staged Architecture

The work followed a staged sequence:

```text
telemetry-only observation
  -> readiness and state correlation
  -> command proposal
  -> operator approval
  -> bounded allowlist
  -> simulation-only dispatch boundary
  -> coupled simulator evidence
  -> fail-closed diagnostics
  -> read-only audit and observation surfaces
```

This order matters. A physical-adjacent agent runtime should not start from
"send a command." It should start from observation, evidence, approval, and
bounded authority.

## Artifact Philosophy

The private implementation represents mission progress through durable artifacts
rather than ephemeral logs alone.

The artifact pattern is:

```text
proposal evidence
  -> approval evidence
  -> bounded authority evidence
  -> dispatch result evidence
  -> simulator state evidence
  -> terminal runner evidence
  -> diagnostics when blocked
```

This makes the runtime auditable. It also makes unsafe or incomplete cases
first-class states instead of exceptions hidden in logs.

## Fail-Closed Cases

The milestone included explicit blocked outcomes for cases such as:

- timeout
- rejected command
- wrong target
- non-simulation endpoint
- missing approval
- missing allowlist
- missing simulator state evidence
- command-like leakage in observation-only paths

The design goal is that failure should produce durable diagnostics, not retries
into stronger execution.

## What Is Public Here

This note publishes:

- the Mission OS design intent
- the staged safety architecture
- the high-level milestone result
- the evidence-first completion philosophy
- the simulation-only boundary

This note does **not** publish:

- working PX4/Gazebo integration code
- transport helpers
- exact command details
- smoke scripts
- container or bridge setup
- detailed schemas
- validation corpus
- recovery implementation
- reproduction steps

In short:

```text
principles are public
implementation is private
results are visible
reproduction steps are not published
```

## Why This Matters

Browser agents and physical agents look different at the actuator layer, but
they need the same mission-level governance:

- what was requested
- what was approved
- what was allowed
- what was observed
- what evidence completed the task
- what blocked unsafe execution

The PX4/Gazebo delivery milestone is one private proof point for that thesis.
It shows that Mission OS can sit above a simulator-backed autonomy stack while
preserving operator approval, bounded authority, and durable evidence.

## Next Public-Safe Directions

Public work can continue around the architecture without exposing the private
implementation:

- toy or fake-endpoint examples
- high-level artifact diagrams
- evaluation criteria for physical-adjacent missions
- operator-visible audit UI concepts
- safety boundary documentation
- benchmark-style task definitions

The live simulator implementation and exact validation chain should remain
private until there is a deliberate reason to publish them.
