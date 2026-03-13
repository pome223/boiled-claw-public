"""Executor agent for the ADK-backed v2 control loop."""

from google.adk.agents import LlmAgent

from src.control_loop.guarded_tools import (
    guarded_web_search,
    guarded_read_file,
    guarded_write_file,
    guarded_memory_read,
    guarded_browser_navigate,
    guarded_browser_extract_text,
    guarded_desktop_view_windows,
    guarded_desktop_view_frontmost_app,
    guarded_desktop_view_screenshot,
    guarded_desktop_ax_snapshot,
    guarded_desktop_control_click,
    guarded_desktop_control_focus_window,
    guarded_desktop_control_type,
    guarded_desktop_control_hotkey,
    guarded_desktop_control_launch_app,
    guarded_desktop_control_drag,
)
from src.control_loop.instructions import build_executor_instruction
from src.runtime.state_keys import StateKeys


executor_agent = LlmAgent(
    name="executor",
    model="gemini-3-flash-preview",
    instruction=build_executor_instruction,
    tools=[
        guarded_web_search,
        guarded_read_file,
        guarded_write_file,
        guarded_memory_read,
        guarded_browser_navigate,
        guarded_browser_extract_text,
        guarded_desktop_view_windows,
        guarded_desktop_view_frontmost_app,
        guarded_desktop_view_screenshot,
        guarded_desktop_ax_snapshot,
        guarded_desktop_control_click,
        guarded_desktop_control_type,
        guarded_desktop_control_launch_app,
        guarded_desktop_control_focus_window,
        guarded_desktop_control_hotkey,
        guarded_desktop_control_drag,
    ],
    output_key=StateKeys.TEMP_EXECUTOR_OUTPUTS,
    description=(
        "Executes the approved plan using policy-gated tools. "
        "Reads plan:approved and writes temp:executor_outputs."
    ),
)
