import subprocess
from pathlib import Path

import pytest

from src.tools import self_improvement


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.mark.asyncio
async def test_prepare_canary_creates_worktree(git_repo, tmp_path):
    result = await self_improvement.self_improvement_prepare_canary(
        goal="Try a benchmarked fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    assert result["success"] is True
    assert Path(result["canary_path"]).exists()
    assert result["branch"].startswith("canary/try-a-benchmarked-fix-")


@pytest.mark.asyncio
async def test_run_benchmarks_reports_failures(git_repo, tmp_path):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Run checks",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    result = await self_improvement.self_improvement_run_benchmarks(
        canary_path=prepare["canary_path"],
        commands="printf 'ok'\nfalse",
        fail_fast=False,
    )

    assert result["success"] is True
    assert result["all_passed"] is False
    assert len(result["results"]) == 2
    assert result["results"][0]["passed"] is True
    assert result["results"][1]["passed"] is False


@pytest.mark.asyncio
async def test_package_candidate_reuses_cached_benchmark_results(git_repo, tmp_path, monkeypatch):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Reuse benchmark result",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )
    canary = Path(prepare["canary_path"])
    (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    run_calls = 0

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        nonlocal run_calls
        run_calls += 1
        return {"stdout": "ok", "stderr": "", "return_code": 0}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)

    first = await self_improvement.self_improvement_run_benchmarks(
        canary_path=str(canary),
        commands="printf ok",
    )
    packaged = await self_improvement.self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands="printf ok",
        improvement_summary="Reuse benchmark output",
    )

    assert first["all_passed"] is True
    assert packaged["success"] is True
    assert packaged["benchmark_reused"] is True
    assert run_calls == 1


@pytest.mark.asyncio
async def test_package_candidate_is_benchmark_gated_and_can_record_approved_memory(git_repo, tmp_path, monkeypatch):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Package candidate",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )
    canary = Path(prepare["canary_path"])
    (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    recorded = {}

    async def _memory_store(content, tags=None, metadata=None, kind="fact", tool_context=None):
        recorded["content"] = content
        recorded["kind"] = kind
        return {"success": True, "memory_id": 7, "kind": kind}

    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)

    result = await self_improvement.self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands="printf 'ok'\n",
        improvement_summary="Improve README wording",
        record_as_approved=True,
    )

    assert result["success"] is True
    assert result["promotable"] is True
    assert "README.md" in result["diff_stat"]
    assert recorded["kind"] == "approved_improvement"


@pytest.mark.asyncio
async def test_cleanup_canary_removes_worktree_and_branch(git_repo, tmp_path):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Cleanup candidate",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    cleanup = await self_improvement.self_improvement_cleanup_canary(
        canary_path=prepare["canary_path"],
    )

    assert cleanup["success"] is True
    assert cleanup["worktree_removed"] is True
    assert Path(prepare["canary_path"]).exists() is False
