import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.computer_use.trajectory_store import ComputerTrajectoryStore
from src.main import cli
from src.tools.memory import MemoryStore
from src.skills.base import get_skill_registry
import src.skills.runtime as skills_runtime
from src.runtime.task_store import get_task_store
from src.tools import self_improvement
import src.runtime.promoted_capabilities as promoted_capabilities
import src.skills.promoted as promoted_skills
from src.tools.self_improvement_runtime.promotion import REUSE_MEMORY_KINDS
from src.tools.skills import resource_list


@pytest.fixture(autouse=True)
def _local_shell_only(monkeypatch):
    """Force run_shell_guarded to use local subprocess, bypassing Host Bridge."""
    import src.tools.shell as shell_module

    original = shell_module.run_shell_guarded

    async def _patched(command, timeout=30, cwd=None, tool_context=None):
        tokens = command.split()
        try:
            process = await asyncio.create_subprocess_exec(
                *tokens,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "return_code": process.returncode,
            }
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": f"Timed out after {timeout}s", "return_code": None}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "return_code": -1, "error": str(e)}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _patched)


@pytest.fixture(autouse=True)
def _isolated_promotions(monkeypatch, tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    monkeypatch.setattr(self_improvement, "get_memory_store", lambda: store)
    monkeypatch.setattr(promoted_skills, "get_memory_store", lambda: store)
    monkeypatch.setattr(promoted_capabilities, "get_memory_store", lambda: store)
    return store


@pytest.fixture
def _reset_skill_runtime():
    registry = get_skill_registry()
    original_skills = dict(registry.skills)
    original_loaded = skills_runtime._loaded
    original_report = dict(skills_runtime._last_report)

    registry.skills.clear()
    skills_runtime._loaded = False
    skills_runtime._last_report = {"loaded": False, "count": 0, "skills": []}

    yield

    registry.skills.clear()
    registry.skills.update(original_skills)
    skills_runtime._loaded = original_loaded
    skills_runtime._last_report = original_report


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


@pytest.fixture
def computer_trajectory_store(tmp_path, monkeypatch):
    store = ComputerTrajectoryStore(str(tmp_path / "computer_trajectories.db"))
    monkeypatch.setattr(self_improvement, "get_computer_trajectory_store", lambda: store)
    return store


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
        recorded["metadata"] = json.loads(metadata or "{}")
        return {"success": True, "memory_id": 7, "kind": kind}

    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)
    self_improvement._persist_state(
        canary,
        demo={
            "failure_type": "focus_mismatch",
            "reuse_hints": {
                "trajectory_key": "click::current_tab::#save",
                "selector": "#save",
                "failure_type": "focus_mismatch",
            },
        },
    )

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
    assert recorded["metadata"]["trajectory_key"] == "click::current_tab::#save"
    assert recorded["metadata"]["selector"] == "#save"
    assert recorded["metadata"]["failure_type"] == "focus_mismatch"
    assert result["promotion_artifact"]["artifact_kind"] == "approved_improvement_memory"
    assert result["promotion_artifact"]["failure_type"] == "focus_mismatch"


@pytest.mark.asyncio
async def test_package_candidate_can_record_approved_skill_with_explicit_approval(
    git_repo,
    tmp_path,
    monkeypatch,
):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Promote Sheets skill",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )
    canary = Path(prepare["canary_path"])
    (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    recorded = {}

    async def _memory_store(content, tags=None, metadata=None, kind="fact", tool_context=None):
        recorded["content"] = content
        recorded["kind"] = kind
        recorded["tags"] = tags
        recorded["metadata"] = json.loads(metadata or "{}")
        return {"success": True, "memory_id": 9, "kind": kind}

    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)
    self_improvement._persist_state(
        canary,
        demo={
            "trajectory_id": 42,
            "goal": "Enter into Sheets",
            "failure_reason": "verification fail",
            "reuse_hints": {
                "trajectory_key": "fill::current_tab::a1",
                "selector": "A1",
                "surface": "current_tab",
                "target": "google-sheets-entry",
            },
        },
    )

    result = await self_improvement.self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands="printf 'ok'\n",
        improvement_summary="Promote the Google Sheets entry repair into a reusable skill.",
        promotion_kind="approved_skill",
        approval_dependencies=["approval-skill-1"],
        record_as_approved=True,
    )

    assert result["success"] is True
    assert result["promotable"] is True
    assert result["promotion_artifact"]["artifact_kind"] == "approved_skill"
    assert result["promotion_artifact"]["approval_status"] == "linked"
    assert result["promotion_artifact"]["proposed_path"].endswith("/SKILL.md")
    assert recorded["kind"] == "approved_skill"
    assert recorded["metadata"]["artifact_kind"] == "approved_skill"
    assert recorded["metadata"]["approval_dependencies"] == ["approval-skill-1"]
    assert recorded["metadata"]["promotion_artifact"]["skill_name"].startswith("promoted/")


@pytest.mark.asyncio
async def test_package_candidate_requires_approval_dependency_for_non_memory_recording(
    git_repo,
    tmp_path,
    monkeypatch,
):
    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Promote capability patch",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )
    canary = Path(prepare["canary_path"])
    (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    async def _memory_store(*args, **kwargs):
        raise AssertionError("typed promotion should not be recorded without explicit approval")

    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)

    result = await self_improvement.self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands="printf 'ok'\n",
        improvement_summary="Promote the evidence capture path.",
        promotion_kind="capability_patch",
        record_as_approved=True,
    )

    assert result["success"] is True
    assert result["promotable"] is True
    assert result["approved_record"]["success"] is False
    assert "requires at least one approval dependency" in result["approved_record"]["error"]


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


@pytest.mark.asyncio
async def test_demo_from_failed_trajectory_runs_candidate_and_packages_diff(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="click",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={"status": "fail", "success": False},
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-demo-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "patched", "stderr": "", "return_code": 0}
        if command == "verify-demo-fix":
            content = (canary / "README.md").read_text(encoding="utf-8")
            return {
                "stdout": "verified" if "world" in content else "",
                "stderr": "" if "world" in content else "missing world",
                "return_code": 0 if "world" in content else 1,
            }
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)

    result = await self_improvement.self_improvement_demo_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_commands="apply-demo-fix",
        benchmark_commands="verify-demo-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
        auto_cleanup=True,
    )

    assert result["success"] is True
    assert result["task_id"].startswith("task_")
    assert result["goal"].startswith("Investigate failed click trajectory")
    assert result["candidate"]["success"] is True
    assert result["package"]["promotable"] is True
    assert "README.md" in result["package"]["diff_stat"]
    assert result["cleanup"]["success"] is True
    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["status"] == "completed"


@pytest.mark.asyncio
async def test_demo_from_failed_trajectory_includes_reuse_suggestions(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="click",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification=None,
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-demo-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "patched", "stderr": "", "return_code": 0}
        if command == "verify-demo-fix":
            return {"stdout": "verified", "stderr": "", "return_code": 0}
        if command.startswith("printf "):
            return {"stdout": "ok", "stderr": "", "return_code": 0}
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    async def _memory_search(query=None, tags=None, kind=None, limit=10, tool_context=None):
        assert kind == ",".join(REUSE_MEMORY_KINDS)
        assert "#save" in query
        return {
            "success": True,
            "results": [
                {
                    "id": 7,
                    "content": "Prefer a more stable selector for the save button.",
                    "kind": "approved_improvement",
                    "metadata": {"diff_stat": " README.md | 1 +"},
                    "tags": ["self-improvement", "approved"],
                    "created_at": 123.0,
                    "score": 0.91,
                }
            ],
        }

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    result = await self_improvement.self_improvement_demo_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_commands="apply-demo-fix",
        benchmark_commands="verify-demo-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    assert result["success"] is True
    assert result["reuse_query"]
    assert result["reuse_suggestions"][0]["memory_id"] == 7
    assert result["reuse_memory_ids"] == [7]
    assert result["reuse_policy"]["enabled"] is True
    assert "Approved improvement reuse hints" in result["repair_prompt"]
    assert "Prefer a more stable selector for the save button." in result["package"]["improvement_summary"]
    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["artifacts"]["reuse_suggestions"][0]["memory_id"] == 7
    assert task["artifacts"]["reuse_memory_ids"] == [7]
    assert "Approved improvement reuse hints" in task["artifacts"]["repair_prompt"]
    updated_trajectory = computer_trajectory_store.get(trajectory_id)
    assert updated_trajectory["reuse_trace"]["source"] == "self_improvement_demo"
    assert updated_trajectory["reuse_trace"]["memory_ids"] == [7]
    assert updated_trajectory["reuse_trace"]["used_memory_ids"] == [7]


@pytest.mark.asyncio
async def test_demo_rejects_non_failed_trajectory(computer_trajectory_store):
    trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="success",
        final_surface="browser",
        attempts=[],
        verification=None,
        request={"selector": "#query"},
        observation={"preferred_surface": "browser", "available_surfaces": ["browser"]},
    )

    result = await self_improvement.self_improvement_demo_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_commands="apply-demo-fix",
        benchmark_commands="verify-demo-fix",
    )

    assert result["success"] is False
    assert "must have status=failed" in result["error"]


@pytest.mark.asyncio
async def test_demo_reports_candidate_command_failure_and_cleans_up(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="click",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification=None,
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        return {"stdout": "", "stderr": "boom", "return_code": 1}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)

    result = await self_improvement.self_improvement_demo_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_commands="apply-demo-fix",
        benchmark_commands="verify-demo-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
        auto_cleanup=True,
    )

    assert result["success"] is False
    assert result["task_id"].startswith("task_")
    assert result["candidate"]["success"] is False
    assert result["cleanup"]["success"] is True
    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["status"] == "failed"


def test_cli_self_improvement_demo_invokes_tool(monkeypatch):
    async def _demo(**kwargs):
        assert kwargs["trajectory_id"] == 7
        assert kwargs["candidate_commands"] == "apply-demo-fix"
        assert kwargs["benchmark_commands"] == "verify-demo-fix"
        return {"success": True, "trajectory": {"id": 7}}

    monkeypatch.setattr(self_improvement, "self_improvement_demo_from_trajectory", _demo)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "self-improvement-demo",
            "--trajectory-id",
            "7",
            "--candidate-command",
            "apply-demo-fix",
            "--benchmark-command",
            "verify-demo-fix",
        ],
    )

    assert result.exit_code == 0
    assert '"id": 7' in result.output


@pytest.mark.asyncio
async def test_search_from_failed_trajectory_selects_smallest_promotable_candidate(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="click",
        status="failed",
        final_surface="current_tab",
        attempts=[{"surface": "current_tab", "strategy": "current_tab_selector"}],
        verification={"status": "fail", "success": False},
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-small-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "small", "stderr": "", "return_code": 0}
        if command == "apply-large-fix":
            (canary / "README.md").write_text("hello\nworld\nextra\nextra\n", encoding="utf-8")
            return {"stdout": "large", "stderr": "", "return_code": 0}
        if command == "verify-search-fix":
            content = (canary / "README.md").read_text(encoding="utf-8")
            return {
                "stdout": "verified" if "world" in content else "",
                "stderr": "" if "world" in content else "missing world",
                "return_code": 0 if "world" in content else 1,
            }
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)

    result = await self_improvement.self_improvement_search_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_specs_json=json.dumps(
            [
                {"name": "small-fix", "commands": ["apply-small-fix"]},
                {"name": "large-fix", "commands": ["apply-large-fix"]},
            ]
        ),
        benchmark_commands="verify-search-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
        cleanup_losers=True,
        auto_cleanup=True,
    )

    assert result["success"] is True
    assert result["task_id"].startswith("task_")
    assert result["winner_name"] == "small-fix"
    assert result["winner"]["package"]["promotable"] is True
    assert result["winner"]["cleanup"]["success"] is True
    assert result["winner"]["task_id"].startswith("task_")
    parent_task = get_task_store().get(result["task_id"])
    assert parent_task is not None
    assert parent_task["winner_task_id"] == result["winner"]["task_id"]
    losing = next(candidate for candidate in result["candidates"] if candidate["name"] == "large-fix")
    assert losing["task_id"].startswith("task_")
    assert losing["cleanup"]["success"] is True


@pytest.mark.asyncio
async def test_search_reports_failure_when_no_candidate_is_promotable(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="failed",
        final_surface="browser",
        attempts=[],
        verification=None,
        request={"selector": "#query"},
        observation={"preferred_surface": "browser", "available_surfaces": ["browser"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-broken-fix":
            (canary / "README.md").write_text("hello\n", encoding="utf-8")
            return {"stdout": "broken", "stderr": "", "return_code": 0}
        if command == "verify-search-fix":
            return {"stdout": "", "stderr": "missing world", "return_code": 1}
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)

    result = await self_improvement.self_improvement_search_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_specs_json=json.dumps(
            [{"name": "broken-fix", "commands": ["apply-broken-fix"]}]
        ),
        benchmark_commands="verify-search-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
        cleanup_losers=True,
    )

    assert result["success"] is False
    assert result["task_id"].startswith("task_")
    assert result["error"] == "No promotable candidate found"
    assert result["winner_name"] == "broken-fix"
    assert result["winner"]["package"]["promotable"] is False
    assert result["winner"]["task_id"].startswith("task_")
    assert result["winner"]["cleanup"]["success"] is True
    parent_task = get_task_store().get(result["task_id"])
    assert parent_task is not None
    assert parent_task["status"] == "failed"


@pytest.mark.asyncio
async def test_search_from_failed_trajectory_includes_reuse_suggestions(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="failed",
        final_surface="browser",
        attempts=[],
        verification=None,
        request={"selector": "#query"},
        observation={"preferred_surface": "browser", "available_surfaces": ["browser"]},
    )

    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "patched", "stderr": "", "return_code": 0}
        if command == "verify-search-fix":
            return {"stdout": "verified", "stderr": "", "return_code": 0}
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    async def _memory_search(query=None, tags=None, kind=None, limit=10, tool_context=None):
        assert kind == ",".join(REUSE_MEMORY_KINDS)
        return {
            "success": True,
            "results": [
                {
                    "id": 11,
                    "content": "Reuse the browser selector normalization fix.",
                    "kind": "approved_skill",
                    "metadata": {"branch": "canary/browser-fix"},
                    "tags": ["approved"],
                    "created_at": 456.0,
                    "score": 0.88,
                }
            ],
        }

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    result = await self_improvement.self_improvement_search_from_trajectory(
        trajectory_id=trajectory_id,
        candidate_specs_json=json.dumps([{"name": "fix", "commands": ["apply-fix"]}]),
        benchmark_commands="verify-search-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    assert result["success"] is True
    assert result["reuse_suggestions"][0]["memory_id"] == 11
    assert result["reuse_memory_ids"] == [11]
    assert "Approved improvement reuse hints" in result["repair_prompt"]
    assert "Reuse the browser selector normalization fix." in result["winner"]["package"]["improvement_summary"]
    assert "Approved improvement reuse hints" in result["winner"]["candidate_generation_prompt"]
    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["artifacts"]["reuse_suggestions"][0]["memory_id"] == 11
    assert task["artifacts"]["reuse_memory_ids"] == [11]
    assert "Approved improvement reuse hints" in task["artifacts"]["repair_prompt"]


@pytest.mark.asyncio
async def test_second_similar_failure_reuses_registered_approved_skill_end_to_end(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
    _isolated_promotions,
):
    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-demo-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "patched", "stderr": "", "return_code": 0}
        if command == "verify-demo-fix":
            return {"stdout": "verified", "stderr": "", "return_code": 0}
        if command.startswith("printf "):
            return {"stdout": "ok", "stderr": "", "return_code": 0}
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    async def _memory_store(content, tags=None, metadata=None, kind="fact", tool_context=None):
        memory_id = _isolated_promotions.store(
            content=content,
            kind=kind,
            tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
            metadata=json.loads(metadata or "{}"),
        )
        return {"success": True, "memory_id": memory_id, "kind": kind}

    async def _memory_search(query=None, tags=None, kind=None, limit=10, tool_context=None):
        return {"success": False, "error": "semantic search unavailable"}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)
    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    prepare = await self_improvement.self_improvement_prepare_canary(
        goal="Promote a Sheets repair",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )
    canary = Path(prepare["canary_path"])
    (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    self_improvement._persist_state(
        canary,
        demo={
            "trajectory_id": 1,
            "goal": "Enter into Sheets",
            "failure_reason": "verification fail",
            "reuse_hints": {
                "trajectory_key": "fill::current_tab::a1",
                "selector": "A1",
                "surface": "current_tab",
                "target": "google-sheets-entry",
            },
        },
    )

    first = await self_improvement.self_improvement_package_candidate(
        canary_path=str(canary),
        benchmark_commands="printf 'ok'\n",
        improvement_summary="Promote the Google Sheets entry repair into a reusable skill.",
        promotion_kind="approved_skill",
        approval_dependencies=["approval-skill-1"],
        record_as_approved=True,
    )
    assert first["success"] is True
    assert first["approved_record"]["success"] is True

    second_trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification={"status": "fail", "success": False},
        request={"selector": "A1"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    second = await self_improvement.self_improvement_demo_from_trajectory(
        trajectory_id=second_trajectory_id,
        candidate_commands="apply-demo-fix",
        benchmark_commands="verify-demo-fix",
        repo_path=str(git_repo),
        worktree_root=str(tmp_path / "canaries"),
    )

    assert second["success"] is True
    assert second["reuse_suggestions"][0]["kind"] == "approved_skill"
    assert second["reuse_suggestions"][0]["runtime_registration"]["kind"] == "skill"
    assert second["reuse_suggestions"][0]["runtime_registration"]["registered"] is True
    assert second["reuse_suggestions"][0]["deployment_gate"]["deployable"] is True
    assert second["reuse_query"]
    assert "Registered approved reuse content:" in second["repair_prompt"]
    assert "promoted/current_tab_google_sheets_entry" not in second["repair_prompt"]
    assert "# promoted/current-tab-google-sheets-entry" in second["repair_prompt"]


@pytest.mark.asyncio
async def test_reuse_prefilter_avoids_semantic_search_when_metadata_matches(
    computer_trajectory_store,
    monkeypatch,
):
    trajectory_id = computer_trajectory_store.record(
        action="click",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification=None,
        request={"selector": "#save"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )
    trajectory = computer_trajectory_store.get(trajectory_id)
    assert trajectory is not None

    class _FakeMemoryStore:
        def search(self, query=None, tags=None, kinds=None, limit=10, embedding=None):
            assert query is None
            assert kinds == list(REUSE_MEMORY_KINDS)
            return [
                {
                    "id": 19,
                    "content": "Reuse the current-tab save selector repair.",
                    "kind": "approved_skill",
                    "metadata": {
                        "trajectory_key": "click::current_tab::#save",
                        "selector": "#save",
                        "action": "click",
                        "surface": "current_tab",
                        "diff_stat": " README.md | 1 +",
                    },
                    "tags": ["self-improvement", "approved"],
                    "created_at": 99.0,
                }
            ]

    async def _memory_search(**kwargs):
        raise AssertionError("semantic memory search should not run when prefilter is enough")

    monkeypatch.setattr(self_improvement, "get_memory_store", lambda: _FakeMemoryStore())
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    result = await self_improvement._find_reuse_suggestions(trajectory, limit=1)

    assert result["results"][0]["memory_id"] == 19
    assert result["results"][0]["match_type"] == "prefilter"
    assert result["memory_ids"] == [19]


@pytest.mark.asyncio
async def test_find_reuse_suggestions_can_be_disabled_by_policy(monkeypatch):
    trajectory = {
        "id": 11,
        "action": "click",
        "status": "failed",
        "final_surface": "current_tab",
        "request": {
            "selector": "#save",
            "policy": {"allow_approved_improvement_reuse": False},
        },
        "observation": {"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    }

    class _FakeMemoryStore:
        def search(self, **kwargs):
            raise AssertionError("prefilter search should not run when reuse is disabled by policy")

    async def _memory_search(**kwargs):
        raise AssertionError("semantic memory search should not run when reuse is disabled by policy")

    monkeypatch.setattr(self_improvement, "get_memory_store", lambda: _FakeMemoryStore())
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    result = await self_improvement._find_reuse_suggestions(trajectory, limit=3)

    assert result["results"] == []
    assert result["memory_ids"] == []
    assert result["policy"]["enabled"] is False
    assert result["policy"]["source"] == "request.policy.allow_approved_improvement_reuse"


def test_cli_self_improvement_search_invokes_tool(monkeypatch):
    async def _search(**kwargs):
        assert kwargs["trajectory_id"] == 9
        specs = json.loads(kwargs["candidate_specs_json"])
        assert [spec["name"] for spec in specs] == ["small-fix", "large-fix"]
        assert kwargs["benchmark_commands"] == "verify-search-fix"
        return {"success": True, "winner_name": "small-fix"}

    monkeypatch.setattr(self_improvement, "self_improvement_search_from_trajectory", _search)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "self-improvement-search",
            "--trajectory-id",
            "9",
            "--candidate-spec",
            '{"name":"small-fix","commands":["apply-small-fix"]}',
            "--candidate-spec",
            '{"name":"large-fix","commands":["apply-large-fix"]}',
            "--benchmark-command",
            "verify-search-fix",
        ],
    )

    assert result.exit_code == 0
    assert '"winner_name": "small-fix"' in result.output


def test_cli_self_improvement_demo_accepts_promotion_kind_and_approval_dependencies(monkeypatch):
    async def _demo(**kwargs):
        assert kwargs["promotion_kind"] == "approved_skill"
        assert kwargs["approval_dependencies"] == ["approval-skill-1"]
        return {"success": True, "trajectory": {"id": 7}}

    monkeypatch.setattr(self_improvement, "self_improvement_demo_from_trajectory", _demo)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "self-improvement-demo",
            "--trajectory-id",
            "7",
            "--candidate-command",
            "apply-demo-fix",
            "--benchmark-command",
            "verify-demo-fix",
            "--promotion-kind",
            "approved_skill",
            "--approval-dependency",
            "approval-skill-1",
        ],
    )

    assert result.exit_code == 0


def test_cli_self_improvement_demo_end_to_end_reuses_recorded_approved_skill(
    git_repo,
    tmp_path,
    computer_trajectory_store,
    monkeypatch,
    _isolated_promotions,
    _reset_skill_runtime,
):
    async def _run_shell_guarded(command, timeout=30, cwd=None, tool_context=None):
        canary = Path(cwd)
        if command == "apply-demo-fix":
            (canary / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            return {"stdout": "patched", "stderr": "", "return_code": 0}
        if command == "verify-demo-fix":
            return {"stdout": "verified", "stderr": "", "return_code": 0}
        return {"stdout": "", "stderr": f"unknown command {command}", "return_code": 1}

    async def _memory_store(content, tags=None, metadata=None, kind="fact", tool_context=None):
        memory_id = _isolated_promotions.store(
            content=content,
            kind=kind,
            tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
            metadata=json.loads(metadata or "{}"),
        )
        return {"success": True, "memory_id": memory_id, "kind": kind}

    async def _memory_search(query=None, tags=None, kind=None, limit=10, tool_context=None):
        kinds = [item.strip() for item in (kind or "").split(",") if item.strip()] or None
        results = _isolated_promotions.search(query=query, kinds=kinds, limit=limit)
        return {"success": True, "results": results, "count": len(results), "query": query}

    monkeypatch.setattr(self_improvement, "run_shell_guarded", _run_shell_guarded)
    monkeypatch.setattr(self_improvement, "memory_store", _memory_store)
    monkeypatch.setattr(self_improvement, "memory_search", _memory_search)

    first_trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification={"status": "fail", "success": False},
        request={"selector": "A1"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "self-improvement-demo",
            "--trajectory-id",
            str(first_trajectory_id),
            "--candidate-command",
            "apply-demo-fix",
            "--benchmark-command",
            "verify-demo-fix",
            "--repo-path",
            str(git_repo),
            "--worktree-root",
            str(tmp_path / "canaries"),
            "--promotion-kind",
            "approved_skill",
            "--approval-dependency",
            "approval-skill-1",
            "--record-as-approved",
        ],
    )

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["success"] is True
    assert first_payload["package"]["approved_record"]["success"] is True
    skill_name = first_payload["package"]["promotion_artifact"]["skill_name"]

    resources = asyncio.run(resource_list())
    assert any(item["id"] == f"skill:{skill_name}" for item in resources["resources"])

    second_trajectory_id = computer_trajectory_store.record(
        action="fill",
        status="failed",
        final_surface="current_tab",
        attempts=[],
        verification={"status": "fail", "success": False},
        request={"selector": "A1"},
        observation={"preferred_surface": "current_tab", "available_surfaces": ["current_tab"]},
    )

    second = runner.invoke(
        cli,
        [
            "self-improvement-demo",
            "--trajectory-id",
            str(second_trajectory_id),
            "--candidate-command",
            "apply-demo-fix",
            "--benchmark-command",
            "verify-demo-fix",
            "--repo-path",
            str(git_repo),
            "--worktree-root",
            str(tmp_path / "canaries"),
        ],
    )

    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["success"] is True
    assert second_payload["reuse_suggestions"][0]["kind"] == "approved_skill"
    assert second_payload["reuse_suggestions"][0]["runtime_registration"]["name"] == skill_name
    assert second_payload["reuse_suggestions"][0]["runtime_registration"]["registered"] is True
    assert second_payload["reuse_suggestions"][0]["deployment_gate"]["deployable"] is True
    assert "Registered approved reuse content:" in second_payload["repair_prompt"]
    assert f"# {skill_name}" in second_payload["repair_prompt"]


def test_cli_self_improvement_search_accepts_promotion_kind_and_approval_dependencies(monkeypatch):
    async def _search(**kwargs):
        assert kwargs["promotion_kind"] == "policy_patch"
        assert kwargs["approval_dependencies"] == ["approval-policy-1"]
        return {"success": True, "winner_name": "small-fix"}

    monkeypatch.setattr(self_improvement, "self_improvement_search_from_trajectory", _search)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "self-improvement-search",
            "--trajectory-id",
            "9",
            "--candidate-spec",
            '{"name":"small-fix","commands":["apply-small-fix"]}',
            "--benchmark-command",
            "verify-search-fix",
            "--promotion-kind",
            "policy_patch",
            "--approval-dependency",
            "approval-policy-1",
        ],
    )

    assert result.exit_code == 0
