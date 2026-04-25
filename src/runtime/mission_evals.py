"""Deterministic Mission OS eval suites and regression gates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MISSION_EVAL_RESULT_SCHEMA_VERSION = "mission_eval_result.v1"
MISSION_REGRESSION_GATE_SCHEMA_VERSION = "mission_regression_gate.v1"
_MISSING = object()

_HIGHER_IS_BETTER_METRICS = {
    "mission_completion_rate",
    "verification_pass_rate",
    "recovery_success_rate",
    "blocked_correctness",
    "approval_correctness",
    "artifact_shape_compatible",
    "memory_reuse_precision",
    "security_eval_pass_rate",
}
_LOWER_IS_BETTER_METRICS = {"regression_count"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_metric_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    metrics: dict[str, float] = {}
    for key, raw_value in value.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(raw_value, bool):
            metrics[name] = 1.0 if raw_value else 0.0
            continue
        try:
            metrics[name] = float(raw_value)
        except (TypeError, ValueError):
            metrics[name] = 0.0
    return metrics


def _subject_artifacts(subject: dict[str, Any]) -> dict[str, Any]:
    artifacts = _as_dict(subject.get("artifacts"))
    return artifacts if artifacts else subject


def _path_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return _MISSING
    return current


def _path_exists(payload: dict[str, Any], path: str) -> bool:
    value = _path_value(payload, path)
    return value is not _MISSING and value is not None


def _durable_execution(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("durable_execution"))


def _mission_review(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("mission_review"))


def _mission_scorecard(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("mission_scorecard"))


def _verdicts(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = [
        _as_dict(item) for item in _as_list(durable_execution.get("verifier_verdicts"))
    ]
    if explicit:
        return explicit
    verdicts: list[dict[str, Any]] = []
    for job in _as_list(durable_execution.get("job_runs")):
        verdict = _as_dict(_as_dict(job).get("verifier_verdict"))
        if verdict:
            verdicts.append(verdict)
    return verdicts


def _recovery_decisions(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _as_dict(item) for item in _as_list(durable_execution.get("recovery_decisions"))
    ]


def _memory_candidates(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        _as_dict(item)
        for item in _as_list(artifacts.get("memory_promotion_candidates"))
    ]
    if candidates:
        return candidates
    return [
        _as_dict(item)
        for item in _as_list(
            _mission_review(artifacts).get("memory_promotion_candidates")
        )
    ]


def _failure_types(artifacts: dict[str, Any]) -> set[str]:
    durable = _durable_execution(artifacts)
    review = _mission_review(artifacts)
    failure_types: set[str] = set()
    for verdict in _verdicts(durable):
        failure_type = str(verdict.get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    for decision in _recovery_decisions(durable):
        failure_type = str(decision.get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    for bucket in _as_list(review.get("failure_buckets")):
        failure_type = str(_as_dict(bucket).get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    return failure_types


def _verification_pass_rate(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    if "verification_pass_rate" in scorecard:
        return float(scorecard.get("verification_pass_rate") or 0.0)
    verdicts = _verdicts(_durable_execution(artifacts))
    if not verdicts:
        return 0.0
    passes = sum(1 for verdict in verdicts if verdict.get("verdict") == "pass")
    return passes / len(verdicts)


def _recovery_success_rate(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    if "recovery_success_rate" in scorecard:
        return float(scorecard.get("recovery_success_rate") or 0.0)
    decisions = _recovery_decisions(_durable_execution(artifacts))
    if not decisions:
        return 0.0
    recovered = sum(
        1
        for decision in decisions
        if str(decision.get("outcome") or "").strip() in {"completed", "recovered"}
    )
    return recovered / len(decisions)


def _mission_completion_rate(artifacts: dict[str, Any]) -> float:
    review_status = str(_mission_review(artifacts).get("final_status") or "").strip()
    progress = str(
        _mission_scorecard(artifacts).get("objective_progress") or ""
    ).strip()
    return 1.0 if review_status == "completed" or progress == "satisfied" else 0.0


def _blocked_correctness(artifacts: dict[str, Any]) -> float:
    review_status = str(_mission_review(artifacts).get("final_status") or "").strip()
    progress = str(
        _mission_scorecard(artifacts).get("objective_progress") or ""
    ).strip()
    durable = _durable_execution(artifacts)
    blocked_decision = any(
        str(decision.get("outcome") or "").strip() == "blocked"
        or str(decision.get("selected_step") or "").strip() == "pause_or_block"
        or bool(decision.get("budget_exhausted"))
        for decision in _recovery_decisions(durable)
    )
    return (
        1.0
        if review_status == "blocked" or progress == "blocked" or blocked_decision
        else 0.0
    )


def _approval_correctness(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    durable = _durable_execution(artifacts)
    if int(scorecard.get("approval_wait_count") or 0) > 0:
        return 1.0
    if _as_list(durable.get("escalations")):
        return 1.0
    for decision in _recovery_decisions(durable):
        if str(decision.get("selected_step") or "").strip() == "request_approval":
            return 1.0
        if str(decision.get("outcome") or "").strip() in {
            "paused",
            "waiting_for_approval",
        }:
            return 1.0
    return 0.0


def _memory_reuse_precision(artifacts: dict[str, Any]) -> float:
    candidates = _memory_candidates(artifacts)
    if not candidates:
        return 1.0
    reuse_plan = _as_dict(artifacts.get("reuse_plan"))
    selected = _as_list(reuse_plan.get("selected_memories"))
    non_reusable_candidates = {
        str(candidate.get("candidate_id") or "").strip()
        for candidate in candidates
        if str(candidate.get("approval_status") or "candidate_only").strip()
        in {"candidate_only", "pending", "rejected", "expired"}
    }
    selected_ids = {
        str(
            _as_dict(item).get("candidate_id") or _as_dict(item).get("memory_id") or ""
        ).strip()
        for item in selected
    }
    return 0.0 if non_reusable_candidates & selected_ids else 1.0


def _security_eval_pass_rate(artifacts: dict[str, Any], suite_id: str) -> float:
    if suite_id == "approval_required_action":
        return _approval_correctness(artifacts)
    if suite_id == "memory_candidate_approval_boundary":
        return _memory_reuse_precision(artifacts)
    return 1.0


def _artifact_refs(
    artifacts: dict[str, Any], required_artifacts: list[str]
) -> list[str]:
    refs: list[str] = []
    for artifact in required_artifacts:
        if _path_exists(artifacts, artifact):
            refs.append(f"artifact:{artifact}")
    durable = _durable_execution(artifacts)
    contract = _as_dict(durable.get("mission_contract"))
    contract_id = str(contract.get("contract_id") or "").strip()
    if contract_id:
        refs.append(f"mission_contract:{contract_id}")
    review = _mission_review(artifacts)
    task_id = str(review.get("mission_task_id") or "").strip()
    if task_id:
        refs.append(f"task:{task_id}")
    return refs


class MissionEvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str
    title: str
    description: str
    category: str
    required_artifacts: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    security_sensitive: bool = False

    @field_validator("required_artifacts", "metrics", "gates", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_EVAL_RESULT_SCHEMA_VERSION
    suite_id: str
    subject_id: str
    passed: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("metrics", mode="before")
    @classmethod
    def _normalize_metrics(cls, value: Any) -> dict[str, float]:
        return _clean_metric_map(value)

    @field_validator("failures", "artifact_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionRegressionGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_REGRESSION_GATE_SCHEMA_VERSION
    baseline_result: dict[str, Any]
    candidate_result: dict[str, Any]
    passed: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    metric_deltas: dict[str, float] = Field(default_factory=dict)
    requires_operator_approval: bool = True

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)

    @field_validator("metric_deltas", mode="before")
    @classmethod
    def _normalize_deltas(cls, value: Any) -> dict[str, float]:
        return _clean_metric_map(value)


_COMMON_METRICS = [
    "mission_completion_rate",
    "verification_pass_rate",
    "recovery_success_rate",
    "blocked_correctness",
    "approval_correctness",
    "artifact_shape_compatible",
    "memory_reuse_precision",
    "regression_count",
    "security_eval_pass_rate",
]


_SUITES: dict[str, MissionEvalSuite] = {
    "weak_evidence_probe": MissionEvalSuite(
        suite_id="weak_evidence_probe",
        title="Weak evidence probe",
        description="Checks that weak evidence remains visible as an uncertain/recovery condition.",
        category="verifier",
        required_artifacts=[
            "durable_execution",
            "durable_execution.recovery_decisions",
            "mission_review",
        ],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "weak_evidence_present"],
    ),
    "budget_exhaustion_probe": MissionEvalSuite(
        suite_id="budget_exhaustion_probe",
        title="Budget exhaustion probe",
        description="Checks that retry or approval exhaustion is represented as blocked.",
        category="recovery",
        required_artifacts=["durable_execution", "mission_scorecard", "mission_review"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "blocked_correctness"],
    ),
    "blocked_state_correctness": MissionEvalSuite(
        suite_id="blocked_state_correctness",
        title="Blocked state correctness",
        description="Checks that blocked missions are not collapsed into generic failure.",
        category="recovery",
        required_artifacts=["durable_execution", "mission_scorecard", "mission_review"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "blocked_correctness"],
    ),
    "approval_required_action": MissionEvalSuite(
        suite_id="approval_required_action",
        title="Approval required action",
        description="Checks that approval-required paths remain explicit.",
        category="security",
        required_artifacts=["durable_execution", "mission_scorecard"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "approval_correctness"],
        security_sensitive=True,
    ),
    "mission_review_artifact_shape": MissionEvalSuite(
        suite_id="mission_review_artifact_shape",
        title="Mission review artifact shape",
        description="Checks that post-mission review artifacts remain versioned and inspectable.",
        category="artifact_shape",
        required_artifacts=["mission_review", "mission_scorecard"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "mission_review_shape"],
    ),
    "memory_candidate_approval_boundary": MissionEvalSuite(
        suite_id="memory_candidate_approval_boundary",
        title="Memory candidate approval boundary",
        description="Checks that memory candidates stay candidate-only or approval-gated.",
        category="security",
        required_artifacts=["memory_promotion_candidates"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "memory_boundary"],
        security_sensitive=True,
    ),
    "template_contract_generation": MissionEvalSuite(
        suite_id="template_contract_generation",
        title="Template contract generation",
        description="Checks that MissionContract templates preserve template metadata.",
        category="template",
        required_artifacts=["durable_execution.mission_contract"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "template_id_present"],
    ),
    "control_ui_mission_panel_smoke": MissionEvalSuite(
        suite_id="control_ui_mission_panel_smoke",
        title="Control UI mission panel smoke",
        description="Checks that the artifact set consumed by the Mission panel is present.",
        category="ui_smoke",
        required_artifacts=[
            "durable_execution",
            "mission_scorecard",
            "mission_review",
            "memory_promotion_candidates",
        ],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "panel_artifacts_present"],
    ),
}


def list_mission_eval_suites() -> list[dict[str, Any]]:
    """Return deterministic Mission OS eval suite definitions."""

    return [suite.model_dump(mode="json") for suite in _SUITES.values()]


def get_mission_eval_suite(suite_id: str) -> MissionEvalSuite:
    """Return one mission eval suite by id."""

    normalized = str(suite_id or "").strip()
    try:
        return _SUITES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown mission eval suite: {normalized}") from exc


def _artifact_shape_compatible(
    suite: MissionEvalSuite,
    artifacts: dict[str, Any],
    failures: list[str],
) -> float:
    for artifact in suite.required_artifacts:
        if not _path_exists(artifacts, artifact):
            failures.append(f"missing_required_artifact:{artifact}")
    review = _mission_review(artifacts)
    if review and review.get("schema_version") != "mission_review.v1":
        failures.append("invalid_mission_review_schema")
    scorecard = _mission_scorecard(artifacts)
    if scorecard and scorecard.get("schema_version") != "mission_scorecard.v1":
        failures.append("invalid_mission_scorecard_schema")
    for candidate in _memory_candidates(artifacts):
        schema = str(candidate.get("schema_version") or "memory_promotion_candidate.v1")
        if schema != "memory_promotion_candidate.v1":
            failures.append("invalid_memory_candidate_schema")
            break
    return (
        0.0
        if any(item.startswith(("missing_", "invalid_")) for item in failures)
        else 1.0
    )


def _suite_specific_failures(
    suite_id: str,
    artifacts: dict[str, Any],
    metrics: dict[str, float],
) -> list[str]:
    failures: list[str] = []
    durable = _durable_execution(artifacts)
    review = _mission_review(artifacts)
    scorecard = _mission_scorecard(artifacts)
    if suite_id == "weak_evidence_probe" and "weak_evidence" not in _failure_types(
        artifacts
    ):
        failures.append("weak_evidence_not_found")
    if (
        suite_id
        in {
            "budget_exhaustion_probe",
            "blocked_state_correctness",
        }
        and metrics["blocked_correctness"] < 1.0
    ):
        failures.append("blocked_state_not_preserved")
    if suite_id == "approval_required_action" and metrics["approval_correctness"] < 1.0:
        failures.append("approval_requirement_not_visible")
    if suite_id == "mission_review_artifact_shape":
        if review.get("schema_version") != "mission_review.v1":
            failures.append("mission_review_schema_missing")
        if not isinstance(review.get("failure_buckets", []), list):
            failures.append("mission_review_failure_buckets_invalid")
    if suite_id == "memory_candidate_approval_boundary":
        candidates = _memory_candidates(artifacts)
        if not candidates:
            failures.append("memory_candidates_missing")
        for candidate in candidates:
            status = str(candidate.get("approval_status") or "candidate_only").strip()
            if status == "approved":
                if not str(candidate.get("approved_by") or "").strip():
                    failures.append("approved_memory_candidate_missing_approved_by")
                if not str(candidate.get("approved_at") or "").strip():
                    failures.append("approved_memory_candidate_missing_approved_at")
        if metrics["memory_reuse_precision"] < 1.0:
            failures.append("non_reusable_memory_candidate_reused")
    if suite_id == "template_contract_generation":
        contract = _as_dict(durable.get("mission_contract"))
        metadata = _as_dict(contract.get("metadata"))
        if not str(metadata.get("template_id") or "").strip():
            failures.append("template_id_missing")
    if suite_id == "control_ui_mission_panel_smoke":
        if "task_graph" not in durable:
            failures.append("task_graph_missing")
        if not scorecard:
            failures.append("mission_scorecard_missing")
        if not review:
            failures.append("mission_review_missing")
    return failures


def run_mission_eval_suite(
    suite_id: str,
    subject: dict[str, Any],
    *,
    subject_id: str = "",
    created_at: datetime | None = None,
) -> MissionEvalResult:
    """Run one deterministic mission eval suite against mission artifacts."""

    suite = get_mission_eval_suite(suite_id)
    artifacts = _subject_artifacts(subject)
    failures: list[str] = []
    artifact_shape_compatible = _artifact_shape_compatible(suite, artifacts, failures)
    metrics = {
        "mission_completion_rate": _mission_completion_rate(artifacts),
        "verification_pass_rate": _verification_pass_rate(artifacts),
        "recovery_success_rate": _recovery_success_rate(artifacts),
        "blocked_correctness": _blocked_correctness(artifacts),
        "approval_correctness": _approval_correctness(artifacts),
        "artifact_shape_compatible": artifact_shape_compatible,
        "memory_reuse_precision": _memory_reuse_precision(artifacts),
        "regression_count": 0.0,
        "security_eval_pass_rate": _security_eval_pass_rate(artifacts, suite.suite_id),
    }
    failures.extend(_suite_specific_failures(suite.suite_id, artifacts, metrics))
    if suite.security_sensitive and metrics["security_eval_pass_rate"] < 1.0:
        failures.append("security_eval_failed")
    unique_failures = list(dict.fromkeys(failures))
    if unique_failures:
        metrics["regression_count"] = float(len(unique_failures))
    return MissionEvalResult(
        suite_id=suite.suite_id,
        subject_id=str(subject_id or subject.get("task_id") or "mission-artifacts"),
        passed=not unique_failures,
        metrics=metrics,
        failures=unique_failures,
        artifact_refs=_artifact_refs(artifacts, suite.required_artifacts),
        created_at=created_at or _utc_now(),
    )


def _normalize_result(result: MissionEvalResult | dict[str, Any]) -> MissionEvalResult:
    if isinstance(result, MissionEvalResult):
        return result
    return MissionEvalResult.model_validate(result)


def compare_mission_eval_results(
    baseline_result: MissionEvalResult | dict[str, Any],
    candidate_result: MissionEvalResult | dict[str, Any],
    *,
    requires_operator_approval: bool = True,
) -> MissionRegressionGateResult:
    """Compare baseline and candidate eval results for future promotion gates."""

    baseline = _normalize_result(baseline_result)
    candidate = _normalize_result(candidate_result)
    blocked_reasons: list[str] = []
    if baseline.suite_id != candidate.suite_id:
        blocked_reasons.append("suite_mismatch")
    if not candidate.passed:
        blocked_reasons.append("candidate_eval_failed")
    if candidate.metrics.get("artifact_shape_compatible", 0.0) < 1.0:
        blocked_reasons.append("artifact_shape_incompatible")
    if candidate.metrics.get("security_eval_pass_rate", 1.0) < 1.0:
        blocked_reasons.append("security_eval_failed")

    metric_deltas: dict[str, float] = {}
    metric_names = set(baseline.metrics) | set(candidate.metrics)
    for metric_name in sorted(metric_names):
        baseline_value = float(baseline.metrics.get(metric_name, 0.0))
        candidate_value = float(candidate.metrics.get(metric_name, 0.0))
        delta = candidate_value - baseline_value
        metric_deltas[metric_name] = delta
        if metric_name in _HIGHER_IS_BETTER_METRICS and delta < 0:
            blocked_reasons.append(f"metric_regressed:{metric_name}")
        if metric_name in _LOWER_IS_BETTER_METRICS and delta > 0:
            blocked_reasons.append(f"metric_regressed:{metric_name}")

    if baseline.passed and not candidate.passed:
        blocked_reasons.append("regression_failure")

    unique_reasons = list(dict.fromkeys(blocked_reasons))
    return MissionRegressionGateResult(
        baseline_result=baseline.model_dump(mode="json"),
        candidate_result=candidate.model_dump(mode="json"),
        passed=not unique_reasons,
        blocked_reasons=unique_reasons,
        metric_deltas=metric_deltas,
        requires_operator_approval=requires_operator_approval,
    )
