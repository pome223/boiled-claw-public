"""Simulator adapter contract — design slice (#171).

This module defines ``simulator_adapter_contract.v1``, the static description
each simulator adapter publishes about itself. It is intentionally **artifact
only**: this slice does not route any runtime through an adapter interface,
does not connect to PX4 SITL / Gazebo / AirSim / Isaac Sim, and does not open
any path to live or physical execution.

What the contract is for
------------------------

When we add a second simulator (or a hardware-in-the-loop telemetry stream),
we want a single artifact that declares:

- which schema versions the adapter speaks (state / action / telemetry /
  governor / episode / replay trace)
- whether the adapter supports stronger execution modes (it must not, in
  this slice — the booleans are pinned to ``False`` at the type level)
- whether operator approval is required (always ``True``)
- the adapter's high-level mode (``dry_run_only`` for now)

Future PRs can:

- have ``run_toy_grid_world_autonomous_episode`` consult the contract before
  consenting to step the simulator
- gate ``autonomy_gate_result`` / ``autonomy_gate_comparison_result`` on the
  contract advertising the same simulator the artifacts came from
- introduce ``hardware_in_the_loop_telemetry_only`` / ``simulated_only`` /
  ``limited_live_execution`` modes by adding new ``SimulatorAdapterMode``
  values, with their own ``Literal`` invariants

Out of scope for this PR
------------------------

- Routing toy-grid runtime through the contract
- Adding a second adapter
- Live / physical / ROS dispatch paths
- Mission API / promotion / runtime reuse
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION = "simulator_adapter_contract.v1"


class SimulatorAdapterMode(str, Enum):
    """The execution mode an adapter advertises.

    Only ``dry_run_only`` exists today. Stronger modes (HIL telemetry-only,
    limited live execution) are deliberately not added until #172 / #173 land
    with their own approval and policy story.
    """

    DRY_RUN_ONLY = "dry_run_only"


class SimulatorAdapterContract(BaseModel):
    """Static description of a single simulator adapter.

    The boolean capability fields are pinned to ``False`` via ``Literal`` so
    Pydantic refuses to construct a contract that advertises live, physical,
    or ROS-dispatch capabilities through this slice. Any future adapter that
    needs those capabilities must come with its own contract version (v2+)
    and an explicit approval / policy story.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION] = (
        SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION
    )
    adapter_id: str
    simulator_kind: str
    state_schema: str
    action_schema: str
    telemetry_schema: str
    governor_schema: str
    episode_schema: str
    replay_trace_schema: str
    supports_live_execution: Literal[False] = False
    supports_physical_execution: Literal[False] = False
    supports_ros_dispatch: Literal[False] = False
    operator_approval_required: Literal[True] = True
    adapter_mode: Literal[SimulatorAdapterMode.DRY_RUN_ONLY] = (
        SimulatorAdapterMode.DRY_RUN_ONLY
    )


class SimulatorAdapterContractError(ValueError):
    """Raised when a simulator adapter contract fails validation against the
    expected adapter for the runtime path being entered.
    """


__all__ = [
    "SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION",
    "SimulatorAdapterContract",
    "SimulatorAdapterContractError",
    "SimulatorAdapterMode",
]
