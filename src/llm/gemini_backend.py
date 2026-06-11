# pyright: reportPrivateUsage=false
#
# This module intentionally calls `llm.cli._enforce_budget_pre_call` (the
# single budget-gate implementation shared by every backend) via a module
# reference so the existing test monkeypatch surface —
# ``monkeypatch.setattr(llm.cli, "_enforce_budget_pre_call", ...)`` — covers
# the Gemini path too. The module-level directive silences only that
# cross-module private-usage rule, preserving every other strict check
# (same precedent as src/llm/cli.py).
"""
src/llm/gemini_backend.py
-------------------------
Consumer-subscription Gemini CLI backend — the EVAL-GATED second backend
behind ``call_llm``. Mirrors the Claude CLI subprocess pattern in
``src/llm/cli.py``: prompt via stdin (Windows 32K CreateProcess limit),
forced UTF-8, absolute .cmd path, JSON output envelope, per-purpose budget
gating, and a best-effort ``llm_calls`` ledger row per call.

DISTINCT from ``src/llm/fallback.py``: the fallback is the API-key-billed
emergency path that fires only when a CLAUDE call fails operationally. This
module is a first-class backend that PRODUCTION ROUTING can select per
purpose — but only after the LLM-evals judges grade its output quality for
that purpose. Until then the allowlist below ships EMPTY and nothing routes
here (``execution/compare_backends.py`` calls it explicitly to produce the
side-by-side outputs the judges grade).

Billing guard (the no-silent-API-key invariant): the subprocess environment
STRIPS ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` / Vertex variables and sets
``GOOGLE_GENAI_USE_GCA=true``, which forces the CLI onto the consumer OAuth
("Login with Google" / Gemini Code Assist) auth path. A call can therefore
only ever bill the user's consumer Google account; if the one-time
interactive ``gemini`` login has never been done, the call fails fast with
``LLMSetupError`` naming the fix — it can never quietly fall back to the
metered API key the repo keeps in ``.env`` for the fallback path.

Setup + routing policy: directives/gemini_backend.md.

Public API:
    GEMINI_BACKEND_DEFAULT_MODEL, GEMINI_BACKEND_FAST_MODEL — model ids.
    GEMINI_MODELS — explicit per-purpose Gemini model pins (ships empty).
    GEMINI_BACKEND_ALLOWED_PURPOSES — the eval-gated routing allowlist
        (ships EMPTY; see gemini_allowed_purposes for the env-var trial hook).
    gemini_allowed_purposes() — allowlist ∪ GEMINI_BACKEND_PURPOSES env csv.
    gemini_model_for(purpose) — purpose → Gemini model id.
    call_gemini(...) — single-shot Gemini CLI call (same contract as
        llm.cli._call_claude).
    parse_gemini_json_output(stdout) — JSON envelope → (text, meta).
    usage_meta_from_gemini_stats(meta) — Gemini stats → claude-shaped usage
        meta for the shared ledger writer (cost pinned to $0 subscription).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llm import cli as llm_cli
from llm.ledger import record_llm_call

log = logging.getLogger(__name__)

# Consumer-tier headline model for analytical writing; Flash for the short
# structured calls that run on Haiku under the Claude backend. The tier
# derivation in gemini_model_for keeps these two aligned with LLM_MODELS so
# retuning a purpose's Claude tier automatically retunes its Gemini mirror.
# Pro = gemini-3.1-pro-preview (current Pro baseline; gemini-3-pro-preview was
# discontinued 2026-03 and there is no 3.5 Pro — only 3.5 Flash). CLI-verified.
GEMINI_BACKEND_DEFAULT_MODEL = "gemini-3.1-pro-preview"
GEMINI_BACKEND_FAST_MODEL = "gemini-3.5-flash"

# Explicit per-purpose Gemini model pins. Ships EMPTY: the default tier
# derivation (Claude fast-classifier purposes → Flash, everything else → Pro)
# is right until an eval says otherwise. Add an entry here when the judges
# find a purpose needs a specific Gemini model (e.g. a long-context bulk job
# pinned to a 1M-context model id).
GEMINI_MODELS: dict[str, str] = {}

# THE EVAL GATE. Purposes listed here route to Gemini in production via
# call_llm's backend resolution. Ships EMPTY by design: a purpose may be
# added ONLY after the LLM-evals judges grade its Gemini output quality
# against Claude's on real prompts (execution/compare_backends.py produces
# the side-by-side corpus they grade). Process + sign-off requirements:
# directives/gemini_backend.md.
GEMINI_BACKEND_ALLOWED_PURPOSES: frozenset[str] = frozenset()

# Local-trial escape hatch: a comma-separated purpose list in this env var is
# merged into the allowlist for the current process only. Lets the operator
# trial-route a purpose without a code change; production stays empty.
GEMINI_BACKEND_PURPOSES_ENV_VAR = "GEMINI_BACKEND_PURPOSES"

# Same default cap as the Claude path (long-context analytical prompts), with
# its own env override mirroring CLAUDE_CLI_TIMEOUT_SECONDS.
GEMINI_BACKEND_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_CLI_TIMEOUT_SECONDS", "1200"))

# One-time interactive login instruction, surfaced verbatim in LLMSetupError
# whenever the CLI reports an auth failure in headless mode.
GEMINI_LOGIN_HINT = (
    "The Gemini backend bills the consumer Google account via OAuth, which "
    "needs a one-time interactive login: run `gemini` in any terminal, pick "
    "'Login with Google', and complete the browser flow (credentials are "
    "cached under C:\\Users\\bhanu\\.gemini). API keys are deliberately "
    "stripped from the backend's environment, so no login means no call."
)

# Auth failures surface differently depending on where the CLI dies (raw
# FatalAuthenticationError trace on stderr when forced-GCA finds no cached
# creds; a JSON error envelope on stdout when no auth method resolves at
# all). Any of these markers in stdout+stderr of a failed call means
# "operator must log in" — deterministic, so LLMSetupError not transient.
_AUTH_ERROR_MARKERS = (
    "FatalAuthenticationError",
    "Please set an Auth method",
    "Manual authorization is required",
)

# Env vars stripped from the subprocess so the CLI cannot silently bill the
# metered Gemini API / Vertex instead of the consumer subscription. The
# repo's .env keeps GEMINI_API_KEY for the *fallback* path (src/llm/fallback.py)
# — that key must never leak into backend calls.
_STRIPPED_ENV_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
)

# Context-file name the backend's neutral workspace points gemini-cli at.
# Nothing by this name exists anywhere, so NO context files load — without
# this, the CLI injects the user's global ~/.gemini/GEMINI.md (the ~17KB
# machine rulebook) into every backend prompt, contaminating judged output.
_NULL_CONTEXT_FILENAME = "ES_GEMINI_BACKEND_NULL_CONTEXT.md"

# Module-level caches, mirroring llm_client._setup_verified /
# _claude_cli_path for the Claude backend. Tests monkeypatch these directly.
_gemini_setup_verified: bool = False
_gemini_cli_path: str | None = None
_gemini_neutral_cwd_cache: str | None = None


def gemini_allowed_purposes() -> frozenset[str]:
    """The effective routing allowlist: the code-level eval-gated frozenset
    plus any purposes in the GEMINI_BACKEND_PURPOSES env var (comma-separated,
    local-trial escape hatch). Re-read per call so a test or an operator
    shell can flip it without a process restart."""
    raw = os.environ.get(GEMINI_BACKEND_PURPOSES_ENV_VAR, "")
    env_purposes = {p.strip() for p in raw.split(",") if p.strip()}
    return GEMINI_BACKEND_ALLOWED_PURPOSES | frozenset(env_purposes)


def gemini_model_for(purpose: str | None) -> str:
    """Resolve a purpose to a Gemini model id.

    Order: explicit GEMINI_MODELS pin → tier derivation from the Claude
    model table (purposes that run on the fast classifier under Claude get
    Flash; everything else gets Pro) → Pro for unknown/None purposes. The
    derivation means one table (LLM_MODELS) keeps both backends' latency
    tiers in sync without a parallel registry to maintain.
    """
    if purpose is not None:
        pinned = GEMINI_MODELS.get(purpose)
        if pinned is not None:
            return pinned
        if llm_cli.LLM_MODELS.get(purpose) == llm_cli.FAST_CLASSIFIER_MODEL:
            return GEMINI_BACKEND_FAST_MODEL
    return GEMINI_BACKEND_DEFAULT_MODEL


def _verify_gemini_setup_once() -> None:
    """Resolve and cache the absolute path to the ``gemini`` binary.

    Windows-specific: bare ``"gemini"`` fails because the npm-installed
    binary is ``gemini.cmd`` and Python's subprocess doesn't apply PATHEXT
    to bare names — identical gotcha to the ``claude`` wrapper. Missing
    binary raises LLMSetupError (deterministic, operator-actionable; see
    llm.cli.is_hard_stop). OAuth state is NOT pre-checked here: credential
    storage is the CLI's business (file today, maybe keychain tomorrow) —
    auth problems are classified from the subprocess failure instead.
    """
    global _gemini_setup_verified, _gemini_cli_path
    if _gemini_setup_verified:
        return
    resolved = shutil.which("gemini")
    if resolved is None:
        raise llm_cli.LLMSetupError(
            "Gemini CLI ('gemini') not found in PATH. Install it with "
            "`npm install -g @google/gemini-cli`, then run `gemini` once "
            "interactively to log in with your Google account. "
            "See directives/gemini_backend.md."
        )
    _gemini_cli_path = resolved
    _gemini_setup_verified = True


def _gemini_subprocess_env() -> dict[str, str]:
    """Subprocess environment enforcing consumer-subscription billing.

    Strips every API-key / Vertex variable (so the CLI cannot silently pick
    one up — the exact failure mode the claude wrapper guards with its
    ANTHROPIC_API_KEY check) and force-selects the consumer OAuth auth path:

      * GOOGLE_GENAI_USE_GCA=true — auth via Gemini Code Assist OAuth (the
        consumer "Login with Google" identity), never an API key.
      * GEMINI_CLI_TRUST_WORKSPACE=true — headless runs in the neutral cwd
        are untrusted by default, which both blocks execution and ignores
        the workspace settings.json that suppresses context injection. The
        neutral cwd is an empty temp dir we create, so trusting it is safe.
      * NO_BROWSER=true — belt-and-braces: were the CLI ever to attempt an
        interactive auth flow from a cron box, it must fail fast, not pop a
        browser. (Non-interactive mode already errors out; verified v0.46.)
    """
    env = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    env["GOOGLE_GENAI_USE_GCA"] = "true"
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    env["NO_BROWSER"] = "true"
    return env


def _gemini_neutral_cwd() -> str:
    """A throwaway workspace for the subprocess, mirroring the Claude path's
    neutral-cwd trick (skip the repo's project config) with one addition: a
    workspace ``.gemini/settings.json`` that points context-file discovery
    at a filename that exists nowhere. That suppresses BOTH the repo's
    project context and the user's global ~/.gemini/GEMINI.md rulebook, so
    backend prompts reach the model clean. Cached per process."""
    global _gemini_neutral_cwd_cache
    if _gemini_neutral_cwd_cache is None:
        d = tempfile.mkdtemp(prefix="es_gemini_cwd_")
        settings_dir = Path(d) / ".gemini"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings = {"context": {"fileName": _NULL_CONTEXT_FILENAME}}
        (settings_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        _gemini_neutral_cwd_cache = d
    return _gemini_neutral_cwd_cache


def parse_gemini_json_output(stdout: str) -> tuple[str, dict[str, object]]:
    """Parse the JSON envelope from ``gemini -o json``.

    Success shape (packages/core .../json-formatter.js)::

      {"session_id": "...", "response": "<text>",
       "stats": {"models": {"<model-id>": {"api": {...}, "tokens": {...}}}, ...},
       "warnings": [...]}

    Error shape: ``{"session_id": "...", "error": {"type", "message", "code"}}``.

    Returns (response_text, full_payload). Raises ValueError when stdout is
    not JSON, carries an ``error`` object, or has no string ``response`` —
    callers route that onto the same operational-failure path as a non-zero
    exit.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini CLI did not return JSON: {stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Gemini CLI returned non-object JSON: {type(payload).__name__}")
    payload_dict = cast("dict[str, object]", payload)
    error = payload_dict.get("error")
    if isinstance(error, dict):
        error_dict = cast("dict[str, object]", error)
        raise ValueError(
            f"Gemini CLI reported error: message={error_dict.get('message')!r} "
            f"code={error_dict.get('code')!r}"
        )
    response = payload_dict.get("response")
    if not isinstance(response, str):
        raise ValueError(
            f"Gemini CLI JSON missing string `response`: keys={sorted(payload_dict.keys())}"
        )
    return response, payload_dict


def usage_meta_from_gemini_stats(payload: dict[str, object]) -> dict[str, object]:
    """Gemini session stats → the claude-shaped usage meta the shared ledger
    writer (llm.ledger.record_llm_call → usage_from_json_meta) consumes.

    Token mapping, summed across every model the session touched (the
    consumer tier can transparently reroute pro → flash under load):
    ``tokens.prompt`` → input_tokens, ``tokens.candidates`` → output_tokens,
    ``tokens.cached`` → cache_read_input_tokens. Cost is pinned to $0.0 —
    subscription billing has no marginal per-call cost, and an explicit zero
    (vs NULL) marks "measured: free" in spend reports and budget sums.
    Defensive throughout: junk stats yield zero counts, never a raise.
    """
    prompt_tokens = 0
    candidate_tokens = 0
    cached_tokens = 0
    stats = payload.get("stats")
    if isinstance(stats, dict):
        models = cast("dict[str, object]", stats).get("models")
        if isinstance(models, dict):
            for per_model in cast("dict[str, object]", models).values():
                if not isinstance(per_model, dict):
                    continue
                tokens = cast("dict[str, object]", per_model).get("tokens")
                if not isinstance(tokens, dict):
                    continue
                tokens_dict = cast("dict[str, object]", tokens)
                prompt_tokens += _token_count(tokens_dict, "prompt")
                candidate_tokens += _token_count(tokens_dict, "candidates")
                cached_tokens += _token_count(tokens_dict, "cached")
    return {
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": candidate_tokens,
            "cache_read_input_tokens": cached_tokens,
        },
        "total_cost_usd": 0.0,
    }


def _token_count(tokens: dict[str, object], key: str) -> int:
    """One numeric token field, defensively (bool is an int subclass)."""
    v = tokens.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def _classify_subprocess_failure(exc: subprocess.CalledProcessError) -> None:
    """Promote an auth-shaped CLI failure to LLMSetupError (hard stop).

    A missing/expired consumer login is deterministic and operator-actionable
    — every retry fails identically until someone runs the interactive login
    — so it must classify as setup (propagate loudly), not transient
    (degrade + retry), exactly like a missing binary on the Claude path.
    Non-auth failures return None and stay on the operational path.
    """
    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    combined = stdout + "\n" + stderr
    if any(marker in combined for marker in _AUTH_ERROR_MARKERS):
        raise llm_cli.LLMSetupError(
            f"Gemini CLI authentication failed (exit {exc.returncode}). {GEMINI_LOGIN_HINT}\n"
            f"CLI output: {combined.strip()[:300]}"
        ) from exc


def call_gemini(
    prompt: str,
    model: str | None = None,
    timeout_seconds: int | None = None,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """Single-shot Gemini CLI call. Same contract as ``llm.cli._call_claude``:
    per-purpose budget gate up front, prompt via stdin, UTF-8 forced, one
    best-effort ``llm_calls`` ledger row per attempt (success or failure;
    rows are distinguishable by the ``gemini-`` model prefix with
    fallback_used NULL — fallback-path rows carry fallback_used='gemini').

    No fallback wiring lives here: routing policy (when a Gemini failure
    falls back to Claude) belongs to ``call_llm``. Raises:
      * LLMBudgetExceeded — hard per-purpose cap (propagate; budget gate).
      * LLMSetupError — binary missing or consumer login absent/expired.
      * RuntimeError / ValueError / subprocess errors — operational failures
        the caller may degrade or reroute.
    """
    llm_cli._enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_gemini_setup_once()  # setup errors propagate
    assert _gemini_cli_path is not None  # set by _verify_gemini_setup_once
    resolved_model = model or gemini_model_for(purpose)
    resolved_timeout = timeout_seconds or GEMINI_BACKEND_TIMEOUT_SECONDS
    log.info(
        {
            "event": "gemini_backend_call_start",
            "model": resolved_model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import sha256_text

    prompt_sha = sha256_text(prompt)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        # Prompt rides stdin alone — gemini-cli enters non-interactive mode
        # on piped stdin without -p (verified v0.46), and stdin dodges the
        # Windows 32K CreateProcess command-line limit like the Claude path.
        result = subprocess.run(
            [_gemini_cli_path, "-o", "json", "-m", resolved_model],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",  # Force UTF-8 — Windows otherwise defaults to cp1252 which dies on
            errors="replace",  # common financial-doc Unicode (U+2212 minus, en/em dashes, arrows).
            check=True,
            timeout=resolved_timeout,
            cwd=_gemini_neutral_cwd(),
            env=_gemini_subprocess_env(),
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        text, meta = parse_gemini_json_output(result.stdout.strip())
        text = text.strip()
        if not text:
            raise RuntimeError(
                f"gemini -o json returned empty `response`. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "gemini_backend_call_done", "response_chars": len(text)})
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            response_text=text,
            meta=usage_meta_from_gemini_stats(meta),
        )
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as gemini_error:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"{type(gemini_error).__name__}: {str(gemini_error)[:500]}",
        )
        if isinstance(gemini_error, subprocess.CalledProcessError):
            _classify_subprocess_failure(gemini_error)  # auth → LLMSetupError
        raise
