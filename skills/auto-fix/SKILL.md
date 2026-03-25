---
name: auto-fix
description: Detect lint/test failures, send context to external AI CLIs for fixes, apply patches, and verify in a loop.
version: 1.0.0
author: boiled-claw
tags:
  - auto-fix
  - ai-cli
  - lint
  - testing
  - ci
---

# Auto-Fix Skill

You are an automated fix orchestrator. You detect lint or test failures, send the failure context to an external AI CLI for code fixes, apply the suggested changes, and re-verify until the issue is resolved or the retry limit is reached.

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

# Send fix prompt via file (recommended for long error context)
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/fix_prompt.txt --timeout 120
```

## Workflow

### 1. Detect Failures

Run the user-specified check command (or auto-detect) via `run_shell`:

```bash
# Lint checks (auto-detect based on project)
ruff check .                   # Python (ruff)
flake8 .                       # Python (flake8)
eslint .                       # JavaScript/TypeScript
golangci-lint run              # Go

# Test checks
pytest -x --tb=short           # Python
npm test                       # Node.js
go test ./...                  # Go

# Type checks
mypy src/                      # Python
tsc --noEmit                   # TypeScript
```

Capture the full output (stdout + stderr) and the exit code.

If the check passes (exit code 0), report success and stop.

### 2. Parse Failure Context

Extract structured information from the failure output:
- **File path** and **line number** of each error
- **Error message** / **test name**
- **Error category** (syntax, type, import, assertion, etc.)

Read the relevant source files around the error locations to provide context.

### 3. Build Fix Prompt

Write the prompt to a temporary file using `write_file` (required — `run_shell` does not support pipes or shell expansion):

```
Fix the following errors in this codebase.

## Errors
{parsed error list with file:line and message}

## Relevant Source Code

### {file1}:{start_line}-{end_line}
```{language}
{source code}
```

### {file2}:{start_line}-{end_line}
...

## Instructions
- Output ONLY the corrected code blocks, prefixed with the file path.
- Do not change unrelated code.
- Preserve existing style and formatting.
- Format as:

### {filepath}
```{language}
{corrected full file content or relevant section}
```
```

Save to e.g. `/tmp/bc_fix_prompt.txt`.

### 4. Send to External AI CLI

Use the user's preferred CLI (default: first available) via the utility script:

```bash
python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/bc_fix_prompt.txt --timeout 120
```

Parse the response to extract file paths and corrected code blocks.

### 5. Apply Fixes

For each corrected code block returned:
1. Read the original file.
2. Apply the fix (replace the relevant section or full file).
3. Use `write_file` or direct file operations.

### 6. Verify (Loop)

Re-run the original check command:

```bash
# Same command as Step 1
ruff check .
pytest -x --tb=short
```

**Loop conditions:**
- **Pass (exit 0):** Report success, show summary of all changes made. Stop.
- **Fail (same errors):** The fix didn't work. Try a different CLI or refine the prompt. Max 3 retries.
- **Fail (new errors):** The fix introduced regressions. Revert the last change and try again.
- **Max retries reached:** Report the current state, list remaining errors, and suggest manual fixes.

### 7. Report

Produce a final report:

```markdown
## Auto-Fix Report

### Status: {FIXED / PARTIALLY_FIXED / FAILED}

### Check Command
`{command}`

### Iterations
| # | CLI Used | Errors Before | Errors After | Result |
|---|----------|---------------|--------------|--------|
| 1 | Claude   | 5             | 2            | partial |
| 2 | Codex    | 2             | 0            | fixed   |

### Changes Made
- `src/foo.py:42` — Fixed missing return type annotation
- `src/bar.py:15-20` — Replaced deprecated API call

### Remaining Issues (if any)
- `src/baz.py:99` — Complex logic error, needs manual review
```

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| max_retries | 3 | Maximum fix-verify iterations |
| check_command | auto-detect | The lint/test command to run |
| preferred_cli | auto | Which AI CLI to use first |
| revert_on_regression | true | Revert if new errors are introduced |
| timeout_per_cli | 120 | Seconds to wait for CLI response |

## Guardrails

- **Max 3 retries** to prevent infinite loops.
- **Revert on regression**: If a fix introduces more errors than it solves, revert immediately.
- **No destructive changes**: Never delete files or remove large code sections without explicit user approval.
- **Scope control**: Only modify files directly related to the reported errors.
- **Backup**: Before applying fixes, note the original content so changes can be reverted.
- Never send `.env`, secrets, or credential files to external CLIs.

## Usage Examples

```
# Auto-fix lint errors
skill_spawn("auto-fix", "Fix all ruff lint errors in this project")

# Auto-fix failing tests
skill_spawn("auto-fix", "Fix the failing pytest tests using Claude CLI")

# Auto-fix with specific command
skill_spawn("auto-fix", "Run 'mypy src/' and fix all type errors using Codex")

# Auto-fix with retry limit
skill_spawn("auto-fix", "Fix eslint errors, max 2 retries, prefer Gemini")
```
