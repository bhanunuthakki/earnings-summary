"""
claude_cli.py — Reusable wrapper for calling Claude from Python via the
``claude`` CLI as a subprocess.

WHY THIS EXISTS
---------------
The CLI accepts a prompt on stdin and returns the model's text on stdout,
honoring whatever auth mechanism is configured in the environment
(``ANTHROPIC_API_KEY`` for metered API billing, or ``claude auth login`` for
subscription billing — whichever the user has set up).

Two Windows-specific gotchas this wrapper handles:
  1. ``subprocess.run(["claude", ...])`` fails with FileNotFoundError on Windows
     because the npm-installed binary is ``claude.CMD`` and Python's subprocess
     doesn't apply PATHEXT to bare names. Fix: resolve the absolute path with
     ``shutil.which()`` at first call.
  2. ``subprocess.run(input=str, text=True)`` defaults to cp1252 on Windows and
     dies on financial/scientific Unicode (U+2212 minus, en/em dashes, arrows).
     Fix: force ``encoding="utf-8"`` and ``errors="replace"``.

ONE-TIME SETUP (per machine)
----------------------------
1. Install Node.js (https://nodejs.org).
2. Install the CLI:
     npm install -g @anthropic-ai/claude-code
3. Set ``ANTHROPIC_API_KEY`` in your shell / ``.env``, OR run ``claude auth login``
   for the subscription path. Either works.
4. Verify:
     claude --version       # confirms binary is on PATH

USAGE
-----
    from claude_cli import call_claude
    response = call_claude("Summarize this in one sentence: " + text)

Or as a CLI test:
    python claude_cli.py "What is 2+2?"
"""

from __future__ import annotations

import shutil
import subprocess
import sys

# Override DEFAULT_MODEL per-call if you need higher fidelity (Opus) or speed (Haiku).
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_SECONDS = 600

_setup_verified: bool = False
_claude_cli_path: str | None = None


def _verify_setup_once() -> None:
    """Resolve the absolute path to the ``claude`` binary on first call.

    Bare ``"claude"`` fails in Windows subprocess because PATHEXT isn't applied
    to bare names — the binary is ``claude.CMD``. Cached so repeat calls are
    free.
    """
    global _setup_verified, _claude_cli_path
    if _setup_verified:
        return
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found in PATH. Install it with:\n"
            "  npm install -g @anthropic-ai/claude-code\n"
            "Then either set ANTHROPIC_API_KEY in your shell / .env, "
            "or run: claude auth login"
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
      RuntimeError: CLI binary missing on PATH.
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
