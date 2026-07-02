"""Deterministic slash commands — handled before any LLM, from either chat
surface (report drawer or Ask tab). No model call, no budget, instant reply.

Moved from execution/comments_server.py (P5.4) when the two chat stacks
merged into the ask engine; the engine intercepts these in its command
route, so both entry points get them. Unknown slash-prefixed messages still
fall through to the narrative LLM (returns None), matching the original
endpoint behavior.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dispatch_registry import Registry, RegistryConflict

_HELP_TEXT = (
    "Commands handled instantly (no LLM):\n"
    "- /review <TICKER> [at $PRICE] — grounded should-I-trim read: weight, break-rules, "
    "valuation ladder, sizing, tax cost of the trim\n"
    "- /discovery list — top live new-name candidates with why-surfaced\n"
    "- /discovery queue <TICKER> | /discovery dismiss <TICKER> [why...]\n"
    "- /discovery build <TICKER> — start the eval build (~25 min + LLM spend)\n"
    "- /view <question> — force a live data view (compile + run, no prose)\n"
    "- /help — this list\n"
    "Anything else goes to the assistant; metric-shaped questions render as "
    "live data views automatically."
)

COMMAND_PREFIXES: tuple[str, ...] = ("/discovery", "/help", "/review")

# "at $70" / "above 70" / "over $12.50" -> the price level to review at.
_AT_PRICE_RX = re.compile(r"(?:at|above|over)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def run_chat_command(repo_root: Path, message: str, registry: Registry) -> str | None:
    """Dispatch one deterministic command. Returns the reply text, or None
    when the message isn't a recognized command (the engine then routes it
    normally)."""
    text = message.strip()
    low = text.lower()
    if low.startswith("/help"):
        return _HELP_TEXT
    if low.startswith("/review"):
        return _review_command(repo_root, text)
    if low.startswith("/discovery"):
        return _discovery_command(repo_root, text, registry)
    return None


def _parse_at_price(text: str) -> float | None:
    match = _AT_PRICE_RX.search(text)
    return float(match.group(1)) if match else None


def _review_command(repo_root: Path, text: str) -> str:
    """``/review <TICKER> [at $PRICE]`` — the instant, no-LLM position read.

    Returns the deterministic pre-analysis (weight, break-rule status, DCF ladder
    verdict, sizing) plus a mechanical trim/hold read. The full LLM-calibrated
    verdict (with the behavioral guard) is a separate, slower path — pointed to in
    the reply — so this command stays instant and budget-free.
    """
    from advisor.position_review import build_pre_analysis, render_pre_analysis_chat

    parts = text.split()
    if len(parts) < 2:
        return "Usage: /review <TICKER> [at $PRICE] — e.g. `/review RBRK` or `/review FLKR at $70`."
    ticker = parts[1].upper().lstrip("$")
    at_price = _parse_at_price(text)
    db_path = repo_root / "data" / "portfolio.db"
    try:
        pre = build_pre_analysis(repo_root, ticker, at_price=at_price, db_path=db_path)
    except Exception as exc:
        return f"Couldn't build a review for {ticker}: {type(exc).__name__}: {exc}"
    return render_pre_analysis_chat(pre)


def _discovery_command(repo_root: Path, text: str, registry: Registry) -> str:
    """Deterministic ``/discovery`` chat commands (P5.4).

    /discovery list             — top live candidates with why-surfaced
    /discovery queue <T>        — mark a candidate queued
    /discovery dismiss <T> [why] — dismiss (stays dismissed across re-runs); a
                                  trailing reason records it as a gradeable avoid
    /discovery build <T>        — start the eval build job (the approval)
    """
    from discovery.store import BUILDABLE_STATUSES, list_candidates, set_status

    db_path = repo_root / "data" / "portfolio.db"
    parts = text.split()
    verb = parts[1].lower() if len(parts) > 1 else "list"
    arg = parts[2].upper() if len(parts) > 2 else ""
    usage = (
        "Usage: /discovery list | /discovery queue <TICKER> | "
        "/discovery dismiss <TICKER> | /discovery build <TICKER>. "
        "The full queue lives under Research -> Discovery."
    )
    try:
        live = list_candidates(db_path=db_path)
    except Exception:
        return "The discovery queue isn't available (table missing?). " + usage
    by_ticker = {c.ticker: c for c in live}

    if verb == "list":
        if not live:
            return (
                "The discovery queue is empty — run the pipelines from "
                "Research -> Discovery (Run discovery)."
            )
        lines = [
            f"{c.ticker} (score {c.score:g}, {c.status}) — "
            + "; ".join(str(e.get("detail") or "") for e in c.evidence[:2])
            for c in live[:10]
        ]
        more = f"\n...and {len(live) - 10} more in Research -> Discovery." if len(live) > 10 else ""
        return "Top discovery candidates:\n" + "\n".join(lines) + more

    if verb in ("queue", "dismiss"):
        if not arg:
            return usage
        cand = by_ticker.get(arg)
        if cand is None:
            return f"{arg} isn't in the live discovery queue. " + usage
        new_status = "queued" if verb == "queue" else "dismissed"
        set_status(cand.id, new_status, db_path=db_path)
        # `/discovery dismiss T <reason...>` records the pass as a first-class,
        # gradeable AVOID decision (L11) so a name you passed leaves a trace.
        reason = " ".join(parts[3:]).strip() if verb == "dismiss" and len(parts) > 3 else ""
        if reason:
            from pass_decisions import LENS_DISCOVERY_DISMISSAL, record_pass_decision

            result = record_pass_decision(
                ticker=arg,
                reason=reason,
                source_dismissal_id=cand.id,
                source_lens=LENS_DISCOVERY_DISMISSAL,
                db_path=db_path,
            )
            if result is not None:
                return (
                    f"{arg} -> dismissed, and recorded as an avoid decision "
                    f"(#{result.decision_id}) — it will be graded against what it does next."
                )
        return f"{arg} -> {new_status}."

    if verb == "build":
        if not arg:
            return usage
        cand = by_ticker.get(arg)
        if cand is None or cand.status not in BUILDABLE_STATUSES:
            return (
                f"{arg} isn't buildable (must be a live candidate in new/queued status). " + usage
            )
        argv = [
            sys.executable,
            str(repo_root / "execution" / "discovery_build.py"),
            "--tickers",
            arg,
            "--repo-root",
            str(repo_root),
        ]
        try:
            job = registry.start(ticker=arg, kind="discovery-build", argv=argv)
        except RegistryConflict as exc:
            return f"Can't start the build: {exc}"
        return (
            f"Eval build started for {arg} (job {job.job_id}, ~25 min + LLM spend). "
            "Watch it under Research -> Discovery."
        )

    return usage


__all__ = ["COMMAND_PREFIXES", "run_chat_command"]
