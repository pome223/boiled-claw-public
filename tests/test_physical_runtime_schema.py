from src.physical_ai.runtime_schema import (
    PhysicalVerifierVerdictValue,
    SafetyGovernorDecisionValue,
    build_action_envelope,
    build_physical_mission_contract,
    build_physical_replay_plan,
    build_physical_verifier_result,
    build_safety_governor_decision,
)


def test_build_physical_mission_contract_keeps_abort_and_evidence_first_class():
    contract = build_physical_mission_contract(
        contract_id="mission_site_a",
        objective_type="inspection",
        objective_target="site_A",
        workflow="facility_inspection",
        scenario="site_A_day",
        robot="spot",
        task="inspect valve row",
    )

    assert contract.objective.type == "inspection"
    assert "direct_motor_control" in contract.forbidden_actions
    assert "battery_below_reserve" in contract.abort_conditions
    assert "mission_report_generated" in contract.completion_criteria
    assert "telemetry_window" in contract.evidence_requirements


def test_build_physical_verifier_result_distinguishes_unsafe_from_fail_and_uncertain():
    unsafe = build_physical_verifier_result(
        {"status": "unsafe", "telemetry_health": {"safety": "unsafe"}},
        validation_run_id="run-unsafe",
        mission_contract_id="mission-1",
    )
    uncertain = build_physical_verifier_result(
        {"status": "queued"},
        validation_run_id="run-queued",
        mission_contract_id="mission-1",
    )
    failed = build_physical_verifier_result(
        {"status": "failed"},
        validation_run_id="run-failed",
        mission_contract_id="mission-1",
    )

    assert unsafe.verdict == PhysicalVerifierVerdictValue.UNSAFE
    assert unsafe.recommended_action == "safe_mode"
    assert uncertain.verdict == PhysicalVerifierVerdictValue.UNCERTAIN
    assert failed.verdict == PhysicalVerifierVerdictValue.FAIL


def test_build_action_envelope_and_governor_decision_capture_controller_boundary():
    contract = build_physical_mission_contract(
        contract_id="mission_site_a",
        objective_type="inspection",
        objective_target="site_A",
        workflow="facility_inspection",
        scenario="site_A_day",
    )
    verifier = build_physical_verifier_result(
        {"status": "validated", "validated": True},
        validation_run_id="run-pass",
        mission_contract_id=contract.contract_id,
    )
    envelope = build_action_envelope(
        capability="navigate_to",
        target={"waypoint_id": "inspection_point_3"},
        robot_namespace="robot_1",
        frame_id="map",
        validation_run_id="run-pass",
        mission_contract_id=contract.contract_id,
    )
    decision = build_safety_governor_decision(
        mission_contract=contract,
        telemetry_health=verifier.telemetry_health,
        verifier_result=verifier,
        allow_real_hardware=True,
        dry_run=False,
    )
    replay = build_physical_replay_plan(
        replay_id="replay-1",
        source_trajectory_id=42,
        adapter="isaac_sim",
        workflow="computer_use_replay",
        scenario="browser_failure_replay",
        mission_contract=contract,
    )

    assert envelope.controller_scope == "direct_motor_thrust_attitude_control_out_of_scope"
    assert envelope.bounds["mission_contract_id"] == contract.contract_id
    assert decision.decision == SafetyGovernorDecisionValue.ALLOW
    assert replay.offline_only is True
    assert replay.live_self_modification_allowed is False
    assert "policy_patch" in replay.candidate_promotion_targets
