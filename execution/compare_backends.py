"""Run the same prompt through the Claude and Gemini backends, side by side.

The experiment hook for the eval-gated Gemini backend (src/llm/gemini_backend.py):
produces the paired-output corpus the LLM-evals judges grade before any purpose
is added to GEMINI_BACKEND_ALLOWED_PURPOSES. Each prompt is sent through BOTH
backends via the canonical ``call_llm`` entry point with an explicit
``backend=`` force — so budget gating, model resolution, and llm_calls ledger
rows behave exactly as production calls do — and one JSONL record per prompt
captures both responses, latencies, models, and errors.

Usage:
    # Built-in smoke: 3 cheap real-purpose prompts (viewspec_compile x2,
    # transcript_metadata x1) through both backends.
    python execution/compare_backends.py --smoke

    # One ad-hoc prompt for a given purpose.
    python execution/compare_backends.py --purpose bear_case --prompt-file p.txt --ticker NU
    python execution/compare_backends.py --purpose viewspec_compile --prompt "..." --label q1

Output: JSONL under <repo-root>/data/backend_compare/ (or --out). One record
per prompt:

    {"run_id": ..., "recorded_at": ..., "purpose": ..., "label": ...,
     "ticker": ..., "prompt_sha256": ..., "prompt_chars": ..., "prompt": ...,
     "expected": ...,            # smoke prompts only — judge ground truth
     "claude": {"model", "ok", "elapsed_ms", "response", "error"},
     "gemini": {"model", "ok", "elapsed_ms", "response", "error"}}

A failed backend records its error string and the run CONTINUES — before the
one-time consumer login (see directives/gemini_backend.md) every gemini record
carries the LLMSetupError login hint, which is itself useful output: it proves
the guard fires and documents the operator action.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from llm.cli import DEFAULT_MODEL, LLM_MODELS, call_llm  # noqa: E402
from llm.gemini_backend import gemini_model_for  # noqa: E402
from llm_call_ledger import sha256_text  # noqa: E402

log = logging.getLogger("compare_backends")

BACKENDS = ("claude", "gemini")

# --- Smoke prompts: cheap REAL purposes -------------------------------------
#
# viewspec_compile uses the real prompt builder (viewspec.nl_compile._build_prompt
# — pure function: query + vocabulary + defaults -> prompt) over a small fixed
# vocabulary, so the smoke is the production prompt shape without a DB
# dependency. transcript_metadata replicates the inline template from
# llm_client.identify_transcript_metadata (kept in sync by eye — it's three
# sentences; the smoke is an experiment harness, not a contract test).

_SMOKE_VIEWSPEC_VOCAB = """\
# fin tokens
fin:revenue
fin:gross_profit
fin:operating_income
fin:net_income
fin:free_cash_flow
# kpi tokens
kpi:total_customers
kpi:monthly_arpac
# seg tokens
seg:product:cloud:revenue"""

_SMOKE_TRANSCRIPT_PROMPT = """
    Analyze the following text from an earnings call transcript cover page or header.
    Identify the:
    1. Company Ticker (e.g., NVDA, GOOGL, MSFT).
       **IMPORTANT**: Always use the **Primary US Listing Ticker** (NYSE/NASDAQ) if available.
       - Example: For "Taiwan Semiconductor" or "2330.TW", return "TSM".
       - Example: For "Tencent" or "700.HK", return "TCEHY".
    2. Fiscal Quarter (Q1, Q2, Q3, or Q4).
    3. Fiscal Year (e.g., 2025).

    Return the result in this STRICT format:
    TICKER_QX_YYYY

    Example: NVDA_Q1_2026

    If you cannot identify the information with confidence, return "UNKNOWN".

    Text:
    ServiceNow, Inc. (NYSE: NOW)
    Q1 2026 Earnings Conference Call
    April 22, 2026, 5:00 PM ET
    Company Participants: Bill McDermott - Chairman & CEO; Gina Mastantuono - President & CFO
"""


def _smoke_prompts() -> list[dict[str, object]]:
    """The built-in golden set: (purpose, label, prompt, expected) tuples."""
    from viewspec.nl_compile import _build_prompt  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "purpose": "viewspec_compile",
            "label": "viewspec_yoy_two_names",
            "prompt": _build_prompt(
                "NU and MELI revenue growth yoy over the last 8 quarters",
                _SMOKE_VIEWSPEC_VOCAB,
                ["NU", "MELI"],
            ),
            "expected": (
                'ViewSpec JSON: tickers ["NU","MELI"], metrics ["fin:revenue"], '
                'transform "yoy", cadence "quarterly", periods 8'
            ),
        },
        {
            "purpose": "viewspec_compile",
            "label": "viewspec_margin_annual",
            "prompt": _build_prompt(
                "show VEEV operating income as % of revenue, annual, last 5 fiscal years",
                _SMOKE_VIEWSPEC_VOCAB,
                ["VEEV"],
            ),
            "expected": (
                'ViewSpec JSON: tickers ["VEEV"], metrics ["fin:operating_income"], '
                'transform "margin", cadence "annual", periods 5'
            ),
        },
        {
            "purpose": "transcript_metadata",
            "label": "transcript_header_now",
            "prompt": _SMOKE_TRANSCRIPT_PROMPT,
            "expected": "NOW_Q1_2026",
        },
    ]


def _claude_model_for_purpose(purpose: str | None, override: str | None) -> str:
    if override:
        return override
    if purpose is None:
        return DEFAULT_MODEL
    return LLM_MODELS.get(purpose, DEFAULT_MODEL)


def _run_one_backend(
    backend: str,
    prompt: str,
    *,
    purpose: str | None,
    ticker: str | None,
    run_id: str,
    model: str | None,
    timeout_seconds: int | None,
    force_budget_bypass: bool,
) -> dict[str, object]:
    """One backend call -> a result dict. Never raises: errors are data here
    (the whole point is recording how each backend behaved on this prompt)."""
    t0 = time.monotonic()
    try:
        response = call_llm(
            prompt,
            purpose=purpose,
            model=model,
            timeout_seconds=timeout_seconds,
            ticker=ticker,
            scope="backend_compare",
            run_id=run_id,
            force_budget_bypass=force_budget_bypass,
            backend=backend,
        )
        return {
            "ok": True,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "response": response,
            "error": None,
        }
    except Exception as exc:  # every failure mode is a recordable outcome here
        return {
            "ok": False,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "response": None,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }


def compare_prompt(
    prompt: str,
    *,
    purpose: str | None,
    label: str,
    ticker: str | None,
    run_id: str,
    claude_model: str | None,
    gemini_model: str | None,
    timeout_seconds: int | None,
    force_budget_bypass: bool,
    expected: object = None,
) -> dict[str, object]:
    """Run one prompt through both backends and assemble the JSONL record."""
    record: dict[str, object] = {
        "run_id": run_id,
        # Repo convention: naive-UTC stamps (see project_naive_utc_datetime_convention).
        "recorded_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "purpose": purpose,
        "label": label,
        "ticker": ticker,
        "prompt_sha256": sha256_text(prompt),
        "prompt_chars": len(prompt),
        "prompt": prompt,
    }
    if expected is not None:
        record["expected"] = expected
    resolved = {
        "claude": _claude_model_for_purpose(purpose, claude_model),
        "gemini": gemini_model or gemini_model_for(purpose),
    }
    for backend in BACKENDS:
        result = _run_one_backend(
            backend,
            prompt,
            purpose=purpose,
            ticker=ticker,
            run_id=run_id,
            model=claude_model if backend == "claude" else gemini_model,
            timeout_seconds=timeout_seconds,
            force_budget_bypass=force_budget_bypass,
        )
        record[backend] = {"model": resolved[backend], **result}
        status = "ok" if result["ok"] else "FAILED"
        log.info(
            "  %s [%s] %s in %sms%s",
            backend,
            resolved[backend],
            status,
            result["elapsed_ms"],
            "" if result["ok"] else f" — {result['error']}",
        )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the same prompt through the Claude and Gemini backends, side by side."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--smoke", action="store_true", help="run the built-in golden set")
    source.add_argument("--prompt", help="inline prompt text")
    source.add_argument("--prompt-file", type=Path, help="read the prompt from a file (UTF-8)")
    parser.add_argument("--purpose", help="purpose key for model resolution + budget + ledger")
    parser.add_argument("--label", default="adhoc", help="short name for this prompt in the record")
    parser.add_argument("--ticker", help="optional ticker for ledger attribution")
    parser.add_argument("--claude-model", help="override the Claude model id")
    parser.add_argument("--gemini-model", help="override the Gemini model id")
    parser.add_argument("--timeout", type=int, default=None, help="per-call timeout seconds")
    parser.add_argument("--out", type=Path, default=None, help="output JSONL path")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="repo root whose data/ receives the JSONL + whose DB takes the ledger rows",
    )
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help="skip per-purpose budget caps for this run (experiment escape hatch)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        # Re-point db.DB_PATH so the best-effort llm_calls ledger rows land in
        # the chosen repo's DB (the ledger resolves from the db module global).
        import db

        db.set_db_path(db_path)

    if args.smoke:
        prompts = _smoke_prompts()
    else:
        if args.purpose is None:
            parser.error("--purpose is required with --prompt/--prompt-file")
        text = (
            args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        )
        prompts = [{"purpose": args.purpose, "label": args.label, "prompt": text}]

    run_id = uuid.uuid4().hex
    out_path = args.out or (repo_root / "data" / "backend_compare" / f"compare_{run_id[:8]}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for item in prompts:
        purpose = str(item["purpose"]) if item["purpose"] is not None else None
        label = str(item["label"])
        log.info("[%s] purpose=%s (%s chars)", label, purpose, len(str(item["prompt"])))
        records.append(
            compare_prompt(
                str(item["prompt"]),
                purpose=purpose,
                label=label,
                ticker=args.ticker,
                run_id=run_id,
                claude_model=args.claude_model,
                gemini_model=args.gemini_model,
                timeout_seconds=args.timeout,
                force_budget_bypass=args.force_budget_bypass,
                expected=item.get("expected"),
            )
        )

    with out_path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Human summary: one line per prompt per backend.
    log.info("")
    log.info("run_id=%s  ->  %s", run_id, out_path)
    failures = 0
    for record in records:
        for backend in BACKENDS:
            raw = record[backend]
            assert isinstance(raw, dict)  # built by compare_prompt above
            result = cast("dict[str, object]", raw)
            ok = bool(result["ok"])
            if not ok:
                failures += 1
            log.info(
                "%-28s %-7s %-22s %-6s %6sms  %s",
                record["label"],
                backend,
                result["model"],
                "ok" if ok else "FAIL",
                result["elapsed_ms"],
                (str(result["response"])[:60].replace("\n", " ") + "…")
                if ok
                else str(result["error"])[:80],
            )
    log.info("%d prompt(s), %d backend failure(s)", len(records), failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
