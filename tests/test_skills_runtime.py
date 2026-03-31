import pytest

import src.runtime.capability_registry as capability_registry
import src.skills.runtime as runtime
from src.skills.base import get_skill_registry
from src.bridges.common_schema import CapabilityDescriptor, CapabilityListResult
from src.tools.skills import capability_invoke, capability_list, resource_list, resource_read, skill_execute


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


@pytest.mark.asyncio
async def test_resource_list_exposes_bridge_and_skill_resources(reset_skills_runtime):
    result = await resource_list()

    assert any(item["id"] == "bridge:host" for item in result["resources"])
    assert any(item["id"] == "bridge:desktop" for item in result["resources"])
    assert any(item["id"] == "bridge:current_tab" for item in result["resources"])
    assert any(item["id"] == "skill:computer-use" for item in result["resources"])


@pytest.mark.asyncio
async def test_resource_read_returns_skill_content(reset_skills_runtime):
    result = await resource_read("skill:computer-use")

    assert result["ok"] is True
    assert result["resource"]["id"] == "skill:computer-use"
    assert "computer_observe" in result["resource"]["content"]


@pytest.mark.asyncio
async def test_capability_list_includes_runtime_registry_surfaces(reset_skills_runtime):
    result = await capability_list()
    names = {item["name"] for item in result["capabilities"]}

    assert "skill.list" in names
    assert "skill.execute" in names
    assert "browser.navigate" in names
    assert "current_tab.info" in names
    assert "desktop.view.windows" in names
    assert "shell.run" in names
    assert "file.list" in names


@pytest.mark.asyncio
async def test_capability_invoke_executes_skill_capability(reset_skills_runtime):
    result = await capability_invoke(
        "skill.execute",
        '{"name":"computer-use","params":{"task":"Inspect the browser-first stack"}}',
    )

    assert result["success"] is True
    assert result["capability"] == "skill.execute"
    assert "computer_observe" in result["result"]["result"]["content"]


@pytest.mark.asyncio
async def test_capability_invoke_dispatches_browser_capability(monkeypatch, reset_skills_runtime):
    async def _fake_browser_navigate(**kwargs):
        return {"success": True, "url": kwargs["url"], "visible": kwargs["visible"]}

    monkeypatch.setitem(
        capability_registry._CAPABILITY_SPECS,
        "browser.navigate",
        capability_registry.RuntimeCapabilitySpec(
            name="browser.navigate",
            provider="browser",
            description="Navigate a browser page through the configured browser runtime.",
            risk="medium",
            requires_approval=False,
            transport="host_bridge_or_local",
            bridge_capability="host.browser.navigate",
            invoker=_fake_browser_navigate,
        ),
    )

    result = await capability_invoke(
        "browser.navigate",
        '{"url":"https://example.com","visible":true}',
    )

    assert result["success"] is True
    assert result["result"]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_capability_invoke_rejects_unknown_capability(reset_skills_runtime):
    result = await capability_invoke("unknown.capability")

    assert result["success"] is False
    assert "Unknown capability" in result["error"]


@pytest.mark.asyncio
async def test_capability_invoke_requires_tool_context_for_approval_capability(reset_skills_runtime):
    result = await capability_invoke("shell.run", '{"command":"pwd"}')

    assert result["success"] is False
    assert "requires tool_context-backed approval flow" in result["error"]


@pytest.mark.asyncio
async def test_capability_list_refresh_uses_desktop_client_probe(monkeypatch, reset_skills_runtime):
    class _FakeDesktopClient:
        async def capabilities(self):
            return CapabilityListResult(
                capabilities=[
                    CapabilityDescriptor(
                        name="desktop.view.windows",
                        risk="low",
                        requires_approval=True,
                        description="List visible windows on the host OS.",
                        implemented=False,
                    ),
                    CapabilityDescriptor(
                        name="desktop.control.click",
                        risk="high",
                        requires_approval=True,
                        description="Click on the host desktop or a matched accessibility element.",
                        implemented=True,
                    ),
                ]
            )

    monkeypatch.setattr(capability_registry, "get_desktop_client", lambda: _FakeDesktopClient())

    result = await capability_list(refresh=True)
    capability_map = {item["name"]: item for item in result["capabilities"]}

    assert capability_map["desktop.view.windows"]["implemented"] is False
    assert capability_map["desktop.control.click"]["implemented"] is True
