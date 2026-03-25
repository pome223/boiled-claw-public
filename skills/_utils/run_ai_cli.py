#!/usr/bin/env python3
"""Utility to invoke external AI CLIs with file-based prompt passing.

Usage:
    python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli codex --mode review --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli gemini --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli claude --prompt "short inline prompt"
    python3 skills/_utils/run_ai_cli.py --detect  # list available CLIs

Designed to work with boiled-claw's run_shell (subprocess_exec, no shell).
"""

import argparse
import shutil
import subprocess
import sys
import textwrap


CLI_CONFIGS = {
    "claude": {
        "binary": "claude",
        "build_args": lambda prompt, mode, extra: ["claude", "-p", "--verbose", prompt] + extra,
        "description": "Claude Code (Anthropic)",
    },
    "codex": {
        "binary": "codex",
        "build_args": lambda prompt, mode, extra: (
            ["codex", "review", prompt] + extra
            if mode == "review"
            else ["codex", "exec", prompt] + extra
        ),
        "description": "Codex CLI (OpenAI)",
    },
    "gemini": {
        "binary": "gemini",
        "build_args": lambda prompt, mode, extra: ["gemini", prompt] + extra,
        "description": "Gemini CLI (Google)",
    },
}


def detect_available():
    """Print available CLIs."""
    available = []
    for name, cfg in CLI_CONFIGS.items():
        path = shutil.which(cfg["binary"])
        status = f"found ({path})" if path else "not found"
        print(f"  {name}: {status} - {cfg['description']}")
        if path:
            available.append(name)
    return available


def run_cli(cli_name: str, prompt: str, mode: str = "default",
            extra_args: list | None = None, timeout: int = 180) -> int:
    """Run an AI CLI and stream output to stdout."""
    if cli_name not in CLI_CONFIGS:
        print(f"Error: Unknown CLI '{cli_name}'. Available: {', '.join(CLI_CONFIGS)}", file=sys.stderr)
        return 1

    cfg = CLI_CONFIGS[cli_name]
    binary_path = shutil.which(cfg["binary"])
    if not binary_path:
        print(f"Error: '{cfg['binary']}' not found in PATH", file=sys.stderr)
        return 1

    args = cfg["build_args"](prompt, mode, extra_args or [])

    print(f"--- Running: {cli_name} (mode={mode}, timeout={timeout}s) ---", file=sys.stderr)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"[{cli_name} stderr]:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"Error: {cli_name} timed out after {timeout}s", file=sys.stderr)
        return 124
    except Exception as e:
        print(f"Error running {cli_name}: {e}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Run external AI CLIs with file-based prompt passing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --detect
              %(prog)s --cli claude --prompt "Explain this code"
              %(prog)s --cli codex --mode review --extra-args "--base main"
              %(prog)s --cli gemini --prompt-file /tmp/prompt.txt
        """),
    )
    parser.add_argument("--detect", action="store_true", help="List available CLIs and exit")
    parser.add_argument("--cli", choices=list(CLI_CONFIGS), help="Which AI CLI to use")
    parser.add_argument("--prompt", help="Inline prompt text")
    parser.add_argument("--prompt-file", help="Path to file containing the prompt")
    parser.add_argument("--mode", default="default", help="CLI mode (e.g. 'review' for codex)")
    parser.add_argument("--extra-args", nargs="*", default=[], help="Extra arguments to pass to the CLI")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds (default: 180)")

    args = parser.parse_args()

    if args.detect:
        available = detect_available()
        if not available:
            print("\nNo AI CLIs found. Install one of: claude, codex, gemini")
            sys.exit(1)
        print(f"\nAvailable: {', '.join(available)}")
        sys.exit(0)

    if not args.cli:
        parser.error("--cli is required (or use --detect)")

    # Read prompt
    if args.prompt_file:
        try:
            with open(args.prompt_file) as f:
                prompt = f.read()
        except FileNotFoundError:
            print(f"Error: Prompt file not found: {args.prompt_file}", file=sys.stderr)
            sys.exit(1)
    elif args.prompt:
        prompt = args.prompt
    else:
        parser.error("Either --prompt or --prompt-file is required")

    if not prompt.strip():
        print("Error: Prompt is empty", file=sys.stderr)
        sys.exit(1)

    rc = run_cli(args.cli, prompt, mode=args.mode, extra_args=args.extra_args, timeout=args.timeout)
    sys.exit(rc)


if __name__ == "__main__":
    main()
