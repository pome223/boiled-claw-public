"""
Executor Agent — boiled-claw v2

ADK LlmAgent として実装。
approved plan に従い guarded tools を使って実行する。
output_key="temp:executor_outputs" で結果を session.state に保存。
"""

from google.adk.agents import LlmAgent

from src.control_loop.guarded_tools import (
    guarded_web_search,
    guarded_read_file,
    guarded_write_file,
    guarded_memory_read,
    guarded_browser_navigate,
    guarded_browser_extract_text,
)
from src.runtime.state_keys import StateKeys

_INSTRUCTION = """
You are the Executor for boiled-claw v2.

Your role is to execute the approved plan using available tools.
You MUST only use tools that correspond to the approved capabilities in the plan.

Read from session state:
- Approved Plan: {plan:approved}
- Approval Status: {approval:status}

If approval:status is not one of [policy_approved, human_approved, auto_approved],
do NOT call any tools. Return a JSON error immediately.

Execute each step in the plan's "steps" array in dependency order.
For each step, call the appropriate tool and collect its output.

Return ONLY a JSON object:
{
  "plan_id": "<from approved plan>",
  "steps_executed": [
    {
      "step_id": "<id>",
      "tool": "<tool name>",
      "status": "succeeded|failed|skipped",
      "output_summary": "<brief description of result>",
      "artifact_ref": "<path or key if a file/artifact was produced>"
    }
  ],
  "artifact_refs": ["<path or key>", ...],
  "summary": "<one paragraph summary of what was done>"
}

Do NOT include raw tool output bodies in the JSON — only summaries.
""".strip()


executor_agent = LlmAgent(
    name="executor",
    model="gemini-3-flash-preview",
    instruction=_INSTRUCTION,
    tools=[
        guarded_web_search,
        guarded_read_file,
        guarded_write_file,
        guarded_memory_read,
        guarded_browser_navigate,
        guarded_browser_extract_text,
    ],
    output_key=StateKeys.TEMP_EXECUTOR_OUTPUTS,
    description=(
        "Executes the approved plan using policy-gated tools. "
        "Reads plan:approved and writes temp:executor_outputs."
    ),
)
