import asyncio

import pytest

from src.control_loop.root_workflow import ExecutionResult
from src.gateway.control_supervisor import ControlLoopSupervisor
from src.runtime.mission_templates import (
    MissionTemplateError,
    build_mission_contract_from_template,
    get_mission_template,
    list_mission_templates,
)
from src.runtime.task_store import TaskStore

TEMPLATE_INPUTS = {
    "observation_review": {"target": "the visible dashboard"},
    "weak_evidence_probe": {"target": "the current tab"},
    "budget_exhaustion_probe": {"target": "the current tab"},
    "current_tab_research_to_report": {
        "topic": "mission runtime UX",
        "report_target": "reports/mission-runtime.md",
    },
    "repo_maintenance_review": {
        "repo_path": "/work/repo",
        "focus": "mission runtime tests",
    },
}


def test_list_mission_templates_includes_initial_presets():
    template_ids = {item["id"] for item in list_mission_templates()}

    assert template_ids == {
        "observation_review",
        "weak_evidence_probe",
        "budget_exhaustion_probe",
        "current_tab_research_to_report",
        "repo_maintenance_review",
    }


@pytest.mark.parametrize("template_id", sorted(TEMPLATE_INPUTS))
def test_each_mission_template_builds_valid_contract(template_id):
    contract = build_mission_contract_from_template(
        template_id,
        TEMPLATE_INPUTS[template_id],
    )

    assert contract.schema_version == "mission_contract.v2"
    assert contract.contract_id == f"mission-template:{template_id}"
    assert contract.metadata["template_id"] == template_id
    assert contract.metadata["template_inputs"] == TEMPLATE_INPUTS[template_id]
    assert contract.success_metrics
    assert contract.forbidden_actions
    assert contract.completion_criteria
    assert contract.evidence_requirements
    assert contract.recovery_policy.ladder
    assert contract.memory_policy.require_operator_approval is True
    assert contract.improvement_policy.mode == "canary_only"


def test_missing_required_inputs_fail_clearly():
    with pytest.raises(MissionTemplateError, match="target"):
        build_mission_contract_from_template("observation_review", {})


def test_unknown_template_fails_clearly():
    with pytest.raises(MissionTemplateError, match="unknown mission template"):
        get_mission_template("missing")


def test_overrides_are_applied_without_removing_forbidden_actions():
    contract = build_mission_contract_from_template(
        "observation_review",
        {"target": "the dashboard"},
        overrides={
            "contract_id": "mission-custom-observation",
            "allowed_actions": ["current_tab.info"],
            "forbidden_actions": ["download files"],
            "metadata": {"operator_note": "narrowed current-tab only"},
        },
    )

    assert contract.contract_id == "mission-custom-observation"
    assert contract.allowed_actions == ["current_tab.info"]
    assert "navigate away" in contract.forbidden_actions
    assert "download files" in contract.forbidden_actions
    assert contract.metadata["template_id"] == "observation_review"
    assert contract.metadata["operator_note"] == "narrowed current-tab only"


def test_allowed_action_overrides_cannot_expand_template_surface():
    with pytest.raises(MissionTemplateError, match="unsupported additions: shell.run"):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"allowed_actions": ["current_tab.info", "shell.run"]},
        )


def test_risk_budget_overrides_cannot_increase_template_budget():
    with pytest.raises(MissionTemplateError, match="risk_budget.max_repair_depth"):
        build_mission_contract_from_template(
            "budget_exhaustion_probe",
            {"target": "the current tab"},
            overrides={"risk_budget": {"max_repair_depth": 1}},
        )


def test_risk_budget_string_override_cannot_bypass_safety():
    with pytest.raises(MissionTemplateError, match="risk_budget.max_repair_depth"):
        build_mission_contract_from_template(
            "budget_exhaustion_probe",
            {"target": "the current tab"},
            overrides={"risk_budget": {"max_repair_depth": "999"}},
        )


def test_recovery_policy_overrides_cannot_increase_retry_budget():
    with pytest.raises(
        MissionTemplateError,
        match="recovery_policy.max_retries_per_step",
    ):
        build_mission_contract_from_template(
            "weak_evidence_probe",
            {"target": "the current tab"},
            overrides={"recovery_policy": {"max_retries_per_step": 1}},
        )


def test_recovery_policy_string_retry_override_cannot_bypass_safety():
    with pytest.raises(
        MissionTemplateError,
        match="recovery_policy.max_retries_per_step",
    ):
        build_mission_contract_from_template(
            "weak_evidence_probe",
            {"target": "the current tab"},
            overrides={"recovery_policy": {"max_retries_per_step": "999"}},
        )


def test_recovery_ladder_overrides_cannot_add_steps():
    with pytest.raises(MissionTemplateError, match="recovery_policy.ladder"):
        build_mission_contract_from_template(
            "weak_evidence_probe",
            {"target": "the current tab"},
            overrides={"recovery_policy": {"ladder": ["alternate_capability"]}},
        )


def test_memory_policy_overrides_cannot_disable_approval_or_expand_types():
    with pytest.raises(
        MissionTemplateError,
        match="memory_policy.require_operator_approval",
    ):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"memory_policy": {"require_operator_approval": False}},
        )

    with pytest.raises(
        MissionTemplateError,
        match="memory_policy.require_operator_approval",
    ):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"memory_policy": {"require_operator_approval": "false"}},
        )

    with pytest.raises(MissionTemplateError, match="memory_policy.promote_only"):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"memory_policy": {"promote_only": ["raw_transcript"]}},
        )


def test_improvement_policy_overrides_cannot_disable_gates_or_expand_kinds():
    with pytest.raises(
        MissionTemplateError,
        match="improvement_policy.require_benchmark_pass",
    ):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"improvement_policy": {"require_benchmark_pass": False}},
        )

    with pytest.raises(
        MissionTemplateError,
        match="improvement_policy.require_benchmark_pass",
    ):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"improvement_policy": {"require_benchmark_pass": "false"}},
        )

    with pytest.raises(
        MissionTemplateError, match="improvement_policy.candidate_kinds"
    ):
        build_mission_contract_from_template(
            "observation_review",
            {"target": "the dashboard"},
            overrides={"improvement_policy": {"candidate_kinds": ["code_patch"]}},
        )


def test_weak_evidence_probe_uses_approval_oriented_recovery():
    contract = build_mission_contract_from_template(
        "weak_evidence_probe",
        {"target": "the current tab"},
    )

    assert contract.recovery_policy.max_retries_per_step == 0
    assert contract.recovery_policy.ladder == [
        "verify_state",
        "request_approval",
        "pause_or_block",
    ]
    assert contract.risk_budget is not None
    assert contract.risk_budget.max_pending_approvals == 1


def test_budget_exhaustion_probe_uses_zero_retry_budget():
    contract = build_mission_contract_from_template(
        "budget_exhaustion_probe",
        {"target": "the current tab"},
    )

    assert contract.recovery_policy.max_retries_per_step == 0
    assert contract.recovery_policy.ladder == ["retry_same_step", "pause_or_block"]
    assert contract.risk_budget is not None
    assert contract.risk_budget.max_same_failure_retries == 0
    assert contract.risk_budget.max_repair_depth == 0
    assert contract.risk_budget.max_pending_approvals == 0


@pytest.mark.asyncio
async def test_generated_contract_can_start_control_supervisor(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    contract = build_mission_contract_from_template(
        "observation_review",
        {"target": "the dashboard"},
    )

    def _create_task_record(**kwargs):
        return store.create(**kwargs)

    def _update_task_record(task_id, **kwargs):
        return store.update(task_id, **kwargs)

    def _append_task_event_record(task_id, **kwargs):
        return store.append_event(task_id, **kwargs)

    async def _emit_session_event(*args, **kwargs):
        return None

    async def _run_control_loop_with_task(**kwargs):
        child_task = store.create(
            kind="control_loop",
            title="template iteration",
            status="completed",
            owner_session_id=kwargs["owner_session_id"],
            owner_user_id=kwargs["user_id"],
            parent_task_id=kwargs["parent_task_id"],
            artifacts={"result": {"success": True, "final_text": "ok"}},
        )
        return (
            ExecutionResult(
                request_id="req-template",
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                final_text="ok",
                success=True,
            ),
            child_task["task_id"],
        )

    supervisor = ControlLoopSupervisor(
        run_control_loop_with_task=_run_control_loop_with_task,
        emit_session_event=_emit_session_event,
        create_task_record_fn=_create_task_record,
        update_task_record_fn=_update_task_record,
        append_task_event_record_fn=_append_task_event_record,
    )

    started = await supervisor.start(
        user_id="alice",
        owner_session_id="sess-template",
        objective=contract.objective,
        constraints=[],
        duration_seconds=60,
        interval_seconds=0,
        source="test",
        max_iterations=1,
        mission_contract=contract,
    )
    await asyncio.gather(*(handle.task for handle in supervisor._handles.values()))

    parent = store.get(started.task["task_id"])
    assert parent is not None
    assert parent["status"] == "completed"
    persisted_contract = parent["artifacts"]["mission_contract"]
    assert persisted_contract["metadata"]["template_id"] == "observation_review"
