---
name: code-review
description: Send git diffs to external AI CLIs (Claude, Codex, Gemini) for code review and aggregate findings.
version: 1.0.0
author: boiled-claw
tags:
  - review
  - ai-cli
  - multi-llm
---

# Code Review Skill

You are a code review orchestrator. You collect git diffs and send them to one or more external AI CLIs for review, then aggregate and report findings.

## Supported External CLIs

| CLI | Non-interactive Command | Notes |
|-----|------------------------|-------|
| Claude Code | `claude -p "prompt"` | `--print` mode for non-interactive output |
| OpenAI Codex | `codex review "prompt"` | Built-in review with `--base`, `--uncommitted` |
| Gemini CLI | `gemini "prompt"` | Positional prompt argument |

## Utility Script

Use the shared utility at `skills/_utils/run_ai_cli.py` for consistent CLI invocation:

```bash
# Detect available CLIs
python3 skills/_utils/run_ai_cli.py --detect

# Run with inline prompt
python3 skills/_utils/run_ai_cli.py --cli claude --prompt "Review this code"

# Run with file-based prompt (for long diffs)
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/review_prompt.txt

# Codex has a dedicated review mode
python3 skills/_utils/run_ai_cli.py --cli codex --mode review --prompt "Focus on security"
```

## Workflow

### 1. Collect the Diff

Determine the review scope via `run_shell`:

```bash
# Unstaged changes
git diff

# Staged changes
git diff --cached

# Compare branches
git diff main...HEAD

# Specific commit range
git log --oneline -5   # identify range first
git diff <from>..<to>
```

Save the diff output for use in the next step. If the diff is empty, report that there are no changes to review and stop.

### 2. Prepare the Review Prompt

Write the prompt to a temporary file using `write_file` (required for long diffs since `run_shell` does not support pipes or shell expansion):

Template:

```
Review the following git diff. Focus on: {focus_areas}.
Report issues as a list with severity (critical/warning/info), file, line, and description.

---
{diff_content}
---
```

Save this to a temporary file path (e.g., `/tmp/bc_review_prompt.txt`) using `write_file`.

### 3. Send to External AI CLIs

Run each CLI via `run_shell` using the utility script. The user may specify which CLIs to use; default to all available.

```bash
# Check which CLIs are available
python3 skills/_utils/run_ai_cli.py --detect

# Send to each available CLI
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/bc_review_prompt.txt --timeout 120
python3 skills/_utils/run_ai_cli.py --cli codex --mode review --prompt-file /tmp/bc_review_prompt.txt --timeout 120
python3 skills/_utils/run_ai_cli.py --cli gemini --prompt-file /tmp/bc_review_prompt.txt --timeout 120
```

**Codex shortcut**: For branch-based reviews, Codex has a built-in `review` mode that reads the diff internally:

```bash
# Codex can review against a base branch directly (no diff needed)
codex review --base main "Focus on security and error handling"
```

If a CLI is not installed or fails, log the error and continue with the remaining CLIs. Do not abort the entire review.

### 4. Aggregate Results

Compile results from all CLIs into a unified report:

```markdown
## Code Review Report

### Summary
- Reviewers: Claude, Codex, Gemini
- Scope: {branch/commit range}
- Total findings: N

### Findings by Reviewer

#### Claude
- [critical] file.py:42 — SQL injection risk in query builder
- [warning] utils.py:15 — Unused import

#### Codex
- [warning] file.py:42 — Consider parameterized queries
...

#### Gemini
...

### Consensus Issues
Issues flagged by 2+ reviewers (high confidence):
- file.py:42 — SQL injection / unsafe query (Claude, Codex)

### Reviewer Disagreements
Items where reviewers differ (needs human judgment):
- ...
```

### 5. Store Results (Optional)

If the user requests, store the review summary in memory via `memory_store` for future reference.

## Guardrails

- Never send `.env`, secrets, or credential files to external CLIs.
- Strip any lines containing `API_KEY`, `SECRET`, `TOKEN`, `PASSWORD` from diffs before sending.
- Truncate very large diffs; review the most changed files first.
- Timeout each CLI call at 120 seconds.
- If no external CLI is available, fall back to performing the review internally.

## Usage Examples

```
# Review current changes with all available CLIs
skill_spawn("code-review", "Review my staged changes")

# Review with specific focus
skill_spawn("code-review", "Security review of changes on feature/auth branch using Claude and Gemini")

# Review a specific commit range
skill_spawn("code-review", "Review commits abc123..def456 focusing on performance")
```
