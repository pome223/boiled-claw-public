import pytest

import src.skills.runtime as runtime
from src.skills.base import get_skill_registry
from src.tools.skills import skill_execute


@pytest.fixture
def reset_skills_runtime():
    registry = get_skill_registry()
    original_skills = dict(registry.skills)
    original_loaded = runtime._loaded
    original_report = dict(runtime._last_report)

    registry.skills.clear()
    runtime._loaded = False
    runtime._last_report = {"loaded": False, "count": 0, "skills": []}

    yield registry

    registry.skills.clear()
    registry.skills.update(original_skills)
    runtime._loaded = original_loaded
    runtime._last_report = original_report


@pytest.mark.asyncio
async def test_ensure_skills_loaded_registers_computer_use_skill(reset_skills_runtime):
    registry = reset_skills_runtime

    report = await runtime.ensure_skills_loaded("skills")

    assert report["loaded"] is True
    assert "computer-use" in report["skills"]
    skill = registry.get_skill("computer-use")
    assert skill is not None


@pytest.mark.asyncio
async def test_skill_execute_returns_computer_use_instructions(reset_skills_runtime):
    result = await skill_execute("computer-use")

    assert result["ok"] is True
    content = result["result"]["content"]
    assert "computer_observe" in content
    assert "computer_click" in content
    assert "computer_fill" in content
    assert 'agent_id="computer_operator"' in content
