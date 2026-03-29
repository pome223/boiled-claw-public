import json

import pytest

from src.tools import physical_ai


@pytest.fixture(autouse=True)
def reset_validation_runs():
    physical_ai.reset_physical_ai_validation_runs()
    yield
    physical_ai.reset_physical_ai_validation_runs()


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
            },
        )(),
    )

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

    physical_ai._validation_runs["run-2"] = {
        "run_id": "run-2",
        "validated": False,
        "adapter": "osmo",
    }
    invalid = await physical_ai.physical_ai_dispatch_ros2_action(
        validation_run_id="run-2",
        ros2_action_json=json.dumps({"action_name": "foo"}),
    )
    assert invalid["success"] is False
    assert "Simulation-first validation has not passed" in invalid["error"]


@pytest.mark.asyncio
async def test_dispatch_allows_real_hardware_only_after_validated_run(monkeypatch):
    physical_ai._validation_runs["run-3"] = {
        "run_id": "run-3",
        "validated": True,
        "adapter": "isaac_sim",
        "status": "validated",
    }

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
            },
        )(),
    )

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
