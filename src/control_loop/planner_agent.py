"""Planner agent for the ADK-backed v2 control loop."""

from google.adk.agents import LlmAgent

from src.control_loop.instructions import build_planner_instruction
from src.runtime.state_keys import StateKeys


planner_agent = LlmAgent(
    name="planner",
    model="gemini-3-flash-preview",
    instruction=build_planner_instruction,
    output_key=StateKeys.TEMP_PLANNER_DRAFT,
    description="Produces a structured execution plan from task:goal and task:constraints.",
)
