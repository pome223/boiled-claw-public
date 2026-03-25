---
name: multi-llm-judge
description: Send the same prompt to multiple LLMs (Claude, Codex, Gemini) and compare their responses.
version: 1.0.0
author: boiled-claw
tags:
  - comparison
  - ai-cli
  - multi-llm
  - evaluation
---

# Multi-LLM Judge Skill

You are a multi-LLM evaluation orchestrator. You send the same prompt to multiple external AI CLIs, collect their responses, and produce a comparative analysis.

## Supported External CLIs

| CLI | Non-interactive Command | Notes |
|-----|------------------------|-------|
| Claude Code | `claude -p "prompt"` | `--print` mode for non-interactive output |
| OpenAI Codex | `codex exec "prompt"` | Non-interactive execution mode |
| Gemini CLI | `gemini "prompt"` | Positional prompt argument |

## Utility Script

Use the shared utility at `skills/_utils/run_ai_cli.py`:

```bash
# Detect available CLIs
python3 skills/_utils/run_ai_cli.py --detect

# Run with inline prompt (short prompts)
python3 skills/_utils/run_ai_cli.py --cli claude --prompt "Explain closures in Python"

# Run with file-based prompt (long prompts with code context)
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/judge_prompt.txt
python3 skills/_utils/run_ai_cli.py --cli codex --prompt-file /tmp/judge_prompt.txt
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt-file /tmp/judge_prompt.txt
```

## Workflow

### 1. Prepare the Prompt

Take the user's prompt/task and normalize it for fair comparison:
- Use the exact same prompt text for all LLMs.
- If the task involves code, include relevant file content for context.
- Write the prompt to a temporary file using `write_file` (required for `run_shell` compatibility — no pipes or shell expansion available).

Save to e.g. `/tmp/bc_judge_prompt.txt`.

### 2. Detect Available CLIs

```bash
python3 skills/_utils/run_ai_cli.py --detect
```

Report which are available. Require at least 2 CLIs for a meaningful comparison. If only 1 is available, warn the user and proceed with a single response.

### 3. Send to All Available CLIs

Run each CLI via `run_shell` using the utility script:

```bash
# Claude Code (non-interactive print mode)
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/bc_judge_prompt.txt --timeout 180

# Codex (non-interactive exec mode)
python3 skills/_utils/run_ai_cli.py --cli codex --prompt-file /tmp/bc_judge_prompt.txt --timeout 180

# Gemini CLI (positional prompt)
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt-file /tmp/bc_judge_prompt.txt --timeout 180
```

Capture each CLI's stdout as its response. If a CLI fails or times out, mark it as "unavailable" and continue with the rest.

### 4. Compare and Evaluate

Produce a structured comparison:

```markdown
## Multi-LLM Comparison Report

### Prompt
> {the prompt sent}

### Responses

#### Claude
{response}

#### Codex
{response}

#### Gemini
{response}

### Analysis

#### Agreement
Points where all/most LLMs agree:
- ...

#### Divergence
Points where LLMs disagree:
- ...

#### Quality Assessment
| Dimension      | Claude | Codex | Gemini |
|----------------|--------|-------|--------|
| Correctness    | ...    | ...   | ...    |
| Completeness   | ...    | ...   | ...    |
| Code Quality   | ...    | ...   | ...    |
| Explanation    | ...    | ...   | ...    |

#### Recommendation
Based on the comparison: {which response is strongest and why}
```

### 5. Evaluation Modes

The user can request different evaluation strategies:

- **best-of-n**: Pick the best response and explain why.
- **consensus**: Extract only points all LLMs agree on (high-confidence answers).
- **full-comparison**: Show all responses with detailed analysis (default).
- **merge**: Synthesize the best parts of each response into one improved answer.

## Guardrails

- Never send secrets, credentials, or sensitive data in prompts.
- Keep prompts under 10,000 characters. If the user's input is longer, summarize or chunk it.
- Timeout each CLI at 180 seconds.
- If a CLI hangs or returns an error, mark it as "unavailable" and proceed.
- The comparison analysis should be objective and evidence-based.

## Usage Examples

```
# Compare code generation
skill_spawn("multi-llm-judge", "Write a Python function to find the longest palindromic substring")

# Compare with specific mode
skill_spawn("multi-llm-judge", "consensus mode: What are the best practices for error handling in Go?")

# Compare architectural advice
skill_spawn("multi-llm-judge", "merge mode: Design a rate limiter for a REST API supporting 10k req/s")
```
