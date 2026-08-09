"""Deterministic slash commands — handled before any LLM, from either chat
surface (report drawer or Ask tab). No model call, no budget, instant reply.

Moved from execution/comments_server.py (P5.4) when the two chat stacks
merged into the ask engine; the engine intercepts these in its command
route, so both entry points get them. Unknown slash-prefixed messages still
fall through to the narrative LLM (returns None), matching the original
endpoint behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

from dispatch_registry import Registry, RegistryConflict
from runtime.python_process import managed_python_prefix

_HELP_TEXT = (
    "Commands handled instantly (no LLM):\n"
    "- /review <TICKER> [at $PRICE] — instant grounded should-I-trim read (weight, "
    "break-rules, valuation ladder, sizing, tax cost of the trim), then kicks off the "
    "full governed verdict (LLM + behavioral guard) in the background and persists it\n"
    "- /discovery list — top live new-name candidates with why-surfaced\n"
    "- /discovery queue <TICKER> | /discovery dismiss <TICKER> [why...]\n"
    "- /discovery build <TICKER> — start the eval build (~25 min + LLM spend)\n"
    "- /view <question> — force a live data view (compile + run, no prose)\n"
    "- /help — this list\n"
    "Anything else goes to the assistant; metric-shaped questions render as "
    "live data views automatically."
)

COMMAND_PREFIXES: tuple[str, ...] = ("/discovery", "/help", "/review")

# Kill switch for the Slice-2 full-verdict follow-up job (default ON). Set
# REVIEW_FULL_VERDICT=0 to keep /review purely the instant, no-LLM Slice-1
# reply — e.g. while iterating on the prompt without spending budget on every
# manual test, or if the background job ever needs a quick global disable.
_FULL_VERDICT_OFF = {"0", "false", "no", "off"}


def _full_verdict_enabled() -> bool:
    return os.environ.get("REVIEW_FULL_VERDICT", "1").strip().lower() not in _FULL_VERDICT_OFF


def run_chat_command(repo_root: Path, message: str, registry: Registry | None) -> str | None:
    """Dispatch one deterministic command. Returns the reply text, or None
    when the message isn't a recognized command (the engine then routes it
    normally)."""
    text = message.strip()
    low = text.lower()
    if low.startswith("/help"):
        return _HELP_TEXT
    if low.startswith("/review"):
        return _review_command(repo_root, text, registry)
    if low.startswith("/discovery"):
        return (
            _discovery_command(repo_root, text, registry)
            if registry is not None
            else ("Commands aren't available on this surface — try /help in the report chat.")
        )
    return None


def _start_full_verdict_job(
    repo_root: Path, ticker: str, at_price: float | None, registry: Registry
) -> str:
    """Kick off the governed Slice-2 review (LLM verdict + behavioral guard +
    persisted ``advisor_memos`` row) as a background subprocess over the
    existing action/SSE plumbing (``dispatch_registry.Registry`` — the same
    single-flight job runner every dashboard action button uses).

    Best-effort: a busy slot (``RegistryConflict`` — a review for this ticker
    is already running) or any other start failure degrades to a one-line
    note rather than breaking the instant reply that already rendered.
    """
    argv = [
        *managed_python_prefix(repo_root),
        str(repo_root / "execution" / "review_position.py"),
        ticker,
        "--verdict",
        # An owner-typed /review in the app — tag it 'doorway' so the persisted
        # memo counts in the Coach P&L (the CLI defaults to 'agent' otherwise).
        "--source",
        "doorway",
    ]
    if at_price is not None:
        argv.extend(["--at-price", str(at_price)])
    try:
        job = registry.start(ticker=ticker, kind="position-review-verdict", argv=argv)
    except RegistryConflict:
        return (
            f"\n\n_A full verdict for {ticker} is already running in the background — "
            "check Research -> Jobs._"
        )
    except Exception as exc:  # never let the background kickoff break the instant reply
        return f"\n\n_Couldn't start the full verdict in the background: {type(exc).__name__}._"
    return (
        f"\n\n_Full calibrated verdict (LLM + behavioral guard) started in the background "
        f"(job {job.job_id}) — it will persist as an advisor memo when done. "
        "Check Research -> Jobs, or run `python execution/review_position.py "
        f"{ticker} --verdict` yourself for the same result synchronously._"
    )


def _review_command(repo_root: Path, text: str, registry: Registry | None) -> str:
    """``/review <TICKER> [at $PRICE]`` — instant, no-LLM position read, then
    (by default) the full governed verdict kicked off in the background.

    The instant reply is always the deterministic pre-analysis (weight,
    break-rule status, DCF ladder verdict, sizing) plus a mechanical
    trim/hold read — delegating to ``advisor.position_review.review_reply_text``,
    the ONE parser for this command shape, shared with the Telegram poller's
    ``/review`` interception so the at-price regex and reply copy never drift
    between the two chat surfaces.

    On a surface with a job registry (the Ask tab / report drawer — not the
    Telegram poller, which has no Flask job registry to spawn against) and
    with the ``REVIEW_FULL_VERDICT`` kill switch on (the default), this ALSO
    starts the governed ``review_position()`` path — LLM verdict, deterministic
    behavioral guard, persisted ``advisor_memos`` row — as a background job over
    the same action/SSE plumbing every dashboard action button uses, so the
    coaching surface actually persists a scoreable memo instead of only ever
    rendering the free pre-analysis.
    """
    from advisor.position_review import resolve_review_target, review_reply_text

    reply = review_reply_text(repo_root, text)
    if registry is None or not _full_verdict_enabled():
        return reply
    parsed = resolve_review_target(repo_root, text)
    if parsed is None:
        return reply  # usage message — nothing to review
    ticker, at_price = parsed
    return reply + _start_full_verdict_job(repo_root, ticker, at_price, registry)


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
            *managed_python_prefix(repo_root),
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
