# OSS v1.0 Candidate Readiness Boundary

This note captures the current Mission OS readiness boundary after the
evaluation-gated autonomy, telemetry-only HIL, and limited live action gate
slices.

This is a **candidate boundary**, not a production robotics release. It says the
reference architecture can demonstrate mission-level autonomy control as
reproducible, evidence-backed artifacts. It does not claim certified physical
autonomy, direct robot control, or a deployable actuator stack.

## Positioning

boiled-claw is a mission-level control plane for heterogeneous agent systems:
browser-first agents, desktop fallback tasks, simulator-backed validation, and
telemetry-only hardware-in-the-loop evidence.

It is not a low-level controller. It does not replace browsers, operating
systems, robot middleware, autopilots, embedded controllers, ROS, MAVLink, PX4,
or a certified safety stack.

## Implemented Chains

### Browser-First Mission OS

```text
MissionContract
  -> control_supervisor task
  -> durable_execution
  -> execute / verify / recover
  -> mission_review
  -> mission_eval_result
  -> promotion_package
  -> approved promotion artifact
  -> reuse_plan
  -> read-only Control UI
```

This chain is operational through task artifacts, durable execution, recovery
decisions, post-mission review, eval suites, promotion packages, approval-gated
artifacts, reuse plans, and operator-visible UI panels.

### Evaluation-Gated Toy Autonomy

```text
autonomy_plan.v1
  -> autonomous_episode.v1
  -> toy_grid_world_replay_trace.v1
  -> mission eval suites
  -> autonomy_scorecard.v1
  -> autonomy_episode_review.v1
  -> autonomy_gate_result.v1
  -> autonomy_gate_comparison_result.v1
  -> read-only Control UI
```

This chain is simulator-only and dry-run-only. The toy grid-world exists as a
safe local testbed for physical-adjacent safety evaluation, not as a game or
hardware runtime.

### Telemetry-Only HIL

```text
hil_telemetry_contract.v1
  -> hil_telemetry_envelope.v1
  -> fail-closed ingestion / command-like rejection
  -> hil_telemetry_evidence.v1
  -> task.artifacts
  -> hil_telemetry_review.v1
  -> autonomy_gate_result.v1
  -> read-only Control UI
  -> mock HIL source for deterministic demos/tests
```

This chain accepts read-only telemetry evidence. It rejects command-like payload
keys before task mutation and never creates an action envelope, command payload,
ROS/MAVLink dispatch, or actuator path.

### Limited Live Action Gate Design

```text
limited_live_action_gate.v1
  -> limited_live_action_approval_package.v1
  -> limited_live_action_rehearsal.v1
  -> tenth_stage_readiness_check.v1
  -> read-only Control UI
```

This layer is design/schema-only. It records the evidence that would be required
before any future limited live physical action could reach operator review:

- autonomy gate result refs
- HIL telemetry review refs
- emergency-stop evidence refs
- rollback plan refs
- proposal-category action allowlist refs
- operator responsibility acknowledgements
- audit refs

Even a fully populated package only becomes `operator_review_ready`.
`stronger_execution_allowed`, `live_execution_allowed`,
`physical_execution_invoked`, `command_payload_allowed`,
`dispatch_implementation_present`, `ros_dispatch_allowed`,
`mavlink_dispatch_allowed`, and `actuator_execution_allowed` remain false.

`limited_live_action_rehearsal.v1` is the final dry-run/evidence package before
any real limited live action can be considered. It bundles the gate, approval
package, mission contract ref, HIL review ref, emergency-stop evidence,
rollback plan, operator responsibility acknowledgement, and audit refs. Missing
evidence blocks the rehearsal with explicit `missing_precondition:*` reasons.
It does not implement live dispatch, command delivery, ROS/MAVLink integration,
actuator execution, or approval-free stronger execution. Reaching the real
10合目 still requires an adopting organization, hardware owner,
certified/autopilot controller, emergency-stop process, and explicit operator
approval.

The Control UI can render the rehearsal package as an operator-visible,
read-only evidence summary. It does not add approval buttons, command payload
inputs, dispatch controls, ROS/MAVLink controls, actuator controls, or execution
state transitions.

`tenth_stage_readiness_check.v1` is the pre-10合目 checklist. It reads the
rehearsal package and records whether the remaining external preconditions are
present:

- adopting organization ref
- hardware owner ref
- certified/autopilot controller ref
- emergency-stop process ref

If these are present, the check can become `ready_for_organization_review`.
That still does not permit live action. The same artifact records
`live_action_status=blocked_for_live_action` because operator approval is not
performed here and no dispatch implementation exists.

The Control UI can render this checklist as a read-only pre-10合目 summary. It does
not add organization approval buttons, command payload inputs, dispatch
controls, ROS/MAVLink controls, actuator controls, or execution state
transitions.

## Safety Boundary

The boundary is deliberately stronger than "dry run" as a flag:

- Evaluation before autonomy.
- Simulation before hardware.
- Telemetry before action.
- Safety governor before movement in toy simulation.
- Rule-based gates before operator review.
- Replay and scorecards before promotion.
- Operator approval before stronger execution.
- Deny by default when evidence is missing, stale, malformed, unsafe,
  mismatched, or ambiguous.

## Explicit Non-Goals

This boundary does not provide:

- live robot control
- actuator execution
- direct motor commands
- ROS or ROS2 dispatch
- MAVLink / PX4 dispatch
- autopilot replacement
- hardware command channels
- approval-free stronger execution
- LLM-judged safety gates
- autonomous code self-modification
- `/missions` API or a `missions` table

## What Comes Next

Future work should remain schema/design-first until the current evidence
surfaces prove durable:

1. Keep limited live action artifacts read-only and operator-visible.
2. Add validity windows / expiry to future approval and rehearsal packages.
3. Add richer emergency-stop, rollback, and responsibility evidence shapes.
4. Add more safety regression gates over HIL and autonomy evidence.
5. Only then design any live-action dispatch boundary, still without
   implementation until explicitly approved.
