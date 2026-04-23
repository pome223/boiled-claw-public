"""Runtime-neutral mission contract schema for live long-running work."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_DEFAULT_ABORT_CONDITIONS = [
    "human_approval_required",
    "guardrail_budget_exhausted",
    "mission_contract_violation",
]
_DEFAULT_COMPLETION_CRITERIA = [
    "objective_satisfied",
    "evidence_recorded_for_each_iteration",
]
_DEFAULT_EVIDENCE_REQUIREMENTS = [
    "child_control_loop_result",
    "verifier_verdict",
    "checkpoint",
]


def _clean_text_list(items: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        candidate = items.strip()
        return [candidate] if candidate else []
    return [str(item).strip() for item in items if str(item).strip()]


def _default_contract_id(objective: str) -> str:
    digest = sha256(objective.encode("utf-8")).hexdigest()[:12]
    return f"mission_{digest}"


class MissionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str = ""
    objective: str = Field(min_length=1)
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    abort_conditions: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("contract_id", "objective", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "allowed_actions",
        "forbidden_actions",
        "abort_conditions",
        "completion_criteria",
        "evidence_requirements",
        mode="before",
    )
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


def build_mission_contract(
    *,
    objective: str,
    constraints: list[str] | tuple[str, ...] | str | None = None,
    contract_id: str = "",
    allowed_actions: list[str] | tuple[str, ...] | str | None = None,
    forbidden_actions: list[str] | tuple[str, ...] | str | None = None,
    abort_conditions: list[str] | tuple[str, ...] | str | None = None,
    completion_criteria: list[str] | tuple[str, ...] | str | None = None,
    evidence_requirements: list[str] | tuple[str, ...] | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> MissionContract:
    normalized_objective = str(objective or "").strip()
    if not normalized_objective:
        raise ValueError("mission contract objective is required")

    legacy_constraints = _clean_text_list(constraints)
    normalized_metadata = dict(metadata or {})
    if legacy_constraints and "constraints" not in normalized_metadata:
        normalized_metadata["constraints"] = legacy_constraints

    normalized_forbidden_actions = _clean_text_list(forbidden_actions)
    if not normalized_forbidden_actions:
        normalized_forbidden_actions = legacy_constraints

    return MissionContract(
        contract_id=str(contract_id or "").strip()
        or _default_contract_id(normalized_objective),
        objective=normalized_objective,
        allowed_actions=_clean_text_list(allowed_actions),
        forbidden_actions=normalized_forbidden_actions,
        abort_conditions=_clean_text_list(abort_conditions)
        or list(_DEFAULT_ABORT_CONDITIONS),
        completion_criteria=_clean_text_list(completion_criteria)
        or list(_DEFAULT_COMPLETION_CRITERIA),
        evidence_requirements=_clean_text_list(evidence_requirements)
        or list(_DEFAULT_EVIDENCE_REQUIREMENTS),
        metadata=normalized_metadata,
    )


def normalize_mission_contract(
    mission_contract: MissionContract | dict[str, Any] | None,
    *,
    objective: str,
    constraints: list[str] | tuple[str, ...] | str | None = None,
    contract_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> MissionContract:
    if mission_contract is None:
        return build_mission_contract(
            objective=objective,
            constraints=constraints,
            contract_id=contract_id,
            metadata=metadata,
        )

    resolved = MissionContract.model_validate(mission_contract)
    updates: dict[str, Any] = {}
    if not resolved.contract_id:
        updates["contract_id"] = str(contract_id or "").strip() or _default_contract_id(
            resolved.objective
        )

    merged_metadata = dict(resolved.metadata)
    if metadata:
        merged_metadata.update(
            {key: value for key, value in metadata.items() if value not in (None, "")}
        )
    legacy_constraints = _clean_text_list(constraints)
    if legacy_constraints and "constraints" not in merged_metadata:
        merged_metadata["constraints"] = legacy_constraints
    if merged_metadata != resolved.metadata:
        updates["metadata"] = merged_metadata
    if legacy_constraints and not resolved.forbidden_actions:
        updates["forbidden_actions"] = legacy_constraints
    if not resolved.abort_conditions:
        updates["abort_conditions"] = list(_DEFAULT_ABORT_CONDITIONS)
    if not resolved.completion_criteria:
        updates["completion_criteria"] = list(_DEFAULT_COMPLETION_CRITERIA)
    if not resolved.evidence_requirements:
        updates["evidence_requirements"] = list(_DEFAULT_EVIDENCE_REQUIREMENTS)

    if updates:
        resolved = resolved.model_copy(update=updates)
    return resolved
