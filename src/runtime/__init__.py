"""Runtime schemas and state keys for boiled-claw v2 control loop."""

from src.runtime.plan_schema import (
    RiskLevel,
    ApprovalStatus,
    CapabilityMode,
    CapabilityRequirement,
    SuccessCriterionType,
    SuccessCriterion,
    PlanStepStatus,
    PlanStep,
    Plan,
)
from src.runtime.verification_schema import (
    VerificationStatus,
    FailureType,
    CriterionResult,
    RepairAction,
    VerificationReport,
)
from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableArtifactRef,
    DurableCheckpoint,
    DurableEscalationRecord,
    DurableJobRun,
    DurableJobRunStatus,
    DurableResumeState,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    EscalationStatus,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryBudgetImpact,
    RecoveryDecision,
    RecoveryPolicy,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)
from src.runtime.mission_contract import (
    MissionAbortCondition,
    MissionAbortConditionType,
    MissionContract,
    build_mission_contract,
    normalize_mission_contract,
)
from src.runtime.policy_schema import (
    ApprovalMode,
    ApprovalDecisionStatus,
    CapabilityGrant,
    RiskAssessment,
    ApprovalDecision,
)
from src.runtime.trace_schema import (
    TraceEventType,
    TraceEvent,
)
from src.runtime.state_keys import StateKeys

__all__ = [
    # plan
    "RiskLevel",
    "ApprovalStatus",
    "CapabilityMode",
    "CapabilityRequirement",
    "SuccessCriterionType",
    "SuccessCriterion",
    "PlanStepStatus",
    "PlanStep",
    "Plan",
    # verification
    "VerificationStatus",
    "FailureType",
    "CriterionResult",
    "RepairAction",
    "VerificationReport",
    # durable execution
    "DurableArtifactRef",
    "DurableCheckpoint",
    "CheckpointBudget",
    "DurableEscalationRecord",
    "DurableJobRun",
    "DurableJobRunStatus",
    "DurableResumeState",
    "DurableTaskGraph",
    "DurableTaskNode",
    "DurableTaskNodeStatus",
    "DurableVerifierVerdict",
    "DurableVerifierVerdictValue",
    "EscalationStatus",
    "GuardrailBudgetPolicy",
    "RecoveryActionType",
    "RecoveryBudgetImpact",
    "RecoveryDecision",
    "RecoveryPolicy",
    "SchedulerQueueEntry",
    "SchedulerQueueKind",
    "SchedulerQueueState",
    "MissionAbortCondition",
    "MissionAbortConditionType",
    "MissionContract",
    "build_mission_contract",
    "normalize_mission_contract",
    # policy
    "ApprovalMode",
    "ApprovalDecisionStatus",
    "CapabilityGrant",
    "RiskAssessment",
    "ApprovalDecision",
    # trace
    "TraceEventType",
    "TraceEvent",
    # state
    "StateKeys",
]
