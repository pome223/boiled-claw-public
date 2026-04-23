from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableCheckpoint,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
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


def test_task_graph_resume_respects_scheduler_queue_and_approval_waits():
    graph = DurableTaskGraph(
        graph_id="graph-approval",
        goal="Wait for approval before resuming",
        nodes=[
            DurableTaskNode(
                node_id="node-retry",
                title="Retry later",
                status=DurableTaskNodeStatus.FAILED,
                scheduler_queue=SchedulerQueueKind.RETRY_LATER,
            ),
            DurableTaskNode(
                node_id="node-approval",
                title="Approval boundary",
                status=DurableTaskNodeStatus.BLOCKED,
                scheduler_queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
            ),
        ],
    )
    checkpoint = DurableCheckpoint(
        checkpoint_id="checkpoint-approval",
        graph_id=graph.graph_id,
        run_id="run-approval",
        current_goal=graph.goal,
        current_task_node_id="node-approval",
        open_task_node_ids=graph.open_task_node_ids(),
        blocked_task_node_ids=graph.blocked_task_node_ids(),
        pending_approval_ids=["approval:run-approval"],
        budget=CheckpointBudget(run_budget_remaining=0),
        retry_counters={"node-retry": 1},
        next_actionable_task_node_id=None,
    )

    resume = checkpoint.resume_state(graph)

    assert graph.next_actionable_task_node_id() is None
    assert graph.blocked_task_node_ids() == ["node-retry", "node-approval"]
    assert resume.pending_approval_ids == ["approval:run-approval"]
    assert resume.reason == "awaiting_approval"


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


def test_scheduler_queue_state_counts_by_queue():
    queue_state = SchedulerQueueState(
        ready_queue=[
            SchedulerQueueEntry(entry_id="q1", node_id="n1", queue=SchedulerQueueKind.READY)
        ],
        waiting_for_approval_queue=[
            SchedulerQueueEntry(
                entry_id="q2",
                node_id="n2",
                queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
            )
        ],
        retry_later_queue=[
            SchedulerQueueEntry(
                entry_id="q3",
                node_id="n3",
                queue=SchedulerQueueKind.RETRY_LATER,
            )
        ],
    )

    assert queue_state.counts() == {
        "ready": 1,
        "blocked": 0,
        "waiting_for_approval": 1,
        "retry_later": 1,
        "periodic_check": 0,
        "completed": 0,
    }
