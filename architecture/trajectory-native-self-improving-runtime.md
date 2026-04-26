# Trajectory-Native Self-Improving Runtime Blueprint

## Decision

The next important boiled-claw milestone is not broader channel coverage.
It is a deeper closed loop:

`execute -> capture trajectory -> verify -> classify failure -> generate repair candidate -> benchmark in canary -> approve -> promote -> reuse`

That loop is the differentiator that can turn boiled-claw from "an agent UI with tools" into a self-improving browser-first / physical-ready runtime.

## Why This Is the Next Spine

The repository already ships the first slice of every required layer:

- browser-first computer use and trajectory capture in `src/tools/computer.py`, `src/tools/current_tab.py`, `src/browser/current_tab_bridge.py`, and `src/computer_use/trajectory_store.py`
- verification-driven control-loop recovery in `src/control_loop/`
- replay, task, and analytics surfaces in `src/gateway/task_replay.py`, `src/gateway/task_analytics.py`, `src/runtime/replay_schema.py`, and `src/runtime/task_store.py`
- offline canary self-improvement flows in `src/tools/self_improvement.py` and `src/tools/self_improvement_runtime/`
- approvals, audit, and policy enforcement in `src/runtime/approval_engine.py` and `src/security/`
- simulation-first physical replay in `src/tools/physical_ai.py` and `src/physical_ai/validation_store.py`

The missing piece is not another integration surface. It is the backbone that makes failures measurable, replayable, repairable, and safely promotable.

## Product Thesis

boiled-claw should be developed as a **trajectory-native self-improving agent runtime** with these properties:

- local-first and operator-visible
- browser-first, with current-tab preference and desktop fallback
- policy-bounded, with approvals and audit built into promotion
- capable of reusing one trajectory schema across browser, desktop, and simulation-first physical adapters

## Durable Execution Is the Substrate

This blueprint is about the self-improvement spine, not about claiming that the
entire durable-execution runtime is already complete.

boiled-claw already has the beginnings of a durable substrate in its task store,
replay/resume artifacts, approval queues, audit trail, and scheduler surfaces.
Those pieces are the base layer that makes longer-running work resumable and
inspectable. The work in PR #83 sits on top of that layer:

```text
durable execution substrate
  -> trajectory capture
  -> eval / failure classification
  -> canary benchmark
  -> promotion / reuse
```

That distinction matters for scope. The goal here is not to ship a full task
graph / checkpoint / budgeted workflow engine in one step. The goal is to make
existing durable artifacts feed a measurable self-improvement loop.

The next minimal substrate slice after PR #83 is therefore:

```text
goal
  -> durable task_graph
  -> bounded job
  -> verifier verdict (pass | fail | uncertain)
  -> checkpoint
  -> resume state
```

This is enough to support #84, #85, and #87 without pulling scheduler policy,
budget enforcement, or human escalation into the same implementation step.
In Phase 0, the emitted `task_graph`, `checkpoint`, `job_run`, and
`verifier_verdict` artifacts should be read as eval-derived substrate contracts,
not as proof that a live scheduler-backed runtime is already in place.

The next narrow substrate slice after that is #86, #88, #89, and #90:

```text
task_graph
  -> scheduler queue placement
  -> recovery policy / recovery decision
  -> guardrail budget state
  -> human escalation record
  -> resume state
```

These are still Phase 0 eval-derived orchestration artifacts. They are not a
distributed scheduler, worker fleet, or live background daemon yet. The point
is to make queueing / retry / approval / blocked-state contracts durable before
the worker implementation lands.

The next vertical slice after that is #92 and #93:

```text
current-tab Google Sheets long-running eval
  -> multiple bounded run_jobs
  -> trajectory/checkpoint/replay refs per run
  -> scheduler / approval / budget state in the persisted report
  -> Control UI task detail for operator inspection
```

This keeps the same rule intact: the UI is reading eval-derived durable
artifacts first, not claiming that a live scheduler daemon already exists.

## Priority Order

| Priority | Theme | Why now | Primary output |
| --- | --- | --- | --- |
| 1 | Trajectory-driven eval / replay spine | Makes failures measurable and turns the existing runtime into a benchmarkable system | `evals/` suites, `boiled-claw eval run`, replay-aware reporting |
| 2 | Failure-to-skill / capability promotion | Turns self-improvement from a demo into cumulative runtime learning | promoted skill / capability artifacts gated by evals |
| 3 | Safe self-improvement pipeline | Prevents benchmark gaming, unsafe repairs, and unreviewed promotion | policy checks, security evals, approval gates |
| 4 | Current-tab and desktop practical depth | Strengthens real tasks in the surface boiled-claw already claims | reliable task suites for Sheets, Docs, dashboards, and cross-app flows |
| 5 | Physical replay PoC | Extends the same control loop into simulation without overcommitting to robotics | trajectory-to-simulation task graph replay |
| 6 | Slack / WhatsApp / Voice / generic Canvas | Useful, but weak differentiation compared with the loop above | secondary after the vertical spine is in place |

## Backbone Architecture

### 1. Trajectory-Driven Eval / Replay Spine

Add a first-class eval layer that can replay realistic browser-first and desktop fallback tasks multiple times, then summarize both outcome and failure shape.

Proposed layout:

```text
evals/
  current_tab_google_sheets.yaml
  browser_form_fill.yaml
  desktop_file_edit.yaml
  multi_app_research_to_sheet.yaml
  hostile_web_prompt_injection.yaml
```

Representative spec:

```yaml
id: current_tab_research_to_sheet_001
goal: "Research in the current browser and write a summary into Google Sheets"
surfaces:
  - current_tab
  - managed_browser
  - desktop
success_criteria:
  - destination_url_contains: "docs.google.com/spreadsheets"
  - sheet_contains_any:
      - "Sources"
      - "Summary"
  - evidence_required: "destination_bound_text_or_screenshot"
runs: 5
judge:
  type: hybrid
  deterministic_checks: true
  llm_judge: optional
```

The eval runner should connect directly to the existing task and replay substrate instead of inventing a separate result plane:

- use trajectory records as the raw execution artifact
- attach task IDs so replay and analytics remain linked
- compare candidate runs against a baseline
- bucket failures so replay is actionable, not just visible

### 2. Formal Failure Taxonomy

Evals should produce explicit failure categories, not only pass/fail status.

Initial taxonomy:

```text
wrong_surface
target_context_mismatch
focus_mismatch
selector_drift
weak_evidence
verifier_false_positive
desktop_permission_missing
tool_timeout
policy_blocked
prompt_injection_detected
```

This taxonomy should live close to the trajectory / verification schema so it can be reused by:

- eval reporting
- repair prompting
- task analytics
- improvement memory retrieval
- future physical replay validation

The source of truth should be a **normalized failure classification** attached to the replay / eval artifact, not ad-hoc labels scattered across prompts or reports.

Classification lifecycle:

1. `verifier` emits a preliminary classification from the live execution result.
2. `replay analysis` can refine that classification using the full trajectory, evidence bundle, and repair context.
3. `promotion pipeline` consumes only the normalized classification, plus provenance for how it was derived.
4. `operator` can override the final label before promotion, with the override stored in audit history.

Downstream consumers should stay aligned with that lifecycle:

- reports summarize the normalized classification and its provenance
- promotion routing picks candidate artifact classes from the normalized bucket
- reuse and repair prompting consume the normalized bucket, never ad-hoc prompt labels

Illustrative shape:

```json
{
  "preliminary_failure_type": "weak_evidence",
  "normalized_failure_type": "weak_evidence",
  "classified_by": ["verifier", "replay_analysis"],
  "operator_override": null
}
```

### 3. Failure -> Skill / Capability Promotion

Failed trajectories should be able to generate structured candidate improvements that can be promoted only after benchmark success and operator approval.

Promotion artifacts should be treated as separate classes with different responsibilities:

| Artifact | Responsibility | Runtime effect | Typical home |
| --- | --- | --- | --- |
| `approved_skill` | reusable task recipe for a bounded workflow such as Sheets entry or dashboard extraction | planner / repair / reuse can register and prefer it as a recipe | `skills/` plus promoted metadata |
| `capability_patch` | typed execution surface improvement such as a new runtime action or stronger evidence-bearing tool path | changes the callable substrate itself after explicit runtime registration | `src/runtime/`, tool layers, registry surfaces |
| `approved_improvement_memory` | retrieval-friendly knowledge about a validated fix or failure pattern | influences future repair prompting and candidate selection, but does not create a new call surface | promoted memory store |
| `policy_patch` | execution or promotion constraint such as anti-cheat or scope restrictions | narrows or governs what is allowed to run or be promoted via security gates | policy store / security layer |

These artifacts are related, but they are not interchangeable:

- `approved_skill` packages reusable procedure
- `capability_patch` changes the typed runtime substrate
- `approved_improvement_memory` stores reusable improvement knowledge
- `policy_patch` constrains execution or promotion rather than improving task skill directly

Phase 0 reuse for `approved_improvement_memory` should stay evidence-bearing and inspectable:

- matching should prefer normalized `failure_type` plus trajectory hints such as selector, action, surface, and trajectory key before semantic fallback
- demo/search/eval reports should expose `reuse_memory_ids` and `reuse_policy`
- persisted trajectories should keep a `reuse_trace` that records which approved memories were considered or used
- policy may disable reuse entirely for a trajectory without blocking the rest of the repair flow

Illustrative promotion shape:

```text
failed trajectory
  -> candidate repair
  -> canary worktree
  -> benchmark suite
  -> operator approval
  -> approved_skill / capability_patch / approved_improvement_memory / policy_patch
  -> reuse in future repair prompts
```

The promotion boundary matters: a successful benchmark should not only save a diff package, it should produce something the runtime can call again with less prompt fragility.

### 4. Policy-Bounded Self-Improvement

Candidate changes should pass a fixed promotion contract before they are eligible for reuse:

1. Target eval improves.
2. Regression evals do not degrade materially.
3. Security evals pass.
4. Verifier false-positive rate does not increase.
5. Audit / tool policy rules are not violated.
6. Real-environment promotion requires explicit operator approval.

The corresponding policy memory should explicitly block common cheating and escalation paths:

```yaml
policy_memory:
  - id: no_eval_harness_mutation
    rule: "self-improvement candidates must not modify eval scoring code"
  - id: no_gold_access
    rule: "agents must not access hidden answer files or evaluator internals"
  - id: no_unscoped_shell
    rule: "shell mutation requires path-scoped approval"
```

This keeps boiled-claw differentiated as a self-improving runtime with explicit safety boundaries, not an unconstrained self-editing agent.

### 5. Current-Tab Preserving Practical Tasks

The next practical task suites should deepen the browser-first substrate that already exists:

1. Web research -> Google Sheets
2. Web research -> Google Docs
3. Read from the current tab -> write into another tab
4. Extract CSV / table data from SaaS dashboards
5. Multi-app desktop + browser workflows

Surface selection should itself become a first-class artifact:

```json
{
  "goal": "Write research findings into Google Sheets",
  "selected_surface": "current_tab",
  "fallback_chain": ["current_tab", "desktop_ax", "desktop_screenshot"],
  "why_not_managed_browser": "user requested the current browser session",
  "evidence_strength": "destination_bound_text_and_screenshot",
  "repair_trigger": "weak_sheet_text_evidence"
}
```

That keeps current-tab preference, desktop fallback, and verification logic inspectable instead of burying them inside prompt state.

### 6. Physical Replay PoC

Physical AI work should stay narrow until the eval / promotion backbone is solid.
The immediate goal is not a robotics stack; it is replaying the same closed loop into simulation-first validation.

Target shape:

```text
computer-use trajectory
  -> mission contract
  -> simulation scenario request
  -> verifier result + telemetry health
  -> safety governor decision
  -> ROS2 dry-run action envelope
  -> offline replay plan
```

Representative contract:

```json
{
  "mission_contract": {
    "objective": {"type": "inspection", "target": "rack_a"},
    "allowed_actions": ["submit_simulation", "capture_image", "build_action_envelope"],
    "forbidden_actions": ["direct_motor_control", "modify_controller"],
    "abort_conditions": ["battery_below_reserve", "human_too_close", "localization_lost"],
    "completion_criteria": ["all_required_targets_observed", "mission_report_generated"]
  },
  "verifier_result": {
    "verdict": "uncertain",
    "telemetry_health": {"battery": "ok", "localization": "ok", "safety": "nominal"},
    "recommended_action": "hold_for_additional_validation"
  },
  "governor_decision": {
    "decision": "require_operator",
    "reasons": ["validation_uncertain"]
  },
  "replay_plan": {
    "offline_only": true,
    "benchmark_required": true,
    "safety_regression_required": true,
    "operator_approval_required": true,
    "live_self_modification_allowed": false
  }
}
```

This preserves the broader thesis: browser-first computer use and simulation-first physical AI should share a control-plane vocabulary.
It should be read as a simulation-first contract surface, not as a claim that boiled-claw already ships a live robotics scheduler or direct motor controller.

## Minimum First Slice

Before the full multi-phase roadmap, boiled-claw should land one narrow end-to-end slice that proves the loop with minimal moving parts.

Suggested Phase 0:

```text
current-tab Google Sheets task
  -> trajectory saved
  -> normalized failure_type assigned
  -> replay-linked eval report
  -> candidate promotion artifacts identified
  -> one repair candidate tested in a canary
  -> operator approval
  -> approved_improvement_memory reused on the next similar repair
```

Concretely:

- add one eval spec: `evals/current_tab_google_sheets.yaml`
- ship the smallest viable `boiled-claw eval run`
- emit a replay-linked report from one run or a tiny repeated-run batch
- expose top-level `durable_execution.task_graph` and `durable_execution.resume_state`
- expose `run_jobs` entries with `trajectory_id`, `verifier_result`, `verifier_verdict`, `failure_type`, `recommended_repair_targets`, `candidate_promotion_artifacts`, `replay_reference`, `checkpoint`, and `job_run`
- keep `unsafe` reserved for future physical verifier integration rather than claiming it is produced by the browser-first Phase 0 slice
- start with only three failure buckets: `weak_evidence`, `focus_mismatch`, `target_context_mismatch`
- promote only to `approved_improvement_memory` first, not the full artifact matrix

This keeps the first implementation PR small while still proving that a failed trajectory can become reusable runtime guidance.

## Implementation Plan

### Phase 1: Build the Eval Spine

Deliverables:

- `evals/` suite definitions
- `boiled-claw eval run <spec>` and `boiled-claw eval report`
- repeated-run execution with baseline comparison
- failure buckets linked to replay artifacts

Likely touchpoints:

- `src/cli/`
- `src/computer_use/trajectory_store.py`
- `src/gateway/task_replay.py`
- `src/gateway/task_analytics.py`
- `src/runtime/replay_schema.py`
- `src/runtime/verification_schema.py`

### Phase 2: Formalize Failure Taxonomy

Deliverables:

- shared failure enum / schema
- trajectory annotation support
- report aggregation by failure bucket
- repair prompt inputs grounded in failure class
- lifecycle support for preliminary vs normalized failure classification
- operator override path with audit visibility

Likely touchpoints:

- `src/tools/computer.py`
- `src/control_loop/verifier.py`
- `src/control_loop/repair.py`
- `src/runtime/verification_schema.py`

### Phase 3: Add Promotion Targets

Deliverables:

- explicit artifact schemas for `approved_skill`, `capability_patch`, `approved_improvement_memory`, and `policy_patch` (artifact-only first slice before runtime reuse)
- `reuse_plan.v1` artifact that records selected approved artifacts, exclusions, expiry checks, invalidation checks, and operator-visible provenance
- candidate skill generation from failed trajectories
- capability registration path for approved promotions
- reuse hooks that prefer promoted skills over raw repair prompting
- clear routing on when a validated fix becomes memory only versus a promoted skill or capability patch

Likely touchpoints:

- `src/tools/self_improvement.py`
- `src/tools/self_improvement_runtime/`
- `src/memory_lifecycle/memory_schema.py`
- `src/memory_lifecycle/promoted_store.py`
- `src/runtime/capability_registry.py`
- `src/skills/`

### Phase 4: Add Security Evals to Promotion

Deliverables:

- anti-cheat checks around eval harness mutation and hidden-answer access
- security regression suites for prompt injection, file leakage, and unscoped shell use
- promotion gates that block approval-free deployment

Likely touchpoints:

- `src/security/`
- `src/runtime/approval_engine.py`
- `src/tools/self_improvement_runtime/common.py`
- `src/tools/self_improvement_runtime/canary.py`

### Phase 5: Ship Physical Replay v1

Deliverables:

- generic task-graph serializer from computer trajectories
- simulation request adapter
- validation-to-verification bridge
- ROS2 dry-run envelope generation from validated runs

Likely touchpoints:

- `src/tools/physical_ai.py`
- `src/physical_ai/runtime_schema.py`
- `src/runtime/replay_schema.py`
- `src/physical_ai/validation_store.py`

## Success Criteria

This blueprint is succeeding when boiled-claw can show all of the following in one coherent flow:

- a failed browser-first or desktop fallback task can be replayed reliably
- the failure is classified into a reusable taxonomy bucket with a clear normalized source of truth
- the runtime proposes a bounded repair candidate
- the candidate is tested in canaries against target, regression, and security evals
- promotion requires explicit approval and leaves an audit trail
- the promoted result is reused later as an approved skill, capability patch, policy patch, or approved improvement memory according to its artifact class

## Non-Goals for the Next Phase

The following are intentionally secondary until the backbone above exists:

- adding more chat surfaces for parity alone
- broadening into a full robotics stack
- replacing the current control plane with a generic workflow engine
- hiding repair and promotion logic behind opaque automation

## References

- [boiled-claw-public](https://github.com/pome223/boiled-claw-public)
- [Browser Use eval infrastructure](https://browser-use.com/posts/our-browser-agent-evaluation-system)
- [WALT: Web Agents that Learn Tools](https://arxiv.org/html/2510.01524v1)
- [Berkeley RDI on benchmark trustworthiness](https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [OpenAI Computer Use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
- [NVIDIA Isaac Sim ROS 2 docs](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/ros2_landing_page.html)
- [Model Context Protocol specification](https://modelcontextprotocol.io/specification/2025-11-25)
