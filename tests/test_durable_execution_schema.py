from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableCheckpoint,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
)


def test_task_graph_resume_prefers_ready_then_failed_nodes():
    graph = DurableTaskGraph(
        graph_id="graph-1",
        goal="Resume a bounded task graph",
        nodes=[
            DurableTaskNode(
                node_id="node-done",
                title="Finished node",
                status=DurableTaskNodeStatus.DONE,
            ),
            DurableTaskNode(
                node_id="node-blocked",
                title="Blocked node",
                status=DurableTaskNodeStatus.BLOCKED,
            ),
            DurableTaskNode(
                node_id="node-failed",
                title="Retry me",
                status=DurableTaskNodeStatus.FAILED,
            ),
        ],
    )
    checkpoint = DurableCheckpoint(
        checkpoint_id="checkpoint-1",
        graph_id=graph.graph_id,
        run_id="run-1",
        current_goal=graph.goal,
        current_task_node_id="node-failed",
        open_task_node_ids=graph.open_task_node_ids(),
        blocked_task_node_ids=graph.blocked_task_node_ids(),
        pending_approval_ids=[],
        budget=CheckpointBudget(run_budget_remaining=0),
        retry_counters={"node-failed": 1},
        next_actionable_task_node_id=None,
    )

    resume = checkpoint.resume_state(graph)

    assert graph.open_task_node_ids() == ["node-blocked", "node-failed"]
    assert graph.blocked_task_node_ids() == ["node-blocked"]
    assert resume.next_actionable_task_node_id == "node-failed"
    assert resume.reason == "resume_from_open_task"


def test_durable_verifier_verdict_supports_uncertain():
    verdict = DurableVerifierVerdict(
        verdict=DurableVerifierVerdictValue.UNCERTAIN,
        evidence_refs=["screenshot.png"],
        failure_type="weak_evidence",
        confidence=0.4,
        confidence_source="reported",
        verifier_source="trajectory_eval_phase0",
        recommended_repair_target="capture stronger post-action evidence",
        trajectory_id=12,
        replay_reference={"trajectory_id": 12},
    )

    assert verdict.verdict == DurableVerifierVerdictValue.UNCERTAIN
    assert verdict.failure_type == "weak_evidence"
    assert verdict.confidence_source == "reported"
    assert verdict.replay_reference["trajectory_id"] == 12
