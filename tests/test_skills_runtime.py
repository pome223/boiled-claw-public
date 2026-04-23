import pytest

import src.runtime.capability_registry as capability_registry
import src.runtime.promoted_capabilities as promoted_capabilities
import src.skills.runtime as runtime
import src.skills.promoted as promoted_skills
from src.skills.base import get_skill_registry
from src.bridges.common_schema import CapabilityDescriptor, CapabilityListResult
from src.tools.memory import MemoryStore
from src.tools.skills import capability_invoke, capability_list, resource_list, resource_read, skill_execute


@pytest.fixture
def reset_skills_runtime(monkeypatch, tmp_path):
    registry = get_skill_registry()
    original_skills = dict(registry.skills)
    original_loaded = runtime._loaded
    original_report = dict(runtime._last_report)
    store = MemoryStore(str(tmp_path / "memory.db"))

    registry.skills.clear()
    runtime._loaded = False
    runtime._last_report = {"loaded": False, "count": 0, "skills": []}
    monkeypatch.setattr(promoted_skills, "get_memory_store", lambda: store)
    monkeypatch.setattr(promoted_capabilities, "get_memory_store", lambda: store)

    yield registry, store

    registry.skills.clear()
    registry.skills.update(original_skills)
    runtime._loaded = original_loaded
    runtime._last_report = original_report


@pytest.mark.asyncio
async def test_ensure_skills_loaded_registers_computer_use_skill(reset_skills_runtime):
    registry, _store = reset_skills_runtime

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


@pytest.mark.asyncio
async def test_promoted_approved_skill_is_registered_and_prioritized(reset_skills_runtime):
    _registry, store = reset_skills_runtime
    store.store(
        content="Promoted Sheets repair",
        kind="approved_skill",
        metadata={
            "trajectory_key": "fill::current_tab::a1",
            "selector": "A1",
            "surface": "current_tab",
            "approval_dependencies": ["approval-skill-1"],
            "promotion_artifact": {
                "artifact_kind": "approved_skill",
                "approval_required": True,
                "approval_status": "linked",
                "approval_dependencies": ["approval-skill-1"],
                "benchmark_step_count": 1,
                "proposed_path": "skills/promoted/current-tab-a1/SKILL.md",
                "skill_name": "promoted/current-tab-a1",
                "surface": "current_tab",
                "target": "a1",
                "content_preview": "# promoted/current-tab-a1\n\nReuse this approved Sheets repair path.",
            },
        },
    )

    listing = await capability_list()
    skills = await skill_execute("promoted/current-tab-a1")

    assert listing["success"] if "success" in listing else True
    assert skills["ok"] is True
    assert "Reuse this approved Sheets repair path." in skills["result"]["content"]

    resource_listing = await resource_list()
    promoted_index = next(
        index
        for index, item in enumerate(resource_listing["resources"])
        if item["id"] == "skill:promoted/current-tab-a1"
    )
    builtin_index = next(
        index
        for index, item in enumerate(resource_listing["resources"])
        if item["id"] == "skill:computer-use"
    )
    assert promoted_index < builtin_index


@pytest.mark.asyncio
async def test_promoted_capability_patch_is_registered_and_invokable(reset_skills_runtime):
    _registry, store = reset_skills_runtime
    store.store(
        content="Promoted capability patch",
        kind="capability_patch",
        metadata={
            "trajectory_key": "click::current_tab::#save",
            "selector": "#save",
            "surface": "current_tab",
            "approval_dependencies": ["approval-cap-1"],
            "promotion_artifact": {
                "artifact_kind": "capability_patch",
                "approval_required": True,
                "approval_status": "linked",
                "approval_dependencies": ["approval-cap-1"],
                "benchmark_step_count": 1,
                "proposed_path": "src/runtime/promoted_capabilities/current-tab-save.json",
                "capability_name": "promoted.current_tab.current_tab_save",
                "surface": "current_tab",
                "target": "#save",
                "content_preview": (
                    '{\n'
                    '  "name": "promoted.current_tab.current_tab_save",\n'
                    '  "surface": "current_tab",\n'
                    '  "target": "#save",\n'
                    '  "summary": "Prefer the approved save repair path."\n'
                    '}'
                ),
            },
        },
    )

    listing = await capability_list()
    names = {item["name"] for item in listing["capabilities"]}
    assert "promoted.current_tab.current_tab_save" in names

    invoke = await capability_invoke("promoted.current_tab.current_tab_save", '{"attempt":1}')
    assert invoke["success"] is True
    assert invoke["result"]["kind"] == "promoted_capability_patch"
    assert invoke["result"]["preview"]["target"] == "#save"


@pytest.mark.asyncio
async def test_unapproved_typed_promotions_are_not_registered(reset_skills_runtime):
    _registry, store = reset_skills_runtime
    store.store(
        content="Pending promoted skill",
        kind="approved_skill",
        metadata={
            "promotion_artifact": {
                "artifact_kind": "approved_skill",
                "approval_required": True,
                "approval_status": "pending",
                "approval_dependencies": [],
                "benchmark_step_count": 1,
                "proposed_path": "skills/promoted/pending/SKILL.md",
                "skill_name": "promoted/pending",
                "surface": "current_tab",
                "target": "a1",
                "content_preview": "# promoted/pending",
            },
        },
    )
    store.store(
        content="Pending capability patch",
        kind="capability_patch",
        metadata={
            "promotion_artifact": {
                "artifact_kind": "capability_patch",
                "approval_required": True,
                "approval_status": "pending",
                "approval_dependencies": [],
                "benchmark_step_count": 1,
                "proposed_path": "src/runtime/promoted_capabilities/pending.json",
                "capability_name": "promoted.current_tab.pending",
                "surface": "current_tab",
                "target": "#save",
                "content_preview": '{"name":"promoted.current_tab.pending"}',
            },
        },
    )

    resources = await resource_list()
    capabilities = await capability_list()
    names = {item["id"] for item in resources["resources"]}
    capability_names = {item["name"] for item in capabilities["capabilities"]}

    assert "skill:promoted/pending" not in names
    assert "promoted.current_tab.pending" not in capability_names
