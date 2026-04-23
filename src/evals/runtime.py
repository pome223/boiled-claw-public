"""Phase 0 trajectory-native eval runner."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.evals.failure_taxonomy import PHASE0_FAILURE_BUCKETS, normalize_trajectory_failure
from src.runtime.task_store import get_task_store
from src.tools.memory import get_memory_store
from src.tools.self_improvement_runtime.promotion import REUSE_MEMORY_KINDS
from src.tools.self_improvement_runtime.reuse import prefilter_reuse_suggestions


class EvalVerifyMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url_contains: str | None = None
    text_contains: str | None = None


class EvalRequestMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    selector_contains: str | None = None
    verify: EvalVerifyMatch = Field(default_factory=EvalVerifyMatch)


class EvalMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str | None = None
    final_surface_any: list[str] = Field(default_factory=list)
    status_any: list[str] = Field(default_factory=list)
    request: EvalRequestMatch = Field(default_factory=EvalRequestMatch)

    @field_validator("final_surface_any", "status_any", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


class EvalSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    goal: str
    surfaces: list[str] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    runs: int = Field(default=1, ge=1)
    failure_buckets: list[str] = Field(default_factory=list)
    slice_type: str = Field(default="bounded_long_running")
    substrate: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    match: EvalMatch = Field(default_factory=EvalMatch)

    @field_validator("surfaces", "failure_buckets", "substrate", "expected_artifacts", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


def load_eval_spec(spec_path: str | Path) -> EvalSpec:
    payload = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("eval spec must decode to a YAML object")
    return EvalSpec.model_validate(payload)


def _contains_casefold(value: Any, expected: str | None) -> bool:
    text = str(expected or "").strip()
    if not text:
        return True
    return text.casefold() in str(value or "").casefold()


def _trajectory_matches_spec(trajectory: dict[str, Any], spec: EvalSpec) -> bool:
    match = spec.match
    request = trajectory.get("request") or {}
    request_verify = request.get("verify") or {}

    if match.action and str(trajectory.get("action") or "").strip() != match.action:
        return False
    if match.final_surface_any and str(trajectory.get("final_surface") or "").strip() not in match.final_surface_any:
        return False
    if match.status_any and str(trajectory.get("status") or "").strip() not in match.status_any:
        return False
    if match.request.selector_contains and not _contains_casefold(
        request.get("selector"),
        match.request.selector_contains,
    ):
        return False
    if match.request.verify.url_contains and not _contains_casefold(
        request_verify.get("url_contains"),
        match.request.verify.url_contains,
    ):
        return False
    if match.request.verify.text_contains and not _contains_casefold(
        request_verify.get("text_contains"),
        match.request.verify.text_contains,
    ):
        return False
    return True


def _criteria_summary(spec: EvalSpec, trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    verification = trajectory.get("verification")
    if isinstance(verification, dict):
        checks = verification.get("checks")
        if isinstance(checks, list):
            return [
                {
                    "name": str(item.get("name") or ""),
                    "passed": bool(item.get("passed")),
                    "expected": item.get("expected"),
                }
                for item in checks
                if isinstance(item, dict)
            ]
    return [
        {
            "name": f"criterion_{index + 1}",
            "passed": None,
            "expected": criterion,
        }
        for index, criterion in enumerate(spec.success_criteria)
    ]


def _recommended_repair_targets(failure_type: str | None) -> list[str]:
    mapping = {
        "weak_evidence": [
            "strengthen destination-bound verifier",
            "capture stronger post-action text or screenshot evidence",
        ],
        "focus_mismatch": [
            "record and verify frontmost app before action",
            "strengthen current-tab and desktop focus recovery",
        ],
        "target_context_mismatch": [
            "bind action to destination URL or window before typing",
            "strengthen current-tab context preservation and replay checks",
        ],
        "unknown": [
            "inspect replay trace and attempts for a more specific bucket",
        ],
    }
    return mapping.get(str(failure_type or "unknown"), mapping["unknown"])


def _candidate_promotion_artifacts(failure_type: str | None) -> list[str]:
    mapping = {
        "weak_evidence": ["approved_improvement_memory", "approved_skill"],
        "focus_mismatch": ["approved_improvement_memory", "approved_skill"],
        "target_context_mismatch": ["approved_improvement_memory", "capability_patch"],
        "unknown": ["approved_improvement_memory"],
    }
    return list(mapping.get(str(failure_type or "unknown"), mapping["unknown"]))


def _build_run_report_entry(
    trajectory: dict[str, Any],
    spec: EvalSpec,
    *,
    run_index: int,
    get_memory_store_fn: Callable[[], Any],
) -> dict[str, Any]:
    classification = normalize_trajectory_failure(trajectory, classified_by="replay_analysis")
    reuse = prefilter_reuse_suggestions(
        trajectory,
        limit=3,
        get_memory_store_fn=get_memory_store_fn,
    )
    verification = trajectory.get("verification") if isinstance(trajectory.get("verification"), dict) else {}
    replay_reference = {
        "trajectory_id": trajectory.get("id"),
        "status": trajectory.get("status"),
        "final_surface": trajectory.get("final_surface"),
        "created_at": trajectory.get("created_at"),
    }
    return {
        "run_index": run_index,
        "run_job_id": f"{spec.id}/run-{run_index}",
        "trajectory_id": trajectory.get("id"),
        "status": trajectory.get("status"),
        "final_surface": trajectory.get("final_surface"),
        "failure_type": classification["failure_type"],
        "preliminary_failure_type": classification["preliminary_failure_type"],
        "normalized_failure_type": classification["normalized_failure_type"],
        "classified_by": classification["classified_by"],
        "operator_override": classification["operator_override"],
        "verification_status": verification.get("status") or "",
        "verifier_result": {
            "status": verification.get("status") or "",
            "success": (
                bool(verification.get("success"))
                if "success" in verification
                else None
            ),
            "check_count": (
                len(verification.get("checks") or [])
                if isinstance(verification.get("checks"), list)
                else 0
            ),
        },
        "criteria": _criteria_summary(spec, trajectory),
        "recommended_repair_targets": _recommended_repair_targets(classification["failure_type"]),
        "candidate_promotion_artifacts": _candidate_promotion_artifacts(classification["failure_type"]),
        "reuse_query": {
            "kinds": list(REUSE_MEMORY_KINDS),
            "strategy": "prefilter",
        },
        "reuse_suggestions": reuse,
        "replay_reference": replay_reference,
        "replay": replay_reference,
    }


def _persist_failure_classification(store: Any, trajectory: dict[str, Any]) -> dict[str, Any]:
    classification = normalize_trajectory_failure(trajectory, classified_by="replay_analysis")
    if (
        trajectory.get("preliminary_failure_type") != classification["preliminary_failure_type"]
        or trajectory.get("normalized_failure_type") != classification["normalized_failure_type"]
        or list(trajectory.get("classified_by") or []) != classification["classified_by"]
        or trajectory.get("operator_override") != classification["operator_override"]
    ):
        store.update_failure_classification(
            int(trajectory["id"]),
            preliminary_failure_type=classification["preliminary_failure_type"],
            normalized_failure_type=classification["normalized_failure_type"],
            classified_by=classification["classified_by"],
            operator_override=classification["operator_override"],
        )
        updated = store.get(int(trajectory["id"]))
        if updated is not None:
            return updated
    return {
        **trajectory,
        **classification,
    }


def _without_operator_override(trajectory: dict[str, Any]) -> dict[str, Any]:
    classified_by = [
        str(item).strip()
        for item in (trajectory.get("classified_by") or [])
        if str(item).strip() and str(item).strip() != "operator"
    ]
    return {
        **trajectory,
        "normalized_failure_type": None,
        "operator_override": None,
        "classified_by": classified_by,
    }


def override_trajectory_failure_type(
    trajectory_id: int,
    *,
    failure_type: str | None,
    get_trajectory_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    resolved_trajectory_store_fn = get_trajectory_store_fn or get_computer_trajectory_store
    store = resolved_trajectory_store_fn()
    trajectory = store.get(int(trajectory_id))
    if trajectory is None:
        return {
            "success": False,
            "error": f"Unknown computer trajectory: {trajectory_id}",
        }

    requested_override = str(failure_type or "").strip() or None
    if requested_override is not None and requested_override not in PHASE0_FAILURE_BUCKETS:
        return {
            "success": False,
            "error": f"Unsupported failure type override: {requested_override}",
        }

    baseline = normalize_trajectory_failure(_without_operator_override(trajectory))
    classified_by = [
        str(item).strip()
        for item in (baseline.get("classified_by") or [])
        if str(item).strip()
    ]
    if requested_override and "operator" not in classified_by:
        classified_by.append("operator")

    normalized_failure_type = requested_override or baseline.get("normalized_failure_type")
    updated = store.update_failure_classification(
        int(trajectory_id),
        preliminary_failure_type=baseline.get("preliminary_failure_type"),
        normalized_failure_type=normalized_failure_type,
        classified_by=classified_by,
        operator_override=requested_override,
    )
    if not updated:
        return {
            "success": False,
            "error": f"Failed to update computer trajectory: {trajectory_id}",
        }

    refreshed = store.get(int(trajectory_id))
    return {
        "success": True,
        "trajectory": refreshed,
        "trajectory_id": trajectory_id,
        "operator_override": requested_override,
    }


def _failure_bucket_delta(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, dict[str, int]]:
    keys = sorted(
        {
            *[str(key) for key in (current or {}).keys()],
            *[str(key) for key in (baseline or {}).keys()],
        }
    )
    return {
        key: {
            "current": int((current or {}).get(key, 0) or 0),
            "baseline": int((baseline or {}).get(key, 0) or 0),
            "delta": int((current or {}).get(key, 0) or 0) - int((baseline or {}).get(key, 0) or 0),
        }
        for key in keys
    }


def compare_eval_reports(
    current_report: dict[str, Any],
    baseline_report: dict[str, Any],
) -> dict[str, Any]:
    current_buckets = current_report.get("failure_buckets") if isinstance(current_report, dict) else {}
    baseline_buckets = baseline_report.get("failure_buckets") if isinstance(baseline_report, dict) else {}
    failure_buckets = _failure_bucket_delta(
        current_buckets if isinstance(current_buckets, dict) else {},
        baseline_buckets if isinstance(baseline_buckets, dict) else {},
    )
    improved = [
        key
        for key, item in failure_buckets.items()
        if int(item["delta"]) < 0
    ]
    regressed = [
        key
        for key, item in failure_buckets.items()
        if int(item["delta"]) > 0
    ]
    current_success_rate = float(current_report.get("success_rate") or 0.0)
    baseline_success_rate = float(baseline_report.get("success_rate") or 0.0)
    current_runs = int(current_report.get("runs_evaluated") or 0)
    baseline_runs = int(baseline_report.get("runs_evaluated") or 0)
    return {
        "success_rate": {
            "current": round(current_success_rate, 4),
            "baseline": round(baseline_success_rate, 4),
            "delta": round(current_success_rate - baseline_success_rate, 4),
        },
        "runs_evaluated": {
            "current": current_runs,
            "baseline": baseline_runs,
            "delta": current_runs - baseline_runs,
        },
        "failure_buckets": failure_buckets,
        "improved_buckets": improved,
        "regressed_buckets": regressed,
    }


def _resolve_eval_task(
    store: Any,
    *,
    task_id: str | None = None,
    eval_id: str | None = None,
) -> dict[str, Any] | None:
    if task_id:
        return store.get(task_id)
    if not eval_id:
        return None
    recent = store.query(kind="eval_run", page=1, page_size=100)["tasks"]
    for item in recent:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if str(metadata.get("eval_id") or "") == str(eval_id):
            return item
    return None


def _select_trajectories(
    store: Any,
    spec: EvalSpec,
    *,
    trajectory_id: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    if trajectory_id is not None:
        trajectory = store.get(int(trajectory_id))
        return [trajectory] if trajectory is not None and _trajectory_matches_spec(trajectory, spec) else []

    requested = max(1, int(limit or spec.runs))
    candidates = store.recent(limit=max(requested * 10, 50))
    matches = [trajectory for trajectory in candidates if _trajectory_matches_spec(trajectory, spec)]
    return matches[:requested]


def run_eval_spec(
    spec_path: str | Path,
    *,
    trajectory_id: int | None = None,
    limit: int | None = None,
    get_trajectory_store_fn: Callable[[], Any] | None = None,
    get_task_store_fn: Callable[[], Any] | None = None,
    get_memory_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    spec = load_eval_spec(spec_path)
    resolved_trajectory_store_fn = get_trajectory_store_fn or get_computer_trajectory_store
    resolved_task_store_fn = get_task_store_fn or get_task_store
    resolved_memory_store_fn = get_memory_store_fn or get_memory_store

    store = resolved_trajectory_store_fn()
    trajectories = _select_trajectories(
        store,
        spec,
        trajectory_id=trajectory_id,
        limit=limit,
    )

    task_store = resolved_task_store_fn()
    task = task_store.create(
        kind="eval_run",
        title=f"Eval run {spec.id}",
        status="running",
        owner_session_id="local_cli",
        owner_user_id="local_cli",
        artifacts={
            "spec": spec.model_dump(mode="json"),
            "spec_path": str(spec_path),
        },
        metadata={
            "eval_id": spec.id,
            "trajectory_id": trajectory_id,
        },
    )

    if not trajectories:
        error = "No trajectories matched this eval spec"
        task_store.update(
            task["task_id"],
            status="failed",
            artifacts={
                "report": {
                    "success": False,
                    "eval_id": spec.id,
                    "goal": spec.goal,
                    "spec_path": str(spec_path),
                    "runs_requested": max(1, int(limit or spec.runs)),
                    "runs_evaluated": 0,
                    "success_rate": 0.0,
                    "failure_buckets": {},
                    "reports": [],
                    "error": error,
                }
            },
            error=error,
        )
        return {
            "success": False,
            "task_id": task["task_id"],
            "error": error,
        }

    normalized_runs = [_persist_failure_classification(store, trajectory) for trajectory in trajectories]
    reports = [
        _build_run_report_entry(
            trajectory,
            spec,
            run_index=index + 1,
            get_memory_store_fn=resolved_memory_store_fn,
        )
        for index, trajectory in enumerate(normalized_runs)
    ]
    success_count = sum(1 for item in reports if item["status"] in {"success", "recovered"})
    buckets = Counter(
        item["failure_type"]
        for item in reports
        if item.get("failure_type")
    )
    configured = spec.failure_buckets or list(PHASE0_FAILURE_BUCKETS)
    failure_buckets = {
        bucket: int(buckets.get(bucket, 0))
        for bucket in configured
        if buckets.get(bucket, 0) or bucket in configured
    }

    report = {
        "success": True,
        "eval_id": spec.id,
        "goal": spec.goal,
        "spec_path": str(spec_path),
        "slice": {
            "type": spec.slice_type,
            "substrate": spec.substrate
            or [
                "task_store",
                "trajectory_store",
                "replay_report",
                "approval_gated_promotion",
            ],
            "expected_artifacts": spec.expected_artifacts
            or [
                "trajectory_id",
                "verifier_result",
                "failure_type",
                "recommended_repair_targets",
                "candidate_promotion_artifacts",
                "replay_reference",
                "reuse_suggestions",
            ],
        },
        "runs_requested": max(1, int(limit or spec.runs)),
        "runs_evaluated": len(reports),
        "success_rate": round(success_count / len(reports), 4),
        "failure_buckets": failure_buckets,
        "run_jobs": reports,
        "reports": reports,
    }
    task_store.update(
        task["task_id"],
        status="completed",
        artifacts={"report": report},
    )
    return {
        "success": True,
        "task_id": task["task_id"],
        **report,
    }


def get_eval_report(
    *,
    task_id: str | None = None,
    eval_id: str | None = None,
    compare_to_task_id: str | None = None,
    compare_to_eval_id: str | None = None,
    get_task_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if not task_id and not eval_id:
        return {
            "success": False,
            "error": "task_id or eval_id is required",
        }

    store = (get_task_store_fn or get_task_store)()
    task = _resolve_eval_task(store, task_id=task_id, eval_id=eval_id)

    if task is None:
        return {
            "success": False,
            "error": "Eval report not found",
        }

    artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
    report = artifacts.get("report") if isinstance(artifacts.get("report"), dict) else {}
    spec = artifacts.get("spec") if isinstance(artifacts.get("spec"), dict) else {}
    payload = {
        "success": True,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "spec": spec,
        "report": report,
    }
    if compare_to_task_id or compare_to_eval_id:
        baseline_task = _resolve_eval_task(
            store,
            task_id=compare_to_task_id,
            eval_id=compare_to_eval_id,
        )
        if baseline_task is None:
            return {
                "success": False,
                "error": "Comparison eval report not found",
            }
        baseline_artifacts = (
            baseline_task.get("artifacts")
            if isinstance(baseline_task.get("artifacts"), dict)
            else {}
        )
        baseline_report = (
            baseline_artifacts.get("report")
            if isinstance(baseline_artifacts.get("report"), dict)
            else {}
        )
        payload["comparison"] = compare_eval_reports(report, baseline_report)
        payload["compare_to_task_id"] = baseline_task.get("task_id")
        payload["compare_to_eval_id"] = (
            baseline_task.get("metadata", {}).get("eval_id")
            if isinstance(baseline_task.get("metadata"), dict)
            else None
        )
    return payload
