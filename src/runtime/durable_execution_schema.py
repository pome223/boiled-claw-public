from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DurableTaskNodeStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


class DurableJobRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class DurableVerifierVerdictValue(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    UNSAFE = "unsafe"


class DurableArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    ref: str
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DurableVerifierVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: DurableVerifierVerdictValue
    evidence_refs: list[str] = Field(default_factory=list)
    failure_type: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_source: str = ""
    verifier_source: str = ""
    recommended_repair_target: str | None = None
    trajectory_id: int | None = None
    replay_reference: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


class DurableTaskNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    title: str
    description: str = ""
    status: DurableTaskNodeStatus = DurableTaskNodeStatus.READY
    depends_on: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    artifacts: list[DurableArtifactRef] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    trajectory_ids: list[int] = Field(default_factory=list)
    replay_references: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_refs: list[str] = Field(default_factory=list)
    verifier_verdict: DurableVerifierVerdict | None = None

    def is_open(self) -> bool:
        return self.status in {
            DurableTaskNodeStatus.READY,
            DurableTaskNodeStatus.RUNNING,
            DurableTaskNodeStatus.FAILED,
            DurableTaskNodeStatus.BLOCKED,
        }


class DurableTaskGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: str
    goal: str
    nodes: list[DurableTaskNode] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def open_task_node_ids(self) -> list[str]:
        return [node.node_id for node in self.nodes if node.is_open()]

    def blocked_task_node_ids(self) -> list[str]:
        return [
            node.node_id
            for node in self.nodes
            if node.status == DurableTaskNodeStatus.BLOCKED
        ]

    def next_actionable_task_node_id(self) -> str | None:
        for preferred_status in (
            DurableTaskNodeStatus.READY,
            DurableTaskNodeStatus.FAILED,
            DurableTaskNodeStatus.RUNNING,
        ):
            for node in self.nodes:
                if node.status == preferred_status:
                    return node.node_id
        return None


class DurableJobRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    graph_id: str
    node_id: str
    goal: str
    status: DurableJobRunStatus
    attempt: int = Field(default=1, ge=1)
    trajectory_id: int | None = None
    replay_reference: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None
    verifier_verdict: DurableVerifierVerdict | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    ended_at: datetime | None = None


class CheckpointBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_budget_remaining: int | None = Field(default=None, ge=0)
    retry_budget_remaining: dict[str, int] = Field(default_factory=dict)


class DurableResumeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    graph_id: str
    next_actionable_task_node_id: str | None = None
    open_task_node_ids: list[str] = Field(default_factory=list)
    blocked_task_node_ids: list[str] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class DurableCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    graph_id: str
    run_id: str
    current_goal: str
    current_task_node_id: str | None = None
    open_task_node_ids: list[str] = Field(default_factory=list)
    blocked_task_node_ids: list[str] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    last_successful_artifacts: dict[str, list[DurableArtifactRef]] = Field(default_factory=dict)
    budget: CheckpointBudget = Field(default_factory=CheckpointBudget)
    retry_counters: dict[str, int] = Field(default_factory=dict)
    trajectory_ids: list[int] = Field(default_factory=list)
    replay_references: list[dict[str, Any]] = Field(default_factory=list)
    next_actionable_task_node_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)

    def resume_state(self, task_graph: DurableTaskGraph) -> DurableResumeState:
        next_actionable = self.next_actionable_task_node_id
        if not next_actionable:
            next_actionable = task_graph.next_actionable_task_node_id()

        reason = ""
        if next_actionable:
            reason = "resume_from_open_task"
        elif self.blocked_task_node_ids:
            reason = "awaiting_unblock_or_human_input"
        elif self.pending_approval_ids:
            reason = "awaiting_approval"
        else:
            reason = "graph_complete"

        return DurableResumeState(
            checkpoint_id=self.checkpoint_id,
            graph_id=self.graph_id,
            next_actionable_task_node_id=next_actionable,
            open_task_node_ids=list(self.open_task_node_ids),
            blocked_task_node_ids=list(self.blocked_task_node_ids),
            pending_approval_ids=list(self.pending_approval_ids),
            reason=reason,
        )
