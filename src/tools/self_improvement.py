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


def _run_shell(command: str, cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        executable="/bin/sh",
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
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

    timeout = timeout_seconds or get_settings().self_improvement_benchmark_timeout_seconds
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in command_list:
        try:
            completed = _run_shell(command, canary, timeout)
            entry = {
                "command": command,
                "return_code": completed.returncode,
                "passed": completed.returncode == 0,
                "stdout": _trim_output(completed.stdout.strip()),
                "stderr": _trim_output(completed.stderr.strip()),
            }
        except subprocess.TimeoutExpired:
            entry = {
                "command": command,
                "return_code": None,
                "passed": False,
                "stdout": "",
                "stderr": f"Timed out after {timeout} seconds",
            }
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
