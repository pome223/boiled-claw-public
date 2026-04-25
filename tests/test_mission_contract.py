import pytest
from pydantic import ValidationError

from src.runtime.mission_contract import (
    MissionAbortConditionType,
    MissionContract,
    build_mission_contract,
)


def test_mission_contract_normalizes_legacy_string_abort_conditions():
    contract = build_mission_contract(
        objective="Keep the current tab healthy",
        abort_conditions=[
            "human approval required",
            "guardrail budget exhausted",
            "current tab connection unavailable",
        ],
    )

    assert contract.abort_condition_types == {
        MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED,
        MissionAbortConditionType.GUARDRAIL_BUDGET_EXHAUSTED,
        MissionAbortConditionType.CURRENT_TAB_CONNECTION_UNAVAILABLE,
    }
    payload = contract.model_dump(mode="json")
    assert [item["type"] for item in payload["abort_conditions"]] == [
        "human_approval_required",
        "guardrail_budget_exhausted",
        "current_tab_connection_unavailable",
    ]


def test_mission_contract_accepts_structured_abort_conditions():
    contract = MissionContract.model_validate(
        {
            "contract_id": "mission-typed-abort",
            "objective": "Inspect a simulated site safely",
            "abort_conditions": [
                {
                    "type": "telemetry_health_unsafe",
                    "reason": "simulated telemetry entered an unsafe state",
                    "metadata": {"source": "simulation"},
                }
            ],
        }
    )

    condition = contract.abort_conditions[0]
    assert condition.type == MissionAbortConditionType.TELEMETRY_HEALTH_UNSAFE
    assert condition.reason == "simulated telemetry entered an unsafe state"
    assert condition.metadata == {"source": "simulation"}


def test_mission_contract_rejects_unknown_abort_condition_type():
    with pytest.raises(ValidationError):
        build_mission_contract(
            objective="Keep the current tab healthy",
            abort_conditions=["unknown unsafe thing"],
        )


def test_mission_contract_accepts_explicit_task_nodes():
    contract = build_mission_contract(
        objective="Inspect a target in stages",
        task_nodes=[
            {
                "node_id": "inspect",
                "title": "Inspect target",
                "description": "Inspect visible state",
                "completion_criteria": ["visible state recorded"],
            },
            {
                "node_id": "verify",
                "title": "Verify target",
                "depends_on": ["inspect"],
            },
        ],
    )

    assert [node.node_id for node in contract.task_nodes] == ["inspect", "verify"]
    assert contract.task_nodes[1].depends_on == ["inspect"]
    assert contract.task_nodes[0].completion_criteria == ["visible state recorded"]


def test_mission_contract_rejects_unknown_task_node_dependency():
    with pytest.raises(ValidationError):
        build_mission_contract(
            objective="Inspect a target in stages",
            task_nodes=[
                {"node_id": "inspect"},
                {"node_id": "verify", "depends_on": ["missing"]},
            ],
        )


def test_mission_contract_v2_fields_are_backward_compatible():
    contract = MissionContract.model_validate({"objective": "Keep a mission healthy"})

    assert contract.schema_version == "mission_contract.v2"
    assert contract.success_metrics == []
    assert contract.memory_policy.require_operator_approval is True
    assert contract.memory_policy.candidate_ttl_seconds == 2_592_000
    assert "create_improvement_candidate" in contract.recovery_policy.ladder
    assert contract.improvement_policy.mode == "canary_only"


def test_build_mission_contract_accepts_manifest_policy_fields():
    contract = build_mission_contract(
        objective="Keep research watch current",
        success_metrics=["fresh sources found", "duplicates avoided"],
        risk_budget={"max_runtime_hours": 12, "max_repair_depth": 1},
        capability_policy={
            "allow": ["web.search", "browser.read"],
            "approval_required": ["shell.run"],
            "deny": ["browser.form_submit"],
        },
        memory_policy={
            "promote_only": ["failure_pattern", "recovery_pattern"],
            "never_promote": ["raw_transcript", "secret"],
            "candidate_ttl_seconds": 3600,
        },
        recovery_policy={"max_retries_per_step": 1, "ladder": ["verify_state"]},
        improvement_policy={
            "mode": "canary_only",
            "candidate_kinds": ["benchmark_case", "verifier_improvement"],
        },
    )

    payload = contract.model_dump(mode="json")
    assert payload["schema_version"] == "mission_contract.v2"
    assert payload["success_metrics"] == ["fresh sources found", "duplicates avoided"]
    assert payload["risk_budget"]["max_runtime_hours"] == 12
    assert payload["capability_policy"]["approval_required"] == ["shell.run"]
    assert payload["memory_policy"]["candidate_ttl_seconds"] == 3600
    assert payload["recovery_policy"]["ladder"] == ["verify_state"]
    assert payload["improvement_policy"]["candidate_kinds"] == [
        "benchmark_case",
        "verifier_improvement",
    ]
