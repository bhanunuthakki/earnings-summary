"""Deterministic slash commands — handled before any LLM, from either chat
surface (report drawer or Ask tab). No model call, no budget, instant reply.

Moved from execution/comments_server.py (P5.4) when the two chat stacks
merged into the ask engine; the engine intercepts these in its command
route, so both entry points get them. Unknown slash-prefixed messages still
fall through to the narrative LLM (returns None), matching the original
endpoint behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dispatch_registry import Registry, RegistryConflict

_HELP_TEXT = (
    "Commands handled instantly (no LLM):\n"
    "- /discovery list — top live new-name candidates with why-surfaced\n"
    "- /discovery queue <TICKER> | /discovery dismiss <TICKER>\n"
    "- /discovery build <TICKER> — start the eval build (~25 min + LLM spend)\n"
    "- /view <question> — force a live data view (compile + run, no prose)\n"
    "- /help — this list\n"
    "Anything else goes to the assistant; metric-shaped questions render as "
    "live data views automatically."
)

COMMAND_PREFIXES: tuple[str, ...] = ("/discovery", "/help")


def run_chat_command(repo_root: Path, message: str, registry: Registry) -> str | None:
    """Dispatch one deterministic command. Returns the reply text, or None
    when the message isn't a recognized command (the engine then routes it
    normally)."""
    text = message.strip()
    low = text.lower()
    if low.startswith("/help"):
        return _HELP_TEXT
    if low.startswith("/discovery"):
        return _discovery_command(repo_root, text, registry)
    return None


def _discovery_command(repo_root: Path, text: str, registry: Registry) -> str:
    """Deterministic ``/discovery`` chat commands (P5.4).

    /discovery list           — top live candidates with why-surfaced
    /discovery queue <T>      — mark a candidate queued
    /discovery dismiss <T>    — dismiss (stays dismissed across re-runs)
    /discovery build <T>      — start the eval build job (the approval)
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
