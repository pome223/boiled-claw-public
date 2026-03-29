"""Offline canary and benchmark-gated self-improvement tools."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.config.settings import get_settings
from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context
from src.tools.memory import memory_store
from src.tools.shell import run_shell_guarded


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
