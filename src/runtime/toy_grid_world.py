"""Toy 2D grid-world simulator for simulation-first physical replay.

The simulator is intentionally local and deterministic. It provides a small
retro top-down world for exercising physical replay artifacts without invoking
hardware, ROS, actuators, or external simulator adapters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from html import escape
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.runtime.mission_contract import build_mission_contract
from src.runtime.physical_mission_replay import (
    DryRunActionEnvelope,
    OfflineReplayPlan,
    SafetyGovernorDecisionArtifact,
    SafetyGovernorStatus,
    SimulationScenarioRequest,
    TelemetryHealthSnapshot,
    build_dry_run_action_envelope,
    build_offline_replay_plan,
    build_safety_governor_decision_artifact,
    build_simulation_scenario_request,
    build_telemetry_health_snapshot,
)

TOY_GRID_WORLD_STATE_SCHEMA_VERSION = "toy_grid_world_state.v1"
TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION = "toy_grid_world_step_result.v1"
TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION = "toy_grid_world_replay_trace.v1"

_AUTO_TELEMETRY = object()


class ToyGridWorldError(ValueError):
    """Raised when the toy simulator receives an invalid map or action."""


class ToyGridWorldAction(str, Enum):
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    WAIT = "wait"


class ToyGridWorldStatus(str, Enum):
    RUNNING = "running"
    GOAL_REACHED = "goal_reached"
    BLOCKED = "blocked"


class ToyGridWorldPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int
    y: int


class ToyGridWorldState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_STATE_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_STATE_SCHEMA_VERSION
    )
    world_id: str
    width: int
    height: int
    agent_position: ToyGridWorldPosition
    goal_position: ToyGridWorldPosition
    obstacles: list[ToyGridWorldPosition] = Field(default_factory=list)
    hazards: list[ToyGridWorldPosition] = Field(default_factory=list)
    battery: int = 100
    low_battery_threshold: int = 20
    step_count: int = 0
    max_steps: int = 100
    status: ToyGridWorldStatus = ToyGridWorldStatus.RUNNING
    last_block_reason: str = ""
    path_trace: list[ToyGridWorldPosition] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("obstacles", "hazards", "path_trace", mode="before")
    @classmethod
    def _normalize_position_list(cls, value: Any) -> list[ToyGridWorldPosition]:
        return _position_list(value)

    @field_validator("agent_position", "goal_position", mode="before")
    @classmethod
    def _normalize_position(cls, value: Any) -> ToyGridWorldPosition:
        return _position(value)

    @model_validator(mode="after")
    def _validate_grid(self) -> "ToyGridWorldState":
        if self.width <= 0 or self.height <= 0:
            raise ToyGridWorldError("grid width and height must be positive")
        if self.battery < 0 or self.battery > 100:
            raise ToyGridWorldError("battery must be between 0 and 100")
        if self.low_battery_threshold < 0 or self.low_battery_threshold > 100:
            raise ToyGridWorldError("low_battery_threshold must be between 0 and 100")
        if self.max_steps <= 0:
            raise ToyGridWorldError("max_steps must be positive")
        for name, position in (
            ("agent_position", self.agent_position),
            ("goal_position", self.goal_position),
        ):
            if not _in_bounds(position, self.width, self.height):
                raise ToyGridWorldError(f"{name} must be inside the grid")
        for name, positions in (("obstacles", self.obstacles), ("hazards", self.hazards)):
            seen: set[tuple[int, int]] = set()
            for position in positions:
                key = _position_key(position)
                if key in seen:
                    raise ToyGridWorldError(f"{name} contains duplicate position {key}")
                seen.add(key)
                if not _in_bounds(position, self.width, self.height):
                    raise ToyGridWorldError(f"{name} position {key} must be inside the grid")
        blocked = {_position_key(item) for item in self.obstacles}
        hazardous = {_position_key(item) for item in self.hazards}
        if blocked & hazardous:
            raise ToyGridWorldError("obstacles and hazards cannot overlap")
        if _position_key(self.agent_position) in blocked:
            raise ToyGridWorldError("agent cannot start inside an obstacle")
        if _position_key(self.agent_position) in hazardous:
            raise ToyGridWorldError("agent cannot start inside a hazard")
        if _position_key(self.goal_position) in blocked:
            raise ToyGridWorldError("goal cannot be inside an obstacle")
        if _position_key(self.goal_position) in hazardous:
            raise ToyGridWorldError("goal cannot be inside a hazard")
        if not self.path_trace:
            self.path_trace = [self.agent_position]
        return self


class ToyGridWorldStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION
    )
    action: ToyGridWorldAction
    accepted: bool
    blocked_reason: str = ""
    previous_state: ToyGridWorldState
    next_state: ToyGridWorldState
    telemetry_health_snapshot: TelemetryHealthSnapshot
    safety_governor_decision: SafetyGovernorDecisionArtifact
    dry_run_action_envelope: DryRunActionEnvelope | None = None
    offline_replay_plan: OfflineReplayPlan | None = None
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION
    )
    trace_id: str
    initial_state: ToyGridWorldState
    actions: list[ToyGridWorldAction]
    steps: list[ToyGridWorldStepResult]
    final_state: ToyGridWorldState
    final_status: ToyGridWorldStatus
    deterministic_hash: str
    offline_replay_plan_ref: str = ""
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actions", mode="before")
    @classmethod
    def _normalize_actions(cls, value: Any) -> list[ToyGridWorldAction]:
        return [_action(item) for item in _as_list(value)]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _position(value: Any) -> ToyGridWorldPosition:
    if isinstance(value, ToyGridWorldPosition):
        return value
    if isinstance(value, dict):
        return ToyGridWorldPosition(x=int(value.get("x", 0)), y=int(value.get("y", 0)))
    if isinstance(value, tuple | list) and len(value) == 2:
        return ToyGridWorldPosition(x=int(value[0]), y=int(value[1]))
    raise ToyGridWorldError("position must be a {x, y} dict or a two-item tuple/list")


def _position_list(value: Any) -> list[ToyGridWorldPosition]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return [_position(item) for item in value]
    return [_position(value)]


def _position_key(position: ToyGridWorldPosition) -> tuple[int, int]:
    return (position.x, position.y)


def _in_bounds(position: ToyGridWorldPosition, width: int, height: int) -> bool:
    return 0 <= position.x < width and 0 <= position.y < height


def _action(value: ToyGridWorldAction | str) -> ToyGridWorldAction:
    if isinstance(value, ToyGridWorldAction):
        return value
    try:
        return ToyGridWorldAction(str(value))
    except ValueError as exc:
        raise ToyGridWorldError(f"unsupported grid-world action: {value}") from exc


def _next_position(
    position: ToyGridWorldPosition,
    action: ToyGridWorldAction,
) -> ToyGridWorldPosition:
    deltas = {
        ToyGridWorldAction.MOVE_UP: (0, -1),
        ToyGridWorldAction.MOVE_DOWN: (0, 1),
        ToyGridWorldAction.MOVE_LEFT: (-1, 0),
        ToyGridWorldAction.MOVE_RIGHT: (1, 0),
        ToyGridWorldAction.WAIT: (0, 0),
    }
    dx, dy = deltas[action]
    return ToyGridWorldPosition(x=position.x + dx, y=position.y + dy)


def _grid_world_source_refs(state: ToyGridWorldState) -> list[str]:
    return [f"toy_grid_world:{state.world_id}", f"toy_grid_world_step:{state.step_count}"]


def build_toy_grid_world_state(
    *,
    width: int = 8,
    height: int = 6,
    agent_position: ToyGridWorldPosition | dict[str, int] | tuple[int, int] = (0, 0),
    goal_position: ToyGridWorldPosition | dict[str, int] | tuple[int, int] = (7, 5),
    obstacles: list[Any] | None = None,
    hazards: list[Any] | None = None,
    battery: int = 100,
    low_battery_threshold: int = 20,
    max_steps: int = 100,
    world_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldState:
    payload_for_id = {
        "width": width,
        "height": height,
        "agent": _position(agent_position).model_dump(mode="json"),
        "goal": _position(goal_position).model_dump(mode="json"),
        "obstacles": [_position(item).model_dump(mode="json") for item in obstacles or []],
        "hazards": [_position(item).model_dump(mode="json") for item in hazards or []],
    }
    try:
        return ToyGridWorldState(
            world_id=world_id or _stable_id("toy_grid_world", payload_for_id),
            width=width,
            height=height,
            agent_position=agent_position,
            goal_position=goal_position,
            obstacles=obstacles or [],
            hazards=hazards or [],
            battery=battery,
            low_battery_threshold=low_battery_threshold,
            max_steps=max_steps,
            metadata={
                **(metadata or {}),
                "simulator": "toy_grid_world",
                "visual_style": "original_retro_top_down_pixel",
                "live_execution_allowed": False,
            },
        )
    except ValidationError as exc:
        raise ToyGridWorldError(str(exc)) from exc


def build_grid_world_simulation_scenario_request(
    state: ToyGridWorldState | dict[str, Any],
    *,
    action: ToyGridWorldAction | str | None = None,
    now: datetime | None = None,
) -> SimulationScenarioRequest:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action) if action is not None else ToyGridWorldAction.WAIT
    contract = build_mission_contract(
        contract_id=f"toy-grid-world-{current.world_id}",
        objective=(
            "Navigate the toy grid-world agent to the goal without entering "
            "obstacles, hazards, or low-battery unsafe states."
        ),
        allowed_actions=[item.value for item in ToyGridWorldAction],
        forbidden_actions=[
            "enter_obstacle",
            "enter_hazard",
            "leave_grid",
            "continue_with_low_battery",
            "live_actuator_execution",
            "direct_motor_control",
        ],
        completion_criteria=["agent_position_equals_goal"],
        evidence_requirements=[
            "grid_position",
            "path_trace",
            "telemetry_health_snapshot",
            "safety_governor_decision",
        ],
        metadata={"simulator": "toy_grid_world", "world_id": current.world_id},
    )
    return build_simulation_scenario_request(
        mission_contract=contract,
        trajectory={
            "id": f"{current.world_id}:{current.step_count}:{selected_action.value}",
            "action": selected_action.value,
            "status": current.status.value,
            "actions": [
                {
                    "type": "toy_grid_world_action",
                    "action": selected_action.value,
                    "agent_position": current.agent_position.model_dump(mode="json"),
                    "goal_position": current.goal_position.model_dump(mode="json"),
                }
            ],
        },
        metadata={
            "simulator": "toy_grid_world",
            "world_id": current.world_id,
            "step_count": current.step_count,
        },
        now=now,
    )


def build_grid_world_telemetry_snapshot(
    state: ToyGridWorldState | dict[str, Any],
    *,
    scenario_id: str = "",
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> TelemetryHealthSnapshot:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    current_time = now or datetime.now(timezone.utc)
    unsafe = current.status == ToyGridWorldStatus.BLOCKED
    low_battery = current.battery <= current.low_battery_threshold
    telemetry = {
        "observed_at": (observed_at or current_time).isoformat(),
        "signals": {
            "battery": "critical" if low_battery else "ok",
            "localization": "ok",
            "comms": "ok",
            "safety": "unsafe" if unsafe else "nominal",
        },
        "source_refs": _grid_world_source_refs(current),
    }
    snapshot = build_telemetry_health_snapshot(
        telemetry,
        scenario_id=scenario_id,
        now=current_time,
    )
    metadata = {
        **snapshot.metadata,
        "simulator": "toy_grid_world",
        "world_id": current.world_id,
        "position": current.agent_position.model_dump(mode="json"),
        "goal_position": current.goal_position.model_dump(mode="json"),
        "battery": current.battery,
        "step_count": current.step_count,
    }
    return snapshot.model_copy(update={"metadata": metadata})


def _action_block_reason(
    state: ToyGridWorldState,
    action: ToyGridWorldAction,
) -> str:
    if state.status != ToyGridWorldStatus.RUNNING:
        return "mission_not_running"
    if state.battery <= state.low_battery_threshold:
        return "low_battery"
    if state.step_count >= state.max_steps:
        return "max_steps_exhausted"
    proposed = _next_position(state.agent_position, action)
    if not _in_bounds(proposed, state.width, state.height):
        return "out_of_bounds"
    obstacle_keys = {_position_key(item) for item in state.obstacles}
    hazard_keys = {_position_key(item) for item in state.hazards}
    if _position_key(proposed) in obstacle_keys:
        return "obstacle"
    if _position_key(proposed) in hazard_keys:
        return "hazard"
    return ""


def build_grid_world_safety_governor_decision(
    state: ToyGridWorldState | dict[str, Any],
    action: ToyGridWorldAction | str,
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    telemetry_snapshot: TelemetryHealthSnapshot | dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> SafetyGovernorDecisionArtifact:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action)
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    if telemetry_snapshot is None:
        return build_safety_governor_decision_artifact(
            scenario,
            None,
            now=now,
        )
    telemetry = (
        telemetry_snapshot
        if isinstance(telemetry_snapshot, TelemetryHealthSnapshot)
        else TelemetryHealthSnapshot.model_validate(telemetry_snapshot)
    )
    if telemetry.scenario_id != scenario.scenario_id:
        reasons = [*telemetry.reasons, "telemetry_scenario_mismatch"]
        return SafetyGovernorDecisionArtifact(
            decision_id=_stable_id(
                "toy_grid_governor",
                {
                    "world_id": current.world_id,
                    "step": current.step_count,
                    "action": selected_action.value,
                    "scenario_id": scenario.scenario_id,
                    "telemetry_scenario_id": telemetry.scenario_id,
                    "reason": "telemetry_scenario_mismatch",
                    "telemetry": telemetry.snapshot_id,
                },
            ),
            scenario_id=scenario.scenario_id,
            decision=SafetyGovernorStatus.BLOCKED,
            reasons=reasons,
            telemetry_snapshot_id=telemetry.snapshot_id,
            checked_at=now or datetime.now(timezone.utc),
            source_refs=sorted(set(scenario.source_refs + telemetry.source_refs)),
            metadata={
                "simulator": "toy_grid_world",
                "world_id": current.world_id,
                "action": selected_action.value,
                "blocked_reason": "telemetry_scenario_mismatch",
                "telemetry_scenario_id": telemetry.scenario_id,
                "expected_scenario_id": scenario.scenario_id,
                "physical_execution_allowed": False,
            },
        )
    base_decision = build_safety_governor_decision_artifact(
        scenario,
        telemetry,
        now=now,
    )
    if base_decision.decision == SafetyGovernorStatus.BLOCKED:
        return base_decision

    block_reason = _action_block_reason(current, selected_action)
    if not block_reason:
        return base_decision

    reasons = [*telemetry.reasons, f"blocked_by_{block_reason}"]
    return SafetyGovernorDecisionArtifact(
        decision_id=_stable_id(
            "toy_grid_governor",
            {
                "world_id": current.world_id,
                "step": current.step_count,
                "action": selected_action.value,
                "reason": block_reason,
                "telemetry": telemetry.snapshot_id,
            },
        ),
        scenario_id=scenario.scenario_id,
        decision=SafetyGovernorStatus.BLOCKED,
        reasons=reasons,
        telemetry_snapshot_id=telemetry.snapshot_id,
        checked_at=now or datetime.now(timezone.utc),
        source_refs=sorted(set(scenario.source_refs + telemetry.source_refs)),
        metadata={
            "simulator": "toy_grid_world",
            "world_id": current.world_id,
            "action": selected_action.value,
            "blocked_reason": block_reason,
            "physical_execution_allowed": False,
        },
    )


def _apply_grid_world_action(
    state: ToyGridWorldState,
    action: ToyGridWorldAction,
) -> ToyGridWorldState:
    proposed = _next_position(state.agent_position, action)
    battery = max(0, state.battery - 1)
    status = (
        ToyGridWorldStatus.GOAL_REACHED
        if _position_key(proposed) == _position_key(state.goal_position)
        else ToyGridWorldStatus.RUNNING
    )
    return state.model_copy(
        update={
            "agent_position": proposed,
            "battery": battery,
            "step_count": state.step_count + 1,
            "status": status,
            "last_block_reason": "",
            "path_trace": [*state.path_trace, proposed],
        }
    )


def _blocked_state(
    state: ToyGridWorldState,
    reason: str,
) -> ToyGridWorldState:
    return state.model_copy(
        update={
            "status": ToyGridWorldStatus.BLOCKED,
            "last_block_reason": reason,
            "path_trace": list(state.path_trace),
        }
    )


def _blocked_reason_from_decision(decision: SafetyGovernorDecisionArtifact) -> str:
    for reason in decision.reasons:
        if reason.startswith("blocked_by_"):
            return reason.removeprefix("blocked_by_")
    return "safety_governor_blocked"


def step_toy_grid_world(
    state: ToyGridWorldState | dict[str, Any],
    action: ToyGridWorldAction | str,
    *,
    telemetry: TelemetryHealthSnapshot | dict[str, Any] | None | object = _AUTO_TELEMETRY,
    now: datetime | None = None,
) -> ToyGridWorldStepResult:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action)
    current_time = now or datetime.now(timezone.utc)
    scenario = build_grid_world_simulation_scenario_request(
        current,
        action=selected_action,
        now=current_time,
    )
    if telemetry is _AUTO_TELEMETRY:
        telemetry_snapshot = build_grid_world_telemetry_snapshot(
            current,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    elif telemetry is None:
        telemetry_snapshot = build_telemetry_health_snapshot(
            None,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    elif isinstance(telemetry, TelemetryHealthSnapshot):
        telemetry_snapshot = telemetry
    else:
        telemetry_snapshot = build_telemetry_health_snapshot(
            telemetry,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    governor = build_grid_world_safety_governor_decision(
        current,
        selected_action,
        scenario,
        telemetry_snapshot,
        now=current_time,
    )
    if governor.decision == SafetyGovernorStatus.BLOCKED:
        reason = _blocked_reason_from_decision(governor)
        return ToyGridWorldStepResult(
            action=selected_action,
            accepted=False,
            blocked_reason=reason,
            previous_state=current,
            next_state=_blocked_state(current, reason),
            telemetry_health_snapshot=telemetry_snapshot,
            safety_governor_decision=governor,
            created_at=current_time,
            metadata={"simulator": "toy_grid_world", "operator_approval_required": True},
        )

    proposed = _next_position(current.agent_position, selected_action)
    envelope = build_dry_run_action_envelope(
        scenario,
        governor,
        proposed_actions=[
            {
                "type": "toy_grid_world_action",
                "action": selected_action.value,
                "from": current.agent_position.model_dump(mode="json"),
                "to": proposed.model_dump(mode="json"),
                "dry_run": True,
            }
        ],
        now=current_time,
    )
    next_state = _apply_grid_world_action(current, selected_action)
    replay_plan = build_offline_replay_plan(
        scenario,
        telemetry_snapshot,
        governor,
        envelope,
        now=current_time,
    )
    return ToyGridWorldStepResult(
        action=selected_action,
        accepted=True,
        previous_state=current,
        next_state=next_state,
        telemetry_health_snapshot=telemetry_snapshot,
        safety_governor_decision=governor,
        dry_run_action_envelope=envelope,
        offline_replay_plan=replay_plan,
        created_at=current_time,
        metadata={
            "simulator": "toy_grid_world",
            "operator_approval_required": True,
            "physical_execution_allowed": False,
        },
    )


def run_toy_grid_world_replay(
    initial_state: ToyGridWorldState | dict[str, Any],
    actions: list[ToyGridWorldAction | str],
    *,
    now: datetime | None = None,
) -> ToyGridWorldReplayTrace:
    current = (
        initial_state
        if isinstance(initial_state, ToyGridWorldState)
        else ToyGridWorldState.model_validate(initial_state)
    )
    current_time = now or datetime.now(timezone.utc)
    selected_actions = [_action(item) for item in actions]
    steps: list[ToyGridWorldStepResult] = []
    offline_ref = ""
    for index, action in enumerate(selected_actions):
        step = step_toy_grid_world(
            current,
            action,
            now=current_time + timedelta(seconds=index),
        )
        steps.append(step)
        if step.offline_replay_plan is not None:
            offline_ref = f"offline_replay_plan:{step.offline_replay_plan.replay_plan_id}"
        current = step.next_state
        if current.status in {ToyGridWorldStatus.BLOCKED, ToyGridWorldStatus.GOAL_REACHED}:
            break

    hash_payload = {
        "initial": (
            initial_state.model_dump(mode="json")
            if isinstance(initial_state, ToyGridWorldState)
            else initial_state
        ),
        "actions": [item.value for item in selected_actions],
        "steps": [step.model_dump(mode="json") for step in steps],
        "final": current.model_dump(mode="json"),
    }
    deterministic_hash = sha256(
        json.dumps(hash_payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return ToyGridWorldReplayTrace(
        trace_id=_stable_id(
            "toy_grid_replay",
            {
                "world_id": current.world_id,
                "actions": [item.value for item in selected_actions],
                "hash": deterministic_hash,
            },
        ),
        initial_state=(
            initial_state
            if isinstance(initial_state, ToyGridWorldState)
            else ToyGridWorldState.model_validate(initial_state)
        ),
        actions=selected_actions,
        steps=steps,
        final_state=current,
        final_status=current.status,
        deterministic_hash=deterministic_hash,
        offline_replay_plan_ref=offline_ref,
        created_at=current_time,
        metadata={
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "operator_approval_required": True,
        },
    )


def render_toy_grid_world_svg(
    state: ToyGridWorldState | dict[str, Any],
    *,
    tile_size: int = 32,
) -> str:
    """Render an original retro top-down SVG view of the grid world.

    The renderer intentionally uses generated geometric tiles and no third-party
    game assets or franchise-specific characters.
    """

    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    size = max(16, int(tile_size))
    width_px = current.width * size
    height_px = current.height * size
    obstacle_keys = {_position_key(item) for item in current.obstacles}
    hazard_keys = {_position_key(item) for item in current.hazards}
    path_keys = {_position_key(item) for item in current.path_trace}
    agent_key = _position_key(current.agent_position)
    goal_key = _position_key(current.goal_position)
    cells: list[str] = []
    for y in range(current.height):
        for x in range(current.width):
            key = (x, y)
            fill = "#8fd16a"
            accent = "#a9df84"
            label = ""
            if key in path_keys:
                fill = "#b7dd7a"
            if key == goal_key:
                fill = "#f6d365"
                accent = "#fda085"
                label = "G"
            if key in hazard_keys:
                fill = "#b85c6b"
                accent = "#e88873"
                label = "!"
            if key in obstacle_keys:
                fill = "#52606d"
                accent = "#36454f"
                label = ""
            px = x * size
            py = y * size
            cells.append(
                f'<rect x="{px}" y="{py}" width="{size}" height="{size}" '
                f'fill="{fill}" stroke="#29422a" stroke-width="1"/>'
            )
            cells.append(
                f'<rect x="{px + 4}" y="{py + 4}" width="{max(2, size - 8)}" '
                f'height="{max(2, size - 8)}" fill="{accent}" opacity="0.28"/>'
            )
            if label:
                cells.append(
                    f'<text x="{px + size / 2:.1f}" y="{py + size * 0.66:.1f}" '
                    'font-family="monospace" font-size="16" text-anchor="middle" '
                    'fill="#263238">'
                    f"{escape(label)}</text>"
                )
    ax, ay = agent_key
    agent_x = ax * size
    agent_y = ay * size
    cells.append(
        f'<rect x="{agent_x + 8}" y="{agent_y + 8}" width="{size - 16}" '
        f'height="{size - 16}" fill="#2f80ed" stroke="#12355b" stroke-width="2"/>'
    )
    cells.append(
        f'<rect x="{agent_x + size / 2 - 4:.1f}" y="{agent_y + 4}" width="8" '
        'height="6" fill="#f5f5f5" stroke="#12355b" stroke-width="1"/>'
    )
    title = escape(f"Toy grid world {current.world_id}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" role="img" aria-label="{title}" '
        'shape-rendering="crispEdges">'
        '<rect width="100%" height="100%" fill="#223322"/>'
        f"{''.join(cells)}"
        "</svg>"
    )


__all__ = [
    "TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION",
    "TOY_GRID_WORLD_STATE_SCHEMA_VERSION",
    "TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION",
    "ToyGridWorldAction",
    "ToyGridWorldError",
    "ToyGridWorldPosition",
    "ToyGridWorldReplayTrace",
    "ToyGridWorldState",
    "ToyGridWorldStatus",
    "ToyGridWorldStepResult",
    "build_grid_world_safety_governor_decision",
    "build_grid_world_simulation_scenario_request",
    "build_grid_world_telemetry_snapshot",
    "build_toy_grid_world_state",
    "render_toy_grid_world_svg",
    "run_toy_grid_world_replay",
    "step_toy_grid_world",
]
