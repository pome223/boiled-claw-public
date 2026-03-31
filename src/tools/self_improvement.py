"""Offline canary and benchmark-gated self-improvement tools."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.config.settings import get_settings
from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context
from src.tools.memory import memory_store
from src.tools.shell import run_shell_guarded
from src.tools.tasks import create_task_record, update_task_record


_STATE_DIRNAME = ".boiled-claw-self-improvement"
_STATE_FILENAME = "state.json"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "canary"


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _trim_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def _repo_path(repo_path: Optional[str]) -> Path:
    return Path(repo_path or ".").resolve()


def _worktree_root(worktree_root: Optional[str]) -> Path:
    settings = get_settings()
    return Path(worktree_root).resolve() if worktree_root else settings.self_improvement_canary_root.resolve()


def _split_commands(commands: str) -> list[str]:
    return [line.strip() for line in commands.splitlines() if line.strip()]


def _state_path(canary: Path) -> Path:
    return canary / _STATE_DIRNAME / _STATE_FILENAME


def _read_state(canary: Path) -> dict[str, Any]:
    state_path = _state_path(canary)
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_state(canary: Path, state: dict[str, Any]) -> None:
    state_path = _state_path(canary)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _persist_state(canary: Path, **updates: Any) -> dict[str, Any]:
    state = _read_state(canary)
    state.update(updates)
    _write_state(canary, state)
    return state


def _cached_benchmark_result(canary: Path, commands: str) -> dict[str, Any] | None:
    state = _read_state(canary)
    benchmark = state.get("benchmark")
    if not isinstance(benchmark, dict):
        return None
    cached_commands = benchmark.get("commands")
    command_list = _split_commands(commands)
    if cached_commands != command_list:
        return None
    if not benchmark.get("all_passed") and benchmark.get("fail_fast"):
        return None
    return {
        "success": True,
        "all_passed": bool(benchmark.get("all_passed")),
        "canary_path": str(canary),
        "results": list(benchmark.get("results") or []),
        "reused": True,
    }


def _trajectory_failure_reason(trajectory: dict[str, Any]) -> str:
    verification = trajectory.get("verification")
    if isinstance(verification, dict) and not verification.get("success"):
        return f"verification {verification.get('status', 'failed')}"
    for attempt in trajectory.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        result = attempt.get("result")
        if isinstance(result, dict):
            error = str(result.get("error") or "").strip()
            if error:
                return error
        verification = attempt.get("verification")
        if isinstance(verification, dict) and not verification.get("success"):
            return f"verification {verification.get('status', 'failed')}"
    return "unknown failure"


def _trajectory_demo_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Investigate failed {action} trajectory {trajectory.get('id')} for {target}"


def _trajectory_search_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Search repair candidates for failed {action} trajectory {trajectory.get('id')} for {target}"


def _trajectory_improvement_summary(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or "unknown-target"
    )
    return (
        f"Demo candidate for failed computer trajectory {trajectory.get('id')}: "
        f"improve {action} handling around {target} after {_trajectory_failure_reason(trajectory)}."
    )


def _parse_candidate_specs(candidate_specs_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(candidate_specs_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"candidate_specs_json must be valid JSON: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError("candidate_specs_json must be a non-empty JSON array")

    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Candidate spec #{index} must be an object")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Candidate spec #{index} must include a non-empty name")
        commands_value = entry.get("commands")
        if isinstance(commands_value, list):
            commands = [str(command).strip() for command in commands_value if str(command).strip()]
        elif isinstance(commands_value, str):
            commands = _split_commands(commands_value)
        else:
            commands = []
        if not commands:
            raise ValueError(f"Candidate spec '{name}' must include at least one command")

        goal = str(entry.get("goal") or "").strip() or None
        improvement_summary = str(entry.get("improvement_summary") or "").strip() or None
        normalized.append(
            {
                "name": name,
                "commands": commands,
                "goal": goal,
                "improvement_summary": improvement_summary,
            }
        )
    return normalized


def _search_candidate_goal(search_goal: str, candidate_name: str, candidate_goal: Optional[str]) -> str:
    if candidate_goal:
        return candidate_goal
    return f"{search_goal} [{candidate_name}]"


def _search_candidate_summary(
    base_summary: str,
    candidate_name: str,
    candidate_summary: Optional[str],
) -> str:
    if candidate_summary:
        return candidate_summary
    return f"{base_summary} Candidate: {candidate_name}."


def _candidate_diff_metrics(canary: Path) -> dict[str, int]:
    numstat = _run_git(canary, "diff", "--numstat")
    changed_files = 0
    changed_lines = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        changed_files += 1
        added = 0 if parts[0] == "-" else int(parts[0])
        deleted = 0 if parts[1] == "-" else int(parts[1])
        changed_lines += added + deleted
    return {"changed_files": changed_files, "changed_lines": changed_lines}


def _candidate_benchmark_counts(package: dict[str, Any]) -> tuple[int, int]:
    benchmark = package.get("benchmark")
    if not isinstance(benchmark, dict):
        return (0, 0)
    results = benchmark.get("results")
    if not isinstance(results, list):
        return (0, 0)
    passed = sum(1 for result in results if isinstance(result, dict) and result.get("passed"))
    return (passed, len(results))


def _candidate_ranking_key(candidate: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    package = candidate.get("package")
    package = package if isinstance(package, dict) else {}
    passed, total = _candidate_benchmark_counts(package)
    diff_metrics = candidate.get("diff_metrics")
    diff_metrics = diff_metrics if isinstance(diff_metrics, dict) else {}
    changed_files = int(diff_metrics.get("changed_files") or 0)
    changed_lines = int(diff_metrics.get("changed_lines") or 0)
    return (
        1 if package.get("promotable") else 0,
        1 if package.get("success") else 0,
        passed,
        -total,
        -changed_files,
        -changed_lines,
    )


async def _run_candidate_commands(
    *,
    canary: Path,
    commands: str,
    timeout_seconds: int,
    tool_context: Optional[ToolContext],
) -> dict[str, Any]:
    command_list = _split_commands(commands)
    if not command_list:
        return {"success": False, "error": "At least one candidate command is required"}

    timeout = timeout_seconds or get_settings().self_improvement_benchmark_timeout_seconds
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in command_list:
        completed = await run_shell_guarded(
            command=command,
            timeout=timeout,
            cwd=str(canary),
            tool_context=tool_context,
        )
        entry = {
            "command": command,
            "return_code": completed.get("return_code"),
            "passed": completed.get("return_code") == 0,
            "stdout": _trim_output(str(completed.get("stdout") or "").strip()),
            "stderr": _trim_output(str(completed.get("stderr") or "").strip()),
        }
        if completed.get("error") and not entry["stderr"]:
            entry["stderr"] = str(completed["error"])
        results.append(entry)
        if not entry["passed"]:
            all_passed = False
            break

    payload = {
        "success": all_passed,
        "canary_path": str(canary),
        "results": results,
    }
    _persist_state(
        canary,
        candidate_commands={
            "commands": command_list,
            "all_passed": all_passed,
            "results": results,
            "completed_at": time.time(),
        },
    )
    if not all_passed:
        payload["error"] = "candidate command failed"
    return payload


async def self_improvement_prepare_canary(
    goal: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Create an offline git worktree for self-improvement experiments."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    repo = _repo_path(repo_path)
    root = _worktree_root(worktree_root)
    slug = _slugify(goal)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch_name = f"canary/{slug}-{stamp}"
    target = root / f"{slug}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)

    rev_parse = _run_git(repo, "rev-parse", "--show-toplevel")
    if rev_parse.returncode != 0:
        error = rev_parse.stderr.strip() or "not a git repository"
        return {"success": False, "error": error}

    result = _run_git(repo, "worktree", "add", "-b", branch_name, str(target), base_ref)
    success = result.returncode == 0
    payload = {
        "success": success,
        "goal": goal,
        "repo_path": str(repo),
        "canary_path": str(target),
        "branch": branch_name,
        "base_ref": base_ref,
        "stdout": _trim_output(result.stdout.strip()),
        "stderr": _trim_output(result.stderr.strip()),
    }
    if success:
        _write_state(
            target,
            {
                "goal": goal,
                "repo_path": str(repo),
                "canary_path": str(target),
                "branch": branch_name,
                "base_ref": base_ref,
                "created_at": time.time(),
            },
        )
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_prepare_canary",
        resource=str(target),
        result="success" if success else f"error:{payload['stderr'] or payload['stdout']}",
        metadata={"repo_path": str(repo), "branch": branch_name, "base_ref": base_ref},
    )
    return payload


async def self_improvement_run_benchmarks(
    canary_path: str,
    commands: str,
    timeout_seconds: int = 0,
    fail_fast: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Run benchmark commands inside a canary worktree."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    canary = Path(canary_path).resolve()
    if not canary.exists():
        return {"success": False, "error": f"Canary path does not exist: {canary}"}

    command_list = _split_commands(commands)
    if not command_list:
        return {"success": False, "error": "At least one benchmark command is required"}

    cached = _cached_benchmark_result(canary, commands)
    if cached is not None:
        return cached

    timeout = timeout_seconds or get_settings().self_improvement_benchmark_timeout_seconds
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in command_list:
        completed = await run_shell_guarded(
            command=command,
            timeout=timeout,
            cwd=str(canary),
            tool_context=tool_context,
        )
        entry = {
            "command": command,
            "return_code": completed.get("return_code"),
            "passed": completed.get("return_code") == 0,
            "stdout": _trim_output(str(completed.get("stdout") or "").strip()),
            "stderr": _trim_output(str(completed.get("stderr") or "").strip()),
        }
        if completed.get("error") and not entry["stderr"]:
            entry["stderr"] = str(completed["error"])
        results.append(entry)
        if not entry["passed"]:
            all_passed = False
            if fail_fast:
                break

    payload = {
        "success": True,
        "all_passed": all_passed,
        "canary_path": str(canary),
        "results": results,
    }
    _persist_state(
        canary,
        benchmark={
            "commands": command_list,
            "timeout_seconds": timeout,
            "fail_fast": fail_fast,
            "all_passed": all_passed,
            "results": results,
            "completed_at": time.time(),
        },
    )
    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_run_benchmarks",
        resource=str(canary),
        result="success" if all_passed else "benchmark_failed",
        metadata={"count": len(results), "all_passed": all_passed},
    )
    return payload


async def self_improvement_package_candidate(
    canary_path: str,
    benchmark_commands: str,
    improvement_summary: str,
    repo_path: Optional[str] = None,
    timeout_seconds: int = 0,
    record_as_approved: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Benchmark-gate a canary and package the candidate diff for review."""

    benchmark_result = await self_improvement_run_benchmarks(
        canary_path=canary_path,
        commands=benchmark_commands,
        timeout_seconds=timeout_seconds,
        fail_fast=False,
        tool_context=tool_context,
    )
    if not benchmark_result.get("success"):
        return benchmark_result

    canary = Path(canary_path).resolve()
    repo = _repo_path(repo_path or canary_path)
    diff_stat = _run_git(canary, "diff", "--stat")
    diff_patch = _run_git(canary, "diff", "--minimal", "--binary", "--no-ext-diff")
    branch = _run_git(canary, "rev-parse", "--abbrev-ref", "HEAD")

    payload = {
        "success": True,
        "promotable": bool(benchmark_result["all_passed"]),
        "canary_path": str(canary),
        "repo_path": str(repo),
        "branch": branch.stdout.strip(),
        "improvement_summary": improvement_summary,
        "benchmark": benchmark_result,
        "benchmark_reused": bool(benchmark_result.get("reused")),
        "diff_stat": diff_stat.stdout.strip(),
        "diff_excerpt": _trim_output(diff_patch.stdout.strip(), limit=8000),
    }

    if record_as_approved and benchmark_result["all_passed"]:
        memory_payload = await memory_store(
            content=improvement_summary,
            tags="self-improvement,approved",
            metadata=json.dumps(
                {
                    "canary_path": str(canary),
                    "branch": payload["branch"],
                    "benchmark_results": benchmark_result["results"],
                    "diff_stat": payload["diff_stat"],
                },
                ensure_ascii=True,
            ),
            kind="approved_improvement",
            tool_context=tool_context,
        )
        payload["approved_memory"] = memory_payload

    return payload


async def self_improvement_cleanup_canary(
    canary_path: str,
    remove_branch: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Remove an offline canary worktree and optionally delete its canary branch."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    canary = Path(canary_path).resolve()
    if not canary.exists():
        return {"success": False, "error": f"Canary path does not exist: {canary}"}

    state = _read_state(canary)
    repo = _repo_path(state.get("repo_path") or str(canary))
    branch_name = str(state.get("branch") or "").strip()
    if not branch_name:
        branch = _run_git(canary, "rev-parse", "--abbrev-ref", "HEAD")
        branch_name = branch.stdout.strip()

    remove_result = _run_git(repo, "worktree", "remove", "--force", str(canary))
    success = remove_result.returncode == 0
    branch_deleted = False
    branch_error = ""
    if success and remove_branch and branch_name:
        branch_result = _run_git(repo, "branch", "-D", branch_name)
        branch_deleted = branch_result.returncode == 0
        branch_error = branch_result.stderr.strip()

    payload = {
        "success": success,
        "canary_path": str(canary),
        "branch": branch_name,
        "worktree_removed": success,
        "branch_deleted": branch_deleted,
        "stdout": _trim_output(remove_result.stdout.strip()),
        "stderr": _trim_output(remove_result.stderr.strip()),
    }
    if branch_error:
        payload["branch_stderr"] = _trim_output(branch_error)

    audit_logger.log(
        event_type=AuditEventType.SHELL_COMMAND,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="self_improvement_cleanup_canary",
        resource=str(canary),
        result="success" if success else f"error:{payload['stderr'] or payload['stdout']}",
        metadata={"branch": branch_name, "remove_branch": remove_branch},
    )
    return payload


async def self_improvement_demo_from_trajectory(
    trajectory_id: int,
    candidate_commands: str,
    benchmark_commands: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    goal: Optional[str] = None,
    improvement_summary: Optional[str] = None,
    timeout_seconds: int = 0,
    record_as_approved: bool = False,
    auto_cleanup: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Run a failed computer trajectory through one canary -> benchmark -> package demo flow."""

    trajectory = get_computer_trajectory_store().get(trajectory_id)
    if trajectory is None:
        return {"success": False, "error": f"Unknown computer trajectory: {trajectory_id}"}
    if str(trajectory.get("status") or "") != "failed":
        return {
            "success": False,
            "error": f"Trajectory {trajectory_id} must have status=failed for the demo flow",
            "trajectory": trajectory,
        }

    resolved_goal = goal or _trajectory_demo_goal(trajectory)
    resolved_summary = improvement_summary or _trajectory_improvement_summary(trajectory)
    task_record = create_task_record(
        kind="self_improvement_demo",
        title=resolved_goal,
        status="running",
        artifacts={
            "trajectory": trajectory,
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
        },
        metadata={
            "trajectory_id": trajectory_id,
            "record_as_approved": record_as_approved,
            "auto_cleanup": auto_cleanup,
        },
        tool_context=tool_context,
    )
    task_id = str(task_record["task_id"])

    prepare = await self_improvement_prepare_canary(
        goal=resolved_goal,
        repo_path=repo_path,
        base_ref=base_ref,
        worktree_root=worktree_root,
        tool_context=tool_context,
    )
    if not prepare.get("success"):
        update_task_record(
            task_id,
            status="failed",
            artifacts={"prepare": prepare},
            error=prepare.get("error") or prepare.get("stderr") or "failed to prepare canary",
        )
        return {
            "success": False,
            "task_id": task_id,
            "trajectory": trajectory,
            "prepare": prepare,
            "error": prepare.get("error") or prepare.get("stderr") or "failed to prepare canary",
        }

    canary = Path(prepare["canary_path"]).resolve()
    _persist_state(
        canary,
        demo={
            "trajectory_id": trajectory_id,
            "trajectory_status": trajectory.get("status"),
            "failure_reason": _trajectory_failure_reason(trajectory),
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
            "started_at": time.time(),
        },
    )

    candidate = await _run_candidate_commands(
        canary=canary,
        commands=candidate_commands,
        timeout_seconds=timeout_seconds,
        tool_context=tool_context,
    )
    if not candidate.get("success"):
        payload = {
            "success": False,
            "task_id": task_id,
            "trajectory": trajectory,
            "prepare": prepare,
            "candidate": candidate,
            "error": candidate.get("error") or "candidate command failed",
        }
        if auto_cleanup:
            payload["cleanup"] = await self_improvement_cleanup_canary(
                canary_path=str(canary),
                tool_context=tool_context,
            )
        update_task_record(
            task_id,
            status="failed",
            artifacts={key: value for key, value in payload.items() if key in {"prepare", "candidate", "cleanup"}},
            error=payload["error"],
        )
        return payload

    packaged = await self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands=benchmark_commands,
        improvement_summary=resolved_summary,
        repo_path=repo_path,
        timeout_seconds=timeout_seconds,
        record_as_approved=record_as_approved,
        tool_context=tool_context,
    )
    payload = {
        "success": bool(packaged.get("success")),
        "task_id": task_id,
        "trajectory": trajectory,
        "prepare": prepare,
        "candidate": candidate,
        "package": packaged,
        "goal": resolved_goal,
        "improvement_summary": resolved_summary,
    }
    if auto_cleanup:
        payload["cleanup"] = await self_improvement_cleanup_canary(
            canary_path=str(canary),
            tool_context=tool_context,
        )
    update_task_record(
        task_id,
        status="completed" if payload["success"] else "failed",
        artifacts={key: value for key, value in payload.items() if key in {"prepare", "candidate", "package", "cleanup"}},
        error=None if payload["success"] else packaged.get("error"),
    )
    return payload


async def self_improvement_search_from_trajectory(
    trajectory_id: int,
    candidate_specs_json: str,
    benchmark_commands: str,
    repo_path: Optional[str] = None,
    base_ref: str = "HEAD",
    worktree_root: Optional[str] = None,
    goal: Optional[str] = None,
    improvement_summary: Optional[str] = None,
    timeout_seconds: int = 0,
    record_winner_as_approved: bool = False,
    cleanup_losers: bool = True,
    auto_cleanup: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Search across multiple canaries for the best benchmarked repair of a failed trajectory."""

    trajectory = get_computer_trajectory_store().get(trajectory_id)
    if trajectory is None:
        return {"success": False, "error": f"Unknown computer trajectory: {trajectory_id}"}
    if str(trajectory.get("status") or "") != "failed":
        return {
            "success": False,
            "error": f"Trajectory {trajectory_id} must have status=failed for the search flow",
            "trajectory": trajectory,
        }

    try:
        candidate_specs = _parse_candidate_specs(candidate_specs_json)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    resolved_goal = goal or _trajectory_search_goal(trajectory)
    resolved_summary = improvement_summary or _trajectory_improvement_summary(trajectory)
    parent_task = create_task_record(
        kind="self_improvement_search",
        title=resolved_goal,
        status="running",
        artifacts={
            "trajectory": trajectory,
            "goal": resolved_goal,
            "improvement_summary": resolved_summary,
        },
        metadata={
            "trajectory_id": trajectory_id,
            "record_winner_as_approved": record_winner_as_approved,
            "cleanup_losers": cleanup_losers,
            "auto_cleanup": auto_cleanup,
        },
        tool_context=tool_context,
    )
    parent_task_id = str(parent_task["task_id"])

    candidates: list[dict[str, Any]] = []
    winner_index: int | None = None
    winner_key: tuple[int, int, int, int, int, int] | None = None

    for index, spec in enumerate(candidate_specs):
        candidate_name = str(spec["name"])
        candidate_goal = _search_candidate_goal(resolved_goal, candidate_name, spec.get("goal"))
        candidate_summary = _search_candidate_summary(
            resolved_summary,
            candidate_name,
            spec.get("improvement_summary"),
        )

        prepare = await self_improvement_prepare_canary(
            goal=candidate_goal,
            repo_path=repo_path,
            base_ref=base_ref,
            worktree_root=worktree_root,
            tool_context=tool_context,
        )
        candidate_result: dict[str, Any] = {
            "name": candidate_name,
            "index": index,
            "goal": candidate_goal,
            "improvement_summary": candidate_summary,
        }
        candidate_task = create_task_record(
            kind="self_improvement_candidate",
            title=candidate_goal,
            status="running",
            parent_task_id=parent_task_id,
            artifacts={
                "trajectory_id": trajectory_id,
                "candidate_name": candidate_name,
                "goal": candidate_goal,
                "improvement_summary": candidate_summary,
            },
            metadata={"candidate_index": index},
            tool_context=tool_context,
        )
        candidate_result["task_id"] = candidate_task["task_id"]
        candidate_result["prepare"] = prepare
        if not prepare.get("success"):
            candidate_result["success"] = False
            candidate_result["error"] = (
                prepare.get("error") or prepare.get("stderr") or "failed to prepare canary"
            )
            update_task_record(
                str(candidate_task["task_id"]),
                status="failed",
                artifacts={"prepare": prepare},
                error=candidate_result["error"],
            )
            candidates.append(candidate_result)
            continue

        canary = Path(prepare["canary_path"]).resolve()
        _persist_state(
            canary,
            search={
                "trajectory_id": trajectory_id,
                "candidate_name": candidate_name,
                "candidate_index": index,
                "trajectory_status": trajectory.get("status"),
                "failure_reason": _trajectory_failure_reason(trajectory),
                "goal": candidate_goal,
                "improvement_summary": candidate_summary,
                "started_at": time.time(),
            },
        )

        candidate = await _run_candidate_commands(
            canary=canary,
            commands="\n".join(spec["commands"]),
            timeout_seconds=timeout_seconds,
            tool_context=tool_context,
        )
        candidate_result["candidate"] = candidate
        if not candidate.get("success"):
            candidate_result["success"] = False
            candidate_result["error"] = candidate.get("error") or "candidate command failed"
            if cleanup_losers:
                candidate_result["cleanup"] = await self_improvement_cleanup_canary(
                    canary_path=str(canary),
                    tool_context=tool_context,
                )
            update_task_record(
                str(candidate_task["task_id"]),
                status="failed",
                artifacts={
                    key: value
                    for key, value in candidate_result.items()
                    if key in {"prepare", "candidate", "cleanup"}
                },
                error=candidate_result["error"],
            )
            candidates.append(candidate_result)
            continue

        packaged = await self_improvement_package_candidate(
            canary_path=str(canary),
            benchmark_commands=benchmark_commands,
            improvement_summary=candidate_summary,
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            record_as_approved=False,
            tool_context=tool_context,
        )
        candidate_result["package"] = packaged
        candidate_result["diff_metrics"] = _candidate_diff_metrics(canary)
        candidate_result["success"] = bool(packaged.get("success"))
        if not packaged.get("success"):
            candidate_result["error"] = packaged.get("error") or "failed to package candidate"
        update_task_record(
            str(candidate_task["task_id"]),
            status="completed" if candidate_result["success"] else "failed",
            artifacts={
                key: value
                for key, value in candidate_result.items()
                if key in {"prepare", "candidate", "package", "diff_metrics", "ranking_key"}
            },
            error=candidate_result.get("error"),
        )
        ranking_key = _candidate_ranking_key(candidate_result)
        candidate_result["ranking_key"] = list(ranking_key)
        if winner_key is None or ranking_key > winner_key:
            winner_key = ranking_key
            winner_index = index
        candidates.append(candidate_result)

    winner = candidates[winner_index] if winner_index is not None else None
    if winner is not None and winner.get("prepare", {}).get("success"):
        winner_canary = str(winner["prepare"]["canary_path"])
    else:
        winner_canary = None
    has_promotable_winner = bool(winner and winner.get("package", {}).get("promotable"))

    if has_promotable_winner and record_winner_as_approved:
        refreshed = await self_improvement_package_candidate(
            canary_path=str(winner_canary),
            benchmark_commands=benchmark_commands,
            improvement_summary=str(winner["improvement_summary"]),
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            record_as_approved=True,
            tool_context=tool_context,
        )
        winner["package"] = refreshed
        winner["diff_metrics"] = _candidate_diff_metrics(Path(winner_canary))

    for index, candidate in enumerate(candidates):
        canary_path = candidate.get("prepare", {}).get("canary_path")
        if not canary_path:
            continue
        should_cleanup = False
        if winner_index is None:
            should_cleanup = cleanup_losers
        elif not has_promotable_winner:
            should_cleanup = cleanup_losers
        elif index != winner_index:
            should_cleanup = cleanup_losers
        elif auto_cleanup:
            should_cleanup = True
        if should_cleanup:
            candidate["cleanup"] = await self_improvement_cleanup_canary(
                canary_path=str(canary_path),
                tool_context=tool_context,
            )
            update_task_record(
                str(candidate.get("task_id")),
                artifacts={"cleanup": candidate["cleanup"]},
            )

    payload = {
        "success": bool(winner and winner.get("package", {}).get("promotable")),
        "task_id": parent_task_id,
        "trajectory": trajectory,
        "goal": resolved_goal,
        "improvement_summary": resolved_summary,
        "candidates": candidates,
    }
    if winner is not None:
        payload["winner"] = winner
        payload["winner_name"] = winner.get("name")
    if not payload["success"]:
        payload["error"] = "No promotable candidate found"
    candidate_task_ids = [str(candidate.get("task_id")) for candidate in candidates if candidate.get("task_id")]
    winner_task_id = str(winner.get("task_id")) if winner and winner.get("task_id") else None
    loser_task_ids = [task_id for task_id in candidate_task_ids if task_id != winner_task_id]
    update_task_record(
        parent_task_id,
        status="completed" if payload["success"] else "failed",
        winner_task_id=winner_task_id,
        loser_task_ids=loser_task_ids,
        artifacts={
            "candidate_count": len(candidates),
            "candidate_task_ids": candidate_task_ids,
            "winner_name": payload.get("winner_name"),
            "winner_task_id": winner_task_id,
        },
        error=payload.get("error"),
    )
    return payload
