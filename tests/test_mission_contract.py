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
