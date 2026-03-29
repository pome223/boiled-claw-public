import json

import pytest

from src.physical_ai import validation_store as validation_store_module
from src.physical_ai.validation_store import reset_physical_ai_validation_store
from src.tools import physical_ai


@pytest.fixture(autouse=True)
def reset_validation_runs():
    reset_physical_ai_validation_store()
    physical_ai.reset_physical_ai_validation_runs()
    yield
    physical_ai.reset_physical_ai_validation_runs()
    reset_physical_ai_validation_store()


@pytest.mark.asyncio
async def test_submit_simulation_records_validated_run(monkeypatch):
    async def _post(url, payload):
        assert payload["validation_mode"] == "simulation_first"
        return {"run_id": "run-1", "status": "validated", "validated": True}

    monkeypatch.setattr(physical_ai, "_post_adapter_json", _post)
    monkeypatch.setattr(
        physical_ai,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "physical_ai_isaac_sim_url": "http://isaac.local/sim",
                "physical_ai_osmo_url": "http://osmo.local/workflows",
                "physical_ai_ros2_bridge_url": "http://ros2.local/dispatch",
                "physical_ai_timeout_seconds": 5,
                "physical_ai_validation_db_path": None,
            },
        )(),
    )
    monkeypatch.setattr(validation_store_module, "get_settings", physical_ai.get_settings)

    result = await physical_ai.physical_ai_submit_simulation(
        adapter="isaac_sim",
        workflow="pick-and-place",
        scenario="warehouse_a",
        robot="ur10",
    )

    assert result["success"] is True
    assert result["validated"] is True
    assert result["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_submit_simulation_does_not_trust_ready_status(monkeypatch):
    async def _post(url, payload):
        return {"run_id": "run-ready", "status": "ready"}

    monkeypatch.setattr(physical_ai, "_post_adapter_json", _post)
    monkeypatch.setattr(
        physical_ai,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "physical_ai_isaac_sim_url": "http://isaac.local/sim",
                "physical_ai_osmo_url": "http://osmo.local/workflows",
                "physical_ai_ros2_bridge_url": "http://ros2.local/dispatch",
                "physical_ai_timeout_seconds": 5,
                "physical_ai_validation_db_path": None,
            },
        )(),
    )
    monkeypatch.setattr(validation_store_module, "get_settings", physical_ai.get_settings)

    result = await physical_ai.physical_ai_submit_simulation(
        adapter="isaac_sim",
        workflow="pick-and-place",
        scenario="warehouse_a",
    )

    assert result["success"] is True
    assert result["validated"] is False


@pytest.mark.asyncio
async def test_build_ros2_action_returns_action_topics():
    result = await physical_ai.physical_ai_build_ros2_action(
        robot_namespace="robot_1",
        action_name="follow_joint_trajectory",
        action_type="control_msgs/action/FollowJointTrajectory",
        goal_json=json.dumps({"joints": ["joint_a"]}),
        frame_id="base_link",
    )

    assert result["success"] is True
    assert result["ros2_action"]["topics"]["send_goal"].endswith("/_action/send_goal")
    assert result["simulation_first_required"] is True


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_or_unvalidated_runs():
    unknown = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="missing",
        ros2_action_json=json.dumps({"action_name": "foo"}),
    )
    assert unknown["success"] is False

    physical_ai._record_validation_run(
        {
        "run_id": "run-2",
        "validated": False,
        "adapter": "osmo",
        "status": "queued",
        "response": {"status": "queued"},
        "created_at": 0.0,
    }
    )
    invalid = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="run-2",
        ros2_action_json=json.dumps({"action_name": "foo"}),
    )
    assert invalid["success"] is False
    assert "Simulation-first validation has not passed" in invalid["error"]


@pytest.mark.asyncio
async def test_dispatch_allows_real_hardware_only_after_validated_run(monkeypatch):
    physical_ai._record_validation_run(
        {
        "run_id": "run-3",
        "validated": True,
        "adapter": "isaac_sim",
        "status": "validated",
        "response": {"status": "validated", "validated": True},
        "created_at": 0.0,
    }
    )

    async def _post(url, payload):
        assert payload["validation_run_id"] == "run-3"
        return {"dispatch_id": "dispatch-1", "status": "accepted"}

    monkeypatch.setattr(physical_ai, "_post_adapter_json", _post)
    monkeypatch.setattr(
        physical_ai,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "physical_ai_isaac_sim_url": "http://isaac.local/sim",
                "physical_ai_osmo_url": "http://osmo.local/workflows",
                "physical_ai_ros2_bridge_url": "http://ros2.local/dispatch",
                "physical_ai_timeout_seconds": 5,
                "physical_ai_validation_db_path": None,
            },
        )(),
    )
    monkeypatch.setattr(validation_store_module, "get_settings", physical_ai.get_settings)

    dry_run = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="run-3",
        ros2_action_json=json.dumps({"namespace": "robot_1", "action_name": "foo"}),
    )
    assert dry_run["success"] is True
    assert dry_run["dry_run"] is True

    dispatched = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="run-3",
        ros2_action_json=json.dumps({"namespace": "robot_1", "action_name": "foo"}),
        allow_real_hardware=True,
        dry_run=False,
    )
    assert dispatched["success"] is True
    assert dispatched["dispatched"] is True


@pytest.mark.asyncio
async def test_dispatch_can_refetch_validated_run_after_store_reload(monkeypatch, tmp_path):
    async def _post(url, payload):
        if url.endswith("/sim"):
            return {"run_id": "run-4", "status": "validated", "validated": True}
        return {"dispatch_id": "dispatch-4", "status": "accepted"}

    settings = type(
        "Settings",
        (),
        {
            "physical_ai_isaac_sim_url": "http://isaac.local/sim",
            "physical_ai_osmo_url": "http://osmo.local/workflows",
            "physical_ai_ros2_bridge_url": "http://ros2.local/dispatch",
            "physical_ai_timeout_seconds": 5,
            "physical_ai_validation_db_path": tmp_path / "physical_ai_validation.db",
        },
    )()
    monkeypatch.setattr(physical_ai, "_post_adapter_json", _post)
    monkeypatch.setattr(physical_ai, "get_settings", lambda: settings)
    monkeypatch.setattr(validation_store_module, "get_settings", physical_ai.get_settings)

    submitted = await physical_ai.physical_ai_submit_simulation(
        adapter="isaac_sim",
        workflow="pick-and-place",
        scenario="warehouse_a",
    )
    assert submitted["validated"] is True

    reset_physical_ai_validation_store()

    dispatched = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="run-4",
        ros2_action_json=json.dumps({"namespace": "robot_1", "action_name": "foo"}),
        allow_real_hardware=True,
        dry_run=False,
    )
    assert dispatched["success"] is True
    assert dispatched["dispatched"] is True
