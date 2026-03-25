#!/usr/bin/env python3
"""Utility to invoke external AI CLIs with stdin-based prompt passing.

Usage:
    python3 skills/_utils/run_ai_cli.py --cli claude --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli codex --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli gemini --prompt-file /tmp/prompt.txt
    python3 skills/_utils/run_ai_cli.py --cli codex --mode review --review-base main
    python3 skills/_utils/run_ai_cli.py --cli claude --prompt "short inline prompt"
    python3 skills/_utils/run_ai_cli.py --detect

Prompts are passed via stdin to avoid OS argument-size limits.
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
        "description": "Claude Code (Anthropic)",
    },
    "codex": {
        "binary": "codex",
        "description": "Codex CLI (OpenAI)",
    },
    "gemini": {
        "binary": "gemini",
        "description": "Gemini CLI (Google)",
    },
}


def _build_args_and_input(cli_name: str, prompt: str, mode: str,
                          extra_args: list, review_base: str | None,
                          review_uncommitted: bool) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text) for the given CLI.

    Prompts are fed via stdin so that large inputs (diffs, error logs)
    never hit OS argument-size limits.

    Special case — ``codex review``:
      Codex review reads its own diff from the repo via --base / --uncommitted.
      The --base/--uncommitted flags and the [PROMPT] positional are **mutually
      exclusive** in the Codex CLI. When a scope flag is used, custom
      instructions cannot be passed.
    """
    if cli_name == "claude":
        # claude -p  (reads prompt from stdin when no positional given)
        return ["claude", "-p"] + extra_args, prompt

    if cli_name == "codex":
        if mode == "review":
            args = ["codex", "review"]
            if review_base:
                # --base and [PROMPT] are mutually exclusive
                args += ["--base", review_base]
            elif review_uncommitted:
                # --uncommitted and [PROMPT] are mutually exclusive
                args.append("--uncommitted")
            else:
                # No scope flag — prompt is used as custom instructions
                if prompt.strip():
                    args.append(prompt)
            args += extra_args
            return args, None  # no stdin — codex reads its own diff
        # codex exec — reads from stdin when positional is "-"
        return ["codex", "exec", "-"] + extra_args, prompt

    if cli_name == "gemini":
        # gemini reads from stdin; positional is appended to stdin input
        return ["gemini"] + extra_args, prompt

    raise ValueError(f"Unknown CLI: {cli_name}")


def detect_available():
    """Print available CLIs and return their names."""
    available = []
    for name, cfg in CLI_CONFIGS.items():
        path = shutil.which(cfg["binary"])
        status = f"found ({path})" if path else "not found"
        print(f"  {name}: {status} - {cfg['description']}")
        if path:
            available.append(name)
    return available


def run_cli(cli_name: str, prompt: str, mode: str = "default",
            extra_args: list | None = None, timeout: int = 180,
            review_base: str | None = None,
            review_uncommitted: bool = False) -> int:
    """Run an AI CLI, passing the prompt via stdin."""
    if cli_name not in CLI_CONFIGS:
        print(f"Error: Unknown CLI '{cli_name}'. Available: {', '.join(CLI_CONFIGS)}", file=sys.stderr)
        return 1

    cfg = CLI_CONFIGS[cli_name]
    binary_path = shutil.which(cfg["binary"])
    if not binary_path:
        print(f"Error: '{cfg['binary']}' not found in PATH", file=sys.stderr)
        return 1

    args, stdin_text = _build_args_and_input(
        cli_name, prompt, mode, extra_args or [],
        review_base, review_uncommitted,
    )

    transport = "stdin" if stdin_text else "argv"
    print(f"--- Running: {cli_name} (mode={mode}, transport={transport}, timeout={timeout}s) ---",
          file=sys.stderr)

    try:
        result = subprocess.run(
            args,
            input=stdin_text,
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
        description="Run external AI CLIs with stdin-based prompt passing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --detect
              %(prog)s --cli claude --prompt "Explain this code"
              %(prog)s --cli codex --mode review --review-base main
              %(prog)s --cli codex --mode review --review-uncommitted
              %(prog)s --cli gemini --prompt-file /tmp/prompt.txt
        """),
    )
    parser.add_argument("--detect", action="store_true", help="List available CLIs and exit")
    parser.add_argument("--cli", choices=list(CLI_CONFIGS), help="Which AI CLI to use")
    parser.add_argument("--prompt", help="Inline prompt text")
    parser.add_argument("--prompt-file", help="Path to file containing the prompt")
    parser.add_argument("--mode", default="default", help="CLI mode (e.g. 'review' for codex)")
    parser.add_argument("--review-base", help="Base branch for codex review (e.g. 'main')")
    parser.add_argument("--review-uncommitted", action="store_true",
                        help="Review uncommitted changes (codex review --uncommitted)")
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
    prompt = ""
    if args.prompt_file:
        try:
            with open(args.prompt_file) as f:
                prompt = f.read()
        except FileNotFoundError:
            print(f"Error: Prompt file not found: {args.prompt_file}", file=sys.stderr)
            sys.exit(1)
    elif args.prompt:
        prompt = args.prompt

    # codex review doesn't require a prompt (reads its own diff)
    if args.mode == "review" and args.cli == "codex":
        if not args.review_base and not args.review_uncommitted:
            parser.error("codex review requires --review-base or --review-uncommitted")
    elif not prompt.strip():
        parser.error("Either --prompt or --prompt-file is required")

    rc = run_cli(
        args.cli, prompt, mode=args.mode, extra_args=args.extra_args,
        timeout=args.timeout, review_base=args.review_base,
        review_uncommitted=args.review_uncommitted,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
