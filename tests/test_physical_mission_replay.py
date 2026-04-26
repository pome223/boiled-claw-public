from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path

import pytest

import src.runtime.physical_mission_replay as physical_replay
from src.runtime.mission_contract import build_mission_contract
from src.runtime.physical_mission_replay import (
    DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION,
    OFFLINE_REPLAY_PLAN_SCHEMA_VERSION,
    PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION,
    SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION,
    SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION,
    TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION,
    PhysicalMissionReplayError,
    SafetyGovernorStatus,
    TelemetryHealthStatus,
    build_dry_run_action_envelope,
    build_offline_replay_plan,
    build_safety_governor_decision_artifact,
    build_simulation_first_replay_artifacts,
    build_simulation_scenario_request,
    build_telemetry_health_snapshot,
)


NOW = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)


def _mission_contract():
    return build_mission_contract(
        contract_id="mission-physical-replay",
        objective="Replay browser trajectory in simulation before any physical work",
        allowed_actions=["submit_simulation", "inspect_replay_artifacts"],
        forbidden_actions=["direct_motor_control", "automatic_physical_deployment"],
        completion_criteria=["offline_replay_plan_generated"],
        evidence_requirements=["telemetry_health_snapshot", "safety_governor_decision"],
    )


def _task():
    contract = _mission_contract().model_dump(mode="json")
    return {
        "task_id": "task-physical-replay",
        "kind": "control_supervisor",
        "title": "Physical replay seed task",
        "artifacts": {
            "mission_contract": contract,
            "durable_execution": {"mission_contract": contract},
        },
    }


def _trajectory():
    return {
        "id": 42,
        "action": "current_tab sheet evidence failed verification",
        "status": "failed",
        "failure_type": "weak_evidence",
        "attempts": [
            {
                "surface": "current_tab",
                "strategy": "destination_bound_verification",
                "success": False,
            }
        ],
    }


def _safe_telemetry():
    return {
        "observed_at": NOW.isoformat(),
        "signals": {
            "battery": "ok",
            "localization": "ok",
            "comms": "ok",
            "safety": "nominal",
        },
        "source_refs": ["telemetry:window-1"],
    }


def test_trajectory_task_input_can_produce_simulation_scenario_request():
    scenario = build_simulation_scenario_request(
        task=_task(),
        trajectory=_trajectory(),
        now=NOW,
    )

    assert scenario.schema_version == SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION
    assert scenario.mission_task_id == "task-physical-replay"
    assert scenario.mission_contract_id == "mission-physical-replay"
    assert scenario.validation_mode == "simulation_first"
    assert "task:task-physical-replay" in scenario.source_refs
    assert "trajectory:42" in scenario.source_refs
    assert "live_actuator_execution" in scenario.forbidden_actions
    assert scenario.metadata["physical_execution_allowed"] is False


def test_scenario_metadata_cannot_enable_physical_execution():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        metadata={
            "artifact_only": False,
            "physical_execution_allowed": True,
            "live_execution_allowed": True,
        },
        now=NOW,
    )

    assert scenario.metadata["artifact_only"] is True
    assert scenario.metadata["physical_execution_allowed"] is False
    assert "live_execution_allowed" not in scenario.metadata


def test_telemetry_snapshot_can_be_attached_to_scenario():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        _safe_telemetry(),
        scenario_id=scenario.scenario_id,
        now=NOW,
    )

    assert telemetry.schema_version == TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION
    assert telemetry.scenario_id == scenario.scenario_id
    assert telemetry.status == TelemetryHealthStatus.NOMINAL
    assert telemetry.missing_signals == []
    assert "telemetry:window-1" in telemetry.source_refs


def test_missing_telemetry_blocks_safety_governor_decision():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        None,
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(
        scenario,
        telemetry,
        now=NOW,
    )

    assert telemetry.status == TelemetryHealthStatus.MISSING
    assert decision.schema_version == SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION
    assert decision.decision == SafetyGovernorStatus.BLOCKED
    assert "blocked_by_missing_telemetry" in decision.reasons
    assert decision.operator_approval_required is True
    assert decision.operator_approval_performed is False
    assert decision.live_execution_allowed is False


def test_telemetry_without_timestamp_blocks_by_default():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        {
            "signals": {
                "battery": "ok",
                "localization": "ok",
                "comms": "ok",
                "safety": "nominal",
            }
        },
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(
        scenario,
        telemetry,
        now=NOW,
    )

    assert telemetry.status == TelemetryHealthStatus.MALFORMED
    assert "telemetry_timestamp_missing" in telemetry.reasons
    assert decision.decision == SafetyGovernorStatus.BLOCKED
    assert "blocked_by_malformed_telemetry" in decision.reasons


def test_unsafe_telemetry_blocks_safety_governor_decision():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        {
            "observed_at": NOW.isoformat(),
            "signals": {
                "battery": "ok",
                "localization": "ok",
                "comms": "ok",
                "safety": "unsafe",
            },
        },
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(scenario, telemetry, now=NOW)

    assert telemetry.status == TelemetryHealthStatus.UNSAFE
    assert decision.decision == SafetyGovernorStatus.BLOCKED
    assert "blocked_by_unsafe_telemetry" in decision.reasons


def test_stale_and_malformed_telemetry_block_by_default():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    stale = build_telemetry_health_snapshot(
        {
            "observed_at": (NOW - timedelta(seconds=120)).isoformat(),
            "signals": {
                "battery": "ok",
                "localization": "ok",
                "comms": "ok",
                "safety": "nominal",
            },
        },
        scenario_id=scenario.scenario_id,
        max_age_seconds=60,
        now=NOW,
    )
    malformed = build_telemetry_health_snapshot(
        {"observed_at": NOW.isoformat(), "signals": {"battery": "ok"}},
        scenario_id=scenario.scenario_id,
        now=NOW,
    )

    assert stale.status == TelemetryHealthStatus.STALE
    assert malformed.status == TelemetryHealthStatus.MALFORMED
    assert (
        build_safety_governor_decision_artifact(scenario, stale, now=NOW).decision
        == SafetyGovernorStatus.BLOCKED
    )
    assert (
        build_safety_governor_decision_artifact(scenario, malformed, now=NOW).decision
        == SafetyGovernorStatus.BLOCKED
    )


def test_safe_telemetry_can_produce_dry_run_only_action_envelope():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        _safe_telemetry(),
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(scenario, telemetry, now=NOW)
    envelope = build_dry_run_action_envelope(scenario, decision, now=NOW)

    assert decision.decision == SafetyGovernorStatus.DRY_RUN_ALLOWED
    assert envelope.schema_version == DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION
    assert envelope.dry_run is True
    assert envelope.live_execution_allowed is False
    assert envelope.operator_approval_required is True
    assert envelope.operator_approval_performed is False
    assert envelope.physical_execution_invoked is False
    assert envelope.proposed_actions


def test_dry_run_action_envelope_rejects_direct_physical_execution_terms():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        _safe_telemetry(),
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(scenario, telemetry, now=NOW)

    with pytest.raises(PhysicalMissionReplayError, match="physical execution"):
        build_dry_run_action_envelope(
            scenario,
            decision,
            proposed_actions=[{"type": "direct_motor_command"}],
            now=NOW,
        )


def test_offline_replay_plan_is_generated_with_live_execution_disallowed():
    scenario = build_simulation_scenario_request(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        now=NOW,
    )
    telemetry = build_telemetry_health_snapshot(
        _safe_telemetry(),
        scenario_id=scenario.scenario_id,
        now=NOW,
    )
    decision = build_safety_governor_decision_artifact(scenario, telemetry, now=NOW)
    envelope = build_dry_run_action_envelope(scenario, decision, now=NOW)
    replay_plan = build_offline_replay_plan(
        scenario,
        telemetry,
        decision,
        envelope,
        now=NOW,
    )

    assert replay_plan.schema_version == OFFLINE_REPLAY_PLAN_SCHEMA_VERSION
    assert replay_plan.offline_only is True
    assert replay_plan.live_execution_allowed is False
    assert replay_plan.benchmark_required is True
    assert replay_plan.safety_regression_required is True
    assert replay_plan.operator_approval_required is True
    assert replay_plan.operator_approval_performed is False
    assert replay_plan.physical_execution_invoked is False


def test_full_artifact_path_is_serializable_and_approval_gated():
    artifacts = build_simulation_first_replay_artifacts(
        task=_task(),
        trajectory=_trajectory(),
        telemetry=_safe_telemetry(),
        now=NOW,
    )

    assert artifacts["simulation_scenario_request"]["schema_version"] == (
        SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION
    )
    assert artifacts["telemetry_health_snapshot"]["schema_version"] == (
        TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION
    )
    assert artifacts["safety_governor_decision"]["schema_version"] == (
        SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION
    )
    assert artifacts["dry_run_action_envelope"]["dry_run"] is True
    assert artifacts["offline_replay_plan"]["live_execution_allowed"] is False
    assert artifacts["physical_mission_review"]["schema_version"] == (
        PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION
    )
    assert artifacts["physical_mission_review"]["operator_approval_required"] is True
    assert artifacts["physical_mission_review"]["operator_approval_performed"] is False


def test_blocked_artifact_path_does_not_generate_action_envelope_or_replay_plan():
    artifacts = build_simulation_first_replay_artifacts(
        mission_contract=_mission_contract(),
        trajectory=_trajectory(),
        telemetry=None,
        now=NOW,
    )

    assert artifacts["safety_governor_decision"]["decision"] == "blocked"
    assert artifacts["dry_run_action_envelope"] is None
    assert artifacts["offline_replay_plan"] is None
    assert artifacts["physical_mission_review"]["final_status"] == "blocked"


def test_physical_replay_module_does_not_invoke_physical_execution_or_add_missions_api():
    source = inspect.getsource(physical_replay)

    assert "httpx" not in source
    assert "requests" not in source
    assert "subprocess" not in source
    assert "dispatch_ros2_action" not in source
    assert "/missions" not in source
    assert "CREATE TABLE missions" not in source
    assert not (Path("src/gateway/mission_routes.py").exists())
