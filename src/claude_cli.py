"""
claude_cli.py — Reusable wrapper for calling Claude from Python via the user's
Claude Pro/Max subscription (NOT the metered Anthropic API).

WHY THIS EXISTS
---------------
The `claude_agent_sdk` Python package and the Anthropic SDK both bill against
the metered Anthropic API (`ANTHROPIC_API_KEY`). The ONLY path to use a Claude
Code subscription from a Python script is to invoke the `claude` CLI as a
subprocess.

Three Windows-specific gotchas this wrapper handles:
  1. `subprocess.run(["claude", ...])` fails with FileNotFoundError on Windows
     because the npm-installed binary is `claude.CMD` and Python's subprocess
     doesn't apply PATHEXT to bare names. Fix: resolve the absolute path with
     shutil.which() at first call.
  2. `subprocess.run(input=str, text=True)` defaults to cp1252 on Windows and
     dies on financial/scientific Unicode (U+2212 minus, en/em dashes, arrows).
     Fix: force `encoding="utf-8"` and `errors="replace"`.
  3. Even with subscription auth set up, if `ANTHROPIC_API_KEY` is set in the
     environment, the CLI silently falls back to API billing. Fix: lazy check
     at first call, raise loudly with the unset instructions.

ONE-TIME SETUP (per machine)
----------------------------
1. Install Node.js (https://nodejs.org).
2. Install the CLI:
     npm install -g @anthropic-ai/claude-code
3. Authenticate to your subscription (browser flow):
     claude auth login
4. Verify:
     claude auth status     # should show your subscription account
     claude --version       # confirms binary is on PATH
5. Remove ANTHROPIC_API_KEY from your shell env / .env / shell profile.

USAGE
-----
    from claude_cli import call_claude
    response = call_claude("Summarize this in one sentence: " + text)

Or as a CLI test:
    python claude_cli.py "What is 2+2?"
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Override DEFAULT_MODEL per-call if you need higher fidelity (Opus) or speed (Haiku).
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_SECONDS = 600

_setup_verified: bool = False
_claude_cli_path: str | None = None


def _verify_setup_once() -> None:
    """Lazy environment check on first call — fails loud rather than mis-billing."""
    global _setup_verified, _claude_cli_path
    if _setup_verified:
        return
    # Treat an empty-string value as unset. Claude Code's Bash tool leaks
    # ANTHROPIC_API_KEY='' into subshells even when the user has it unset, which
    # used to trip this guard as a false positive. Only a non-empty value would
    # actually route the CLI to API billing.
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set in the environment. The Claude Code CLI will silently "
            "route calls to API billing instead of your subscription. Unset it before running:\n"
            "  PowerShell:  Remove-Item env:ANTHROPIC_API_KEY\n"
            "  Bash:        unset ANTHROPIC_API_KEY"
        )
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found in PATH. Install it with:\n"
            "  npm install -g @anthropic-ai/claude-code\n"
            "Then authenticate with: claude auth login"
        )
    _claude_cli_path = resolved
    _setup_verified = True


def call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """
    Single-shot LLM call via the Claude Code CLI. Returns the model's text response.

    Prompts are passed via stdin to avoid the Windows CreateProcess command-line
    length limit (32K). Output is decoded as UTF-8 with error replacement so bad
    bytes from upstream sources (e.g., PDF extraction) don't crash the call.

    Raises:
      RuntimeError: setup is wrong (CLI not installed, or ANTHROPIC_API_KEY set).
      subprocess.CalledProcessError: CLI returned non-zero exit; stderr in .stderr.
      subprocess.TimeoutExpired: prompt didn't finish within timeout_seconds.
    """
    _verify_setup_once()
    assert _claude_cli_path is not None
    result = subprocess.run(
        [_claude_cli_path, "-p", "--model", model],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=timeout_seconds,
    )
    text = result.stdout.strip()
    if not text:
        raise RuntimeError(f"claude -p returned empty stdout. stderr: {result.stderr.strip()}")
    return text


if __name__ == "__main__":
    # CLI test entrypoint: `python claude_cli.py "your prompt here"`
    if len(sys.argv) < 2:
        print("Usage: python claude_cli.py <prompt>", file=sys.stderr)
        sys.exit(2)
    prompt = " ".join(sys.argv[1:])
    print(call_claude(prompt))
