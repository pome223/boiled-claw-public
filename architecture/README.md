# boiled-claw Architecture Overview

boiled-claw is a browser-first, policy-bounded closed-loop agent runtime. It is
designed to move from normal computer-use tasks toward simulation-first physical
AI validation without changing the core execution contract.

The system is not organized around "LLM plus tools." It is organized around a
durable loop:

```text
plan -> execute -> verify -> repair -> record trajectory -> improve
```

## Main Layers

1. **Gateway and Control UI**
   The FastAPI Gateway exposes chat, task APIs, WebSocket events, approvals,
   audit logs, task timelines, and the browser-based Control UI.

2. **Agent runtime**
   The ADK-backed agent layer routes user requests to browser, current-tab,
   desktop, skills, memory, and supervisor capabilities.

3. **Execution surfaces**
   boiled-claw prefers structured current-tab and browser operations before
   falling back to desktop accessibility, screenshot, or hotkey control. Host
   and desktop bridges keep machine-control capabilities outside the Dockerized
   Gateway.

4. **Durable task and evidence substrate**
   Tasks, audit events, approvals, trajectories, scheduler state, checkpoints,
   mission contracts, verifier reports, and reuse traces are persisted so a run
   can be inspected, resumed, replayed, or reviewed later.

5. **Live supervisor runtime**
   Control-loop supervisors can run long-lived mission contracts, tick due
   scheduler entries, update heartbeat and checkpoint state, resume after
   Gateway restart, enforce typed abort conditions, and publish mission
   scorecards / post-mission reviews without creating a separate mission table.

6. **Self-improvement spine**
   Failed or uncertain trajectories can be classified, replayed, turned into
   repair candidates, benchmarked in isolated canaries, and promoted only after
   explicit approval and audit.

7. **Physical-ready adapter surface**
   Physical work stays simulation-first. Mission contracts, telemetry health,
   action envelopes, governor decisions, verifier reports, and replay plans are
   represented as durable artifacts before any restricted physical proof of
   concept is attempted. The first Mission OS physical replay slice is
   artifact-only: it can produce simulation scenario requests, telemetry health
   snapshots, blocked-or-dry-run safety decisions, dry-run action envelopes, and
   offline replay plans without adapter dispatch or actuator execution. A
   deterministic 2D grid-world toy simulator now gives this path a local
   testbed for obstacles, hazards, low battery, replay traces, and original
   retro top-down SVG visualization.

## Control Flow

```mermaid
flowchart TD
    U["User, schedule, or mission contract"] --> G["Gateway and Control UI"]
    G --> R["Router and agent runtime"]
    R --> S["Execution surface"]
    S --> V["Verifier"]
    V --> T["Durable task, trajectory, and evidence store"]
    V -->|pass| C["Complete with evidence"]
    V -->|fail or uncertain| P["Recovery policy"]
    P --> W["Repair or escalation"]
    W --> S
    T --> I["Canary self-improvement pipeline"]
    I --> A{"Approval gate"}
    A -->|approved| M["Promoted memory, skill, capability, or policy"]
    A -->|denied| D["Rejected proposal with audit trail"]
    M --> R
```

## OSS v1.0 Candidate Boundary

boiled-claw's current physical/autonomy posture is evaluation-gated and
mission-level. The runtime is not a low-level controller, robot middleware,
autopilot, operating system, or browser replacement. It is the policy-bounded
control plane above heterogeneous execution surfaces: browser, desktop,
simulator, and telemetry-only HIL.

For the milestone-level summary, read
[oss-v1-readiness-boundary.md](oss-v1-readiness-boundary.md).

For a public, non-reproducible narrative of the private PX4/Gazebo drone
delivery milestone, read
[mission-os-px4-gazebo-delivery.md](mission-os-px4-gazebo-delivery.md). It
publishes the Mission OS safety architecture and result shape without releasing
the implementation, simulator setup, transport details, or smoke scripts.

Two paths are now closed enough to describe as an OSS v1.0 candidate boundary:

This is a readiness boundary rather than a final release claim. It means the
reference architecture can reproduce and inspect evaluation-gated autonomy and
telemetry-only HIL as artifact chains; it does not claim production physical
autonomy or certified live robot control.

```text
toy-grid autonomy:
  autonomy_plan.v1
    -> autonomous_episode.v1
    -> toy_grid_world_replay_trace.v1
    -> mission eval suites
    -> autonomy_scorecard.v1
    -> autonomy_episode_review.v1
    -> autonomy_gate_result.v1
    -> autonomy_gate_comparison_result.v1
    -> read-only Control UI

telemetry-only HIL:
  hil_telemetry_contract.v1
    -> hil_telemetry_envelope.v1
    -> fail-closed ingestion
    -> hil_telemetry_evidence.v1
    -> hil_telemetry_review.v1
    -> autonomy_gate_result.v1
    -> read-only Control UI
```

The safety boundary is deliberately stronger than "a flag says dry run." The
schemas make command and action payloads unrepresentable in the HIL path,
ingestion rejects command-like keys before task mutation, toy-grid motion stays
inside a deterministic simulator, gates are rule-based rather than LLM-judged,
and every stronger execution path remains blocked behind explicit operator
approval and a future design contract.

This boundary explicitly does **not** provide live robot control, actuator
execution, ROS/MAVLink dispatch, autopilot replacement, hardware command
channels, approval-free stronger execution, or autonomous code
self-modification.

The limited live physical action gate is therefore a design/schema artifact,
not a dispatch path. `limited_live_action_gate.v1` gathers autonomy gate refs,
HIL telemetry review refs, emergency-stop evidence, rollback plan refs, action
allowlist refs, operator responsibility acknowledgements, audit refs, and a
nested `limited_live_action_approval_package.v1`. If all preconditions are
present the status can become `operator_review_ready`, but
`stronger_execution_allowed=false`, `live_execution_allowed=false`,
`physical_execution_invoked=false`, `command_payload_allowed=false`, and
ROS/MAVLink/actuator dispatch flags remain false. The action allowlist is a set
of proposal categories for human review, not a dispatch permission.

## Mission Contract v2 Example

Mission work still enters through a `control_supervisor` task. The contract adds
manifest-level policy fields to the existing task artifact instead of creating a
separate mission table:

```yaml
schema_version: mission_contract.v2
contract_id: mission-research-watch-001
objective: Keep a research topic watch current with cited, non-duplicative updates.
allowed_actions:
  - web.search
  - browser.read
  - memory.search
forbidden_actions:
  - external_account_post
  - credential_read
abort_conditions:
  - type: human_approval_required
  - type: guardrail_budget_exhausted
success_metrics:
  - important new sources are captured with citations
  - duplicate findings are not promoted to memory
risk_budget:
  max_runtime_hours: 12
  max_same_failure_retries: 2
capability_policy:
  allow:
    - web.search
    - browser.read
  approval_required:
    - shell.run
memory_policy:
  promote_only:
    - fact
    - procedure
    - failure_pattern
    - recovery_pattern
  never_promote:
    - raw_transcript
    - secret
  require_operator_approval: true
recovery_policy:
  max_retries_per_step: 2
  ladder:
    - observe_again
    - verify_state
    - retry_smaller_step
    - alternate_capability
    - request_approval
    - pause_or_block
improvement_policy:
  mode: canary_only
  require_benchmark_pass: true
  require_human_promotion: true
```

## Recovery Ladder

Recovery Ladder v1 is the live bridge from verifier/tool failures to bounded
runtime behavior. A failed tick is classified, mapped to a typed ladder step,
checked against `mission_contract.recovery_policy`, and persisted as a
`recovery_decision` artifact in `durable_execution.recovery_decisions[]`.

Initial steps are:

```text
observe_again
verify_state
retry_same_step
retry_smaller_step
alternate_capability
diagnostic_task
request_approval
pause_or_block
create_improvement_candidate
```

Each persisted decision records the selected step, reason, attempt index,
budget before/after snapshots, outcome, and source refs to task/verifier/runtime
evidence when available. Non-terminal steps preserve the existing scheduler
policy; `request_approval` pauses into approval wait, and exhausted policy or
budget becomes `blocked`.

State semantics remain explicit:

| State | Meaning | Resume path |
| --- | --- | --- |
| `failed` | Execution was attempted and failed. | Requires repair or a new run. |
| `blocked` | Policy, budget, safety, environment, or recovery exhaustion prevents progress. | Requires operator or policy/budget intervention. |
| `paused` | Approval or operator input is required before continuing. | Can resume after the approval/input is resolved. |
| `cancelled` | An operator explicitly stopped the mission. | Does not auto-resume. |

Post-mission review is the next read-only layer. It reads `mission_contract`,
`durable_execution`, scheduler state, checkpoints, job runs, verifier verdicts,
recovery decisions, budget state, escalations, child task refs, and scorecard
state, then writes a versioned `mission_review` artifact. The review summarizes
the mission outcome, failure buckets, repeated failure patterns, recovery
effectiveness, evidence quality, candidate-only improvement proposals,
candidate-only memory promotion proposals, recommended contract edits, and
source refs.
A `paused` review is intentionally interim: it captures the approval-wait
boundary for operators, and a later terminal `completed`, `blocked`, or `failed`
review may replace it after the mission resumes.

Memory promotion candidates are formal approval-gated artifacts, not promoted
memory. `mission_review.memory_promotion_candidates` remains candidate-only
review output for backward compatibility, while
`artifacts.memory_promotion_candidates` contains normalized records with:

- `approval_status`: `pending`, `approved`, `rejected`, `expired`, or
  `candidate_only`
- `source_task_id`, `source_artifact_ref`, and `source_refs`
- `last_verified_at`, `expires_at`, and `invalidation_rule`
- `approved_by`, `approved_at`, and `rejected_reason`

Pending and approved candidates are not used by planning, recovery, or mission
reuse in this layer. They only make the review-and-approval boundary durable for
future reuse-planner work.

The Control UI task detail reads the same task artifacts directly. For
`control_supervisor` missions it exposes the current mission status, active node,
task graph, scheduler queues, recovery decisions, verifier evidence refs,
approval waits, budget exhaustion, mission scorecard, post-mission review, and
approval-gated memory candidate state without adding a separate mission API.

Mission templates are a small preset layer on top of `MissionContract v2`.
They generate validated contracts from typed inputs and safe defaults, then hand
the resulting payload to the existing `control_supervisor` API. Templates do not
create missions, runs, tables, or execution behavior by themselves. Initial
presets cover observation review, weak-evidence probing, budget-exhaustion
probing, current-tab research reports, and repository maintenance review.
Overrides may narrow allowed actions or add metadata, but they preserve default
forbidden actions so a template cannot silently drop safety constraints.

Mission eval suites are the measurement layer for the same artifact substrate.
They are deterministic and repo-local: a suite reads existing mission artifacts,
emits a serializable `mission_eval_result.v1`, and the regression gate compares
baseline vs candidate results as `mission_regression_gate.v1`. The gate blocks
on candidate failures, artifact-shape incompatibility, security eval failure, or
metric regression, while still representing operator approval as required. This
is groundwork for future benchmark-gated promotion; it does not promote memory,
run canaries, reuse approved artifacts, add UI, or introduce mission storage.

Physical replay safety eval suites extend that measurement layer to the toy
grid-world simulator before any autonomy runner exists. They inspect existing
`toy_grid_world_replay_trace.v1` artifacts and block regressions such as live
execution flags, physical execution invocation, accepted obstacle/hazard moves,
missing or stale telemetry, scenario-mismatched telemetry, non-dry-run action
envelopes, online replay plans, governor/step result mismatches, or
non-deterministic replay hashes. They also expose telemetry freshness as an
explicit metric for regression gates. These suites are artifact checks only;
they do not add simulator execution endpoints, UI controls, ROS dispatch,
actuator execution, `/missions`, or a `missions` table.

Toy grid-world autonomy plans are the next plan-only layer. An
`autonomy_plan.v1` records a deterministic bounded path over the 2D grid,
including constraints, safety assumptions, predicted final position, and a
blocked reason when no safe or budget-feasible path exists. The planner avoids
obstacles and hazards, respects battery and step budgets, and keeps
`execution_allowed=false` with operator approval required. It does not step the
simulator, mutate state, invoke replay, register policies, dispatch ROS, or
touch live hardware.

Toy grid-world autonomous episodes are the first simulator-only movement layer.
An `autonomous_episode.v1` consumes an `autonomy_plan.v1` inside the local toy
grid-world only, emitting `autonomous_step.v1` records for each attempted move.
Every step carries telemetry, a safety governor decision, and, when accepted,
dry-run action envelope plus offline replay plan evidence. The episode stops on
goal reached, blocked action, or step budget exhaustion, and records a
`toy_grid_world_replay_trace.v1` for eval suites. It keeps
`live_execution_allowed=false`, `physical_execution_invoked=false`, and
operator approval required; it does not dispatch ROS, touch hardware, add UI
controls, or integrate with runtime mission execution.

Toy grid-world autonomy reviews score those simulator-only episodes before any
stronger autonomy layer is allowed. An `autonomy_scorecard.v1` records goal
completion, safety violations, blocked steps, recovery/replan counts, dry-run
compliance, telemetry freshness, live/physical execution flags, and path
efficiency. An `autonomy_episode_review.v1` snapshots that scorecard, buckets
failures such as `unsafe_plan`, `blocked_by_governor`, `low_battery`,
`stale_telemetry`, `mismatch_telemetry`, and `replay_not_deterministic`, and can
emit improvement proposals only as `candidate_only`. This layer does not
promote artifacts, register skills/capabilities/policies, reuse runtime memory,
add UI controls, or permit live physical execution.

Toy grid-world autonomy gate results aggregate those episode artifacts into a
single rule-based decision before any stronger execution mode is considered. An
`autonomy_gate_result.v1` snapshots the scorecard and review, links safety eval
results, records blocking reasons such as live execution flags, physical
execution flags, safety violations, dry-run non-compliance, telemetry failures,
or non-deterministic replay, and always keeps `operator_approval_required=true`
with `stronger_execution_allowed=false`. It is not an LLM judge, does not
promote artifacts, does not reuse runtime memory, and does not permit live
physical execution.

Simulator adapter contracts are the first design slice toward a second
simulator. A `simulator_adapter_contract.v1` is a static declaration that
each adapter publishes about itself: which `state` / `action` / `telemetry` /
`governor` / `episode` / `replay_trace` schema versions it speaks, which
execution capabilities it does NOT support, and which mode it advertises. The
toy-grid adapter contract pins `supports_live_execution=false`,
`supports_physical_execution=false`, `supports_ros_dispatch=false`,
`operator_approval_required=true`, and `adapter_mode=dry_run_only` at the
type level so a contract that advertises stronger capabilities cannot be
constructed through this slice. The toy-grid autonomy episode runner and
the safety regression gate entry-point now consult the contract before
running, fail closed on any mismatch via `SimulatorAdapterContractError`,
and record the contract's `adapter_id` / `schema_version` /
`simulator_kind` / `mode` in episode and gate metadata so any future UI /
audit / scorecard can see which adapter contract a run was bound to.

HIL telemetry-only contracts are a separate, read-only ingestion surface
intended for real or real-equivalent hardware. *HIL means
hardware-in-the-loop. In this slice it is telemetry-only and read-only.*
A `hil_telemetry_contract.v1` advertises `supports_action_dispatch=false`,
`supports_command_payload=false`, `supports_live_execution=false`,
`supports_physical_execution=false`, `supports_ros_dispatch=false`,
`operator_approval_required=true`, and `mode=telemetry_only` at the type
level. The matching `hil_telemetry_envelope.v1` deliberately has no
`action` / `command` / `actuator` / `dispatch` field and only carries
scalar measurements (`float | int | bool | str`); unknown fields are
rejected by Pydantic `extra="forbid"`. The ingestion function
`ingest_hil_telemetry_envelope` adds a recursive, case-insensitive
pre-check that fails closed on any command-like key
(`action` / `command` / `actuator` / `dispatch` / `ros_topic` /
`ros2_topic` / `execute` / `execute_now` / `live_execution_allowed` /
`physical_execution_invoked`) appearing anywhere in the payload — top
level, nested in `metadata` / `measurements`, or inside a list. The
boundary is therefore physical, not flag-based: the simulator-adapter
contract type and the HIL telemetry contract type are distinct, and the
HIL telemetry path cannot accept an action-like payload by construction.

The HIL evidence slice attaches accepted telemetry as read-only Mission OS
artifacts. `hil_telemetry_evidence.v1` is built only after
`ingest_hil_telemetry_envelope(...)` accepts the payload. It snapshots the
envelope, records contract / envelope refs, measurement keys, freshness seconds,
stale gate/review findings, and fixed no-action / no-command / no-ROS /
no-live / no-physical flags. `attach_hil_telemetry_artifacts(...)` can merge
`hil_telemetry_contract`, `hil_telemetry_envelope`, and
`hil_telemetry_evidence` into an existing task without changing task status,
approval state, promotion state, reuse state, UI behavior, ROS dispatch,
actuator dispatch, or live execution. Command-like or malformed payloads reject
before any task update. This slice still does not connect to real hardware, ROS,
or any actuator. The Control UI can display the accepted HIL contract, envelope,
evidence, freshness, findings, and fixed safety-boundary flags as read-only
task detail context; it does not add approval, command, dispatch, or execution
controls.

The HIL telemetry review layer is the gate / scorecard input contract above
that evidence. A `hil_telemetry_review.v1` aggregates one or more
`hil_telemetry_evidence.v1` artifacts into a single deterministic pass /
block decision plus typed findings: stale evidence becomes
`hil_telemetry_stale`, an evidence with no measurements becomes
`hil_telemetry_malformed`, an empty input list under `required=true` becomes
`hil_telemetry_missing`, and any non-zero count of payloads the ingestion
path refused for command-like content becomes `command_payload_rejected`.
Each finding carries a `bucket` / `reason` / `severity` and a structured
`detail` so a future safety gate can read HIL telemetry health uniformly
across subjects. The review pins `operator_approval_required=true`,
`live_execution_allowed=false`, `physical_execution_invoked=false`, and
`command_payload_allowed=false` at the type level via Pydantic `Literal`.
This layer is artifact-only and rule-based: there is no LLM judge, no
external hardware connection, no ROS / actuator / dispatch path, no
mission API, no promotion, and no runtime reuse. Wiring the review into
`autonomy_gate_result.v1` is a separate slice; this slice produces the
contract that such a gate would read.

The HIL mock telemetry source closes the chain end-to-end without
touching real hardware. `build_mock_hil_telemetry_envelope`,
`build_mock_hil_telemetry_chain`, and `attach_mock_hil_telemetry_chain`
produce deterministic envelopes that go through the same
`ingest_hil_telemetry_envelope` path as production: command-like keys
are still refused (`HilTelemetryRejected` propagates before the chain
returns), and the resulting `hil_telemetry_envelope` /
`hil_telemetry_evidence` / `hil_telemetry_review` artifacts feed the
existing autonomy gate wiring. The attach helper raises before any task
update, so a rejected payload never pollutes `task.artifacts`. The mock
source is fixture-grade: there is no runtime polling, no source
identity registry, no live transport (no PX4 / MAVLink / ROS / SSE),
and no actuator / dispatch surface. Its purpose is to give tests, demos,
and operators a way to exercise the full read-only HIL chain
deterministically while the boundary stays type-pinned to
`telemetry_only`.

The limited live action design layer sits after the HIL review and autonomy
gate artifacts. It is a rule-based checklist for future operator review, not an
execution grant: missing autonomy-gate, HIL-review, emergency-stop, rollback,
allowlist, responsibility, or audit evidence blocks the gate with explicit
`missing_precondition:*` reasons, and even a fully populated evidence package
only prepares human review. No command payload, dispatch implementation,
ROS/MAVLink path, or actuator surface is introduced by this layer.

The Control UI can render `limited_live_action_gate.v1` and its nested
`limited_live_action_approval_package.v1` as read-only task detail artifacts.
Operators can inspect status, missing preconditions, evidence refs,
proposal-category allowlist scope, approval-required flags, and the pinned
no-live / no-physical / no-command / no-dispatch safety boundary without gaining
any approval button or action control.

`limited_live_action_rehearsal.v1` is the final dry-run / evidence package
before any future limited live action can be considered. It bundles the gate,
approval package, mission contract ref, HIL review ref, emergency-stop evidence,
rollback plan, operator responsibility acknowledgement, and audit refs. Missing
evidence blocks the rehearsal; a ready rehearsal still only means
`ready_for_operator_review`, with live execution and physical invocation pinned
to false. The Control UI renders this rehearsal package read-only and does not
add approval, command, dispatch, or execution controls.

`tenth_stage_readiness_check.v1` is the pre-10合目 checklist over that
rehearsal package. It adds the external readiness refs that boiled-claw cannot
itself satisfy: adopting organization, hardware owner, certified/autopilot
controller, and emergency-stop process. A complete checklist can become
`ready_for_organization_review`, but `live_action_status` remains
`blocked_for_live_action` because approval is not performed and no dispatch
implementation exists. The Control UI renders the checklist read-only and adds
no approval, command, dispatch, or execution controls.

Promotion packages are the artifact-only bridge from post-mission review to a
future promotion pipeline. `mission_review.improvement_candidates` can be
normalized into typed promotion candidates, mapped to required eval suites, and
packaged with baseline/candidate `mission_eval_result.v1`,
`mission_regression_gate.v1`, and optional security eval evidence. The package
always remains `approval_status=pending` and `requires_operator_approval=true`.
It makes the benchmark-gate decision inspectable, but it does not create
approved memory, skills, capabilities, policies, code patches, runtime reuse,
UI behavior, `/missions`, or a `missions` table.

Approved promotion artifacts are the next artifact-only layer. An
approval-ready `promotion_package.v1` can become one of four typed artifacts
after explicit operator approval:

- `approved_improvement_memory.v1`: retrieval-only improvement knowledge.
- `approved_skill.v1`: a bounded reusable recipe.
- `capability_patch.v1`: a typed capability proposal that still requires
  runtime registration before use.
- `policy_patch.v1`: a safety or scope policy proposal.

The approval path preserves source package refs, benchmark refs, security eval
refs, approval metadata, expiry/invalidation metadata, and target-specific
schema fields. Listing helpers can filter usable approved artifacts by type and
ignore rejected or expired records. This still does not wire approved artifacts
into planning, recovery, runtime registration, or mission reuse.

Mission reuse plans close the artifact loop without changing runtime behavior.
A `reuse_plan.v1` compares a new `MissionContract` against usable approved
artifacts, selects relevant memories, skills, policy patches, and capability
patches, and records why each artifact was selected or excluded. The plan
preserves expiry checks, invalidation checks, policy checks, matched terms, and
operator-visible provenance. Capability and policy patches are selected only as
visible plan entries; they are not automatically registered or applied.
When approved promotion artifacts are supplied at `control_supervisor` mission
start, the Gateway records the generated `reuse_plan.v1` on task artifacts and
`durable_execution` and emits a `mission_reuse_plan_recorded` timeline event.
The Control UI task detail renders the selected memories, skills, policy
patches, capability patches, excluded candidates, expiry checks, policy checks,
and reuse-plan history. This remains a visibility path, not runtime reuse.

Non-goals for this layer: no automatic promotion, no hidden runtime reuse, no
canary benchmark execution, no runtime application from the UI, no `/missions`
API, and no `missions` table.

## Current Maturity

boiled-claw already has the main contract surfaces: task objects, audit events,
approvals, trajectories, scheduler state, durable execution artifacts, live
supervisor resume, typed mission abort conditions, and simulation-first physical
runtime artifacts. Physical replay now has an artifact-only Mission OS path for
simulation scenario requests, telemetry health snapshots, safety governor
decisions, dry-run action envelopes, offline replay plans, and a deterministic
2D grid-world simulator; it does not apply those artifacts to live hardware.

The system is still a reference implementation. The live mission runtime is
active for control supervisors, but the project does not claim a distributed
scheduler, production robotics autonomy, or approval-free self-modification.

## Where To Read Next

- [Root architecture deep dive](../ARCHITECTURE.md)
- [Trajectory-native self-improving runtime](trajectory-native-self-improving-runtime.md)
- [Routing and execution](routing-and-execution.md)
- [Control loop and memory](control-loop-and-memory.md)
- [Host and desktop bridge](host-and-desktop-bridge.md)
