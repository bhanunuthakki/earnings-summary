"""One-click approve / dismiss for queued actions and their parent alerts.

The dashboard's queued-action cards render ``<a href="/approve?...">``
links plus a "copy and run this" CLI hint. This module is both halves:
the CLI the hint invokes, and the shared core (``approve_and_apply`` /
``dismiss_action``) behind the live server's ``GET /approve`` route
(execution/comments_server.py) — one code path, identical semantics.

Modes (mutually exclusive — pass exactly one selector):

  --action-id N                    approve and apply queued_action N,
                                   then write the downstream ledger /
                                   sizing-intent row
  --action-id N --dismiss          cancel queued_action N; no ledger
  --alert-id N --dismiss-alert     dismiss alert N and cancel all of
                                   its pending queued actions

Exit codes:
  0 on success.
  1 on missing IDs, status conflicts (already applied / dismissed),
    or any other surfaced precondition failure.

The ledger/sizing dispatch is keyed by ``action_kind``:

  thesis_update         → thesis_ledger_entries(entry_kind='thesis_update')
  bear_append           → thesis_ledger_entries(entry_kind='bear_append')
  earnings_prep_append  → thesis_ledger_entries(entry_kind='earnings_prep_append')
  sizing_update         → position_sizing_intent row (NOT the ledger)

For sizing_update the payload is expected to carry intent_kind /
intent_value / narrative; the ledger family carries body + optional
title. Missing required keys cause a clear failure rather than a
silent partial write.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from alerts import (  # noqa: E402
    ACTION_STATUS_PENDING,
    QueuedActionRow,
    apply_action,
    cancel_action,
    dismiss_alert,
    get_action,
    get_alert,
    list_queued_actions_for_alert,
)
from user_state.ledger import ThesisLedgerEntryRow, append_entry  # noqa: E402
from user_state.sizing import PositionSizingIntentRow, append_intent  # noqa: E402

# action_kind → entry_kind for the ledger-family kinds. sizing_update is
# routed elsewhere (it writes to position_sizing_intent, not the ledger).
_ACTION_TO_ENTRY_KIND: dict[str, str] = {
    "thesis_update": "thesis_update",
    "bear_append": "bear_append",
    "earnings_prep_append": "earnings_prep_append",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action-id",
        type=int,
        default=None,
        help="ID of the queued_action to approve (or dismiss with --dismiss).",
    )
    parser.add_argument(
        "--alert-id",
        type=int,
        default=None,
        help="ID of the alert to dismiss (requires --dismiss-alert).",
    )
    parser.add_argument(
        "--dismiss",
        action="store_true",
        help="Cancel the action instead of applying it (with --action-id).",
    )
    parser.add_argument(
        "--dismiss-alert",
        action="store_true",
        help="Dismiss the alert and cancel all its pending actions (with --alert-id).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db. Defaults to the worktree root.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the DB path (else uses data/portfolio.db under --repo-root).",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.repo_root, args.db_path)

    try:
        if args.dismiss_alert:
            if args.alert_id is None:
                _err("--dismiss-alert requires --alert-id N")
                return 1
            return _do_dismiss_alert(args.alert_id, db_path)
        if args.action_id is None:
            _err(
                "Pass exactly one of: --action-id N (approve+apply), "
                "--action-id N --dismiss (cancel), or "
                "--alert-id N --dismiss-alert (dismiss whole alert)."
            )
            return 1
        if args.dismiss:
            return _do_cancel_action(args.action_id, db_path)
        return _do_approve_action(args.action_id, db_path)
    except (LookupError, ValueError, KeyError) as exc:
        _err(str(exc))
        return 1


# ----------------------------------------------------------------------------
# Shared core — called by the CLI handlers below AND by the dashboard's
# GET /approve route (execution/comments_server.py), so the web links and
# the CLI hint can never diverge on semantics.
# ----------------------------------------------------------------------------


def approve_and_apply(action_id: int, db_path: Path | None = None) -> str:
    """Approve queued_action ``action_id``: write its downstream user-state
    row, then mark it 'applied'. Returns a one-line summary.

    Order of operations:
      1. Fetch the queued_action (raises LookupError if missing).
      2. Dispatch on ``action_kind`` to write the downstream row
         (thesis_ledger_entries or position_sizing_intent). Done BEFORE
         apply_action so a downstream-write failure leaves the queued
         action 'pending' (the user can retry).
      3. Mark the queued_action 'applied' via apply_action.

    Raises LookupError (unknown id), ValueError (already applied/cancelled,
    or unsupported action_kind), KeyError (malformed payload).
    """
    qa = _fetch_action_or_raise(action_id, db_path)

    if qa.status != ACTION_STATUS_PENDING:
        # Surface the status conflict BEFORE the downstream dispatch. The
        # ledger / sizing tables are append-only, so writing first would turn
        # a stale double-click into a duplicate downstream row that
        # apply_action's own conflict check can no longer prevent.
        raise ValueError(
            f"queued_actions id={qa.id} status is {qa.status!r}, "
            f"cannot transition (expected {ACTION_STATUS_PENDING!r})"
        )

    if qa.action_kind == "sizing_update":
        intent = _write_sizing_intent(qa, db_path)
        applied = apply_action(action_id, db_path=db_path)
        return (
            f"Approved queued_action id={applied.id} (action_kind=sizing_update). "
            f"position_sizing_intent id={intent.id} written: "
            f"{intent.ticker} · {intent.intent_kind} · "
            f"value={intent.intent_value}."
        )

    if qa.action_kind in _ACTION_TO_ENTRY_KIND:
        entry = _write_ledger_entry(qa, db_path)
        applied = apply_action(action_id, db_path=db_path)
        return (
            f"Approved queued_action id={applied.id} (action_kind={applied.action_kind!r}). "
            f"Ledger entry id={entry.id} written: "
            f"{entry.ticker} · {entry.entry_kind} · "
            f"source_alert_id={entry.source_alert_id}."
        )

    raise ValueError(
        f"queued_action id={action_id} has unsupported action_kind="
        f"{qa.action_kind!r}; expected one of: "
        f"{sorted({'sizing_update', *_ACTION_TO_ENTRY_KIND.keys()})}"
    )


def dismiss_action(action_id: int, db_path: Path | None = None) -> str:
    """Cancel queued_action ``action_id`` (the card's 'dismiss' link / the
    CLI's ``--dismiss``). No downstream row is written. Returns a one-line
    summary. Raises LookupError (unknown id) / ValueError (already
    applied/cancelled)."""
    qa_before = _fetch_action_or_raise(action_id, db_path)
    cancelled = cancel_action(action_id, db_path=db_path)
    return (
        f"Cancelled queued_action id={cancelled.id} "
        f"(was {qa_before.status!r}, now {cancelled.status!r}). "
        "No ledger entry written."
    )


# ----------------------------------------------------------------------------
# Mode handlers
# ----------------------------------------------------------------------------


def _do_approve_action(action_id: int, db_path: Path | None) -> int:
    sys.stdout.write(approve_and_apply(action_id, db_path=db_path) + "\n")
    return 0


def _do_cancel_action(action_id: int, db_path: Path | None) -> int:
    sys.stdout.write(dismiss_action(action_id, db_path=db_path) + "\n")
    return 0


def _do_dismiss_alert(alert_id: int, db_path: Path | None) -> int:
    """Dismiss the alert and cancel all of its pending queued actions.

    Cancels each pending action first so a partial failure mid-cancel
    leaves the parent alert still 'pending' (callers can re-run).
    Already-applied / already-cancelled actions are skipped.
    """
    actions = list_queued_actions_for_alert(alert_id, db_path=db_path)
    cancelled_ids: list[int] = []
    skipped_ids: list[int] = []
    for qa in actions:
        if qa.status == "pending":
            cancel_action(qa.id, db_path=db_path)
            cancelled_ids.append(qa.id)
        else:
            skipped_ids.append(qa.id)
    dismissed = dismiss_alert(alert_id, db_path=db_path)
    sys.stdout.write(
        f"Dismissed alert id={dismissed.id} ({dismissed.ticker} · "
        f"{dismissed.trigger_kind}). "
        f"Cancelled {len(cancelled_ids)} pending action(s)"
    )
    if cancelled_ids:
        sys.stdout.write(f": {cancelled_ids}")
    if skipped_ids:
        sys.stdout.write(
            f"; skipped {len(skipped_ids)} non-pending action(s): {skipped_ids}"
        )
    sys.stdout.write(".\n")
    return 0


# ----------------------------------------------------------------------------
# Payload-shape adapters
# ----------------------------------------------------------------------------


def _write_ledger_entry(
    qa: QueuedActionRow, db_path: Path | None
) -> ThesisLedgerEntryRow:
    """Write one thesis_ledger_entries row from a queued_action payload.

    Payload shape (drafter-owned):
      {
        "body": "Cloud margin ...",  # required
      }

    ``ticker`` comes from the parent alert, not the payload — see
    ``_resolve_ticker``.
    """
    entry_kind = _ACTION_TO_ENTRY_KIND[qa.action_kind]
    ticker = _resolve_ticker(qa, db_path)
    body = _require_str(qa.payload, "body", qa)
    return append_entry(
        ticker=ticker,
        entry_kind=entry_kind,
        body=body,
        source_alert_id=qa.alert_id,
        db_path=db_path,
    )


def _write_sizing_intent(
    qa: QueuedActionRow, db_path: Path | None
) -> PositionSizingIntentRow:
    """Write one position_sizing_intent row from a queued_action payload.

    Payload shape (expected):
      {
        "intent_kind":  "target_pct", # required (e.g. target_pct / max_pct / note)
        "intent_value": 0.05,         # optional
        "narrative":    "..."         # optional
      }

    ``ticker`` comes from the parent alert, not the payload — see
    ``_resolve_ticker``.
    """
    ticker = _resolve_ticker(qa, db_path)
    intent_kind = _require_str(qa.payload, "intent_kind", qa)
    raw_value = qa.payload.get("intent_value")
    intent_value: float | None
    if raw_value is None:
        intent_value = None
    elif isinstance(raw_value, (int, float)):
        intent_value = float(raw_value)
    else:
        raise KeyError(
            f"queued_action id={qa.id} payload.intent_value must be a number "
            f"or null; got {type(raw_value).__name__}"
        )
    raw_narrative = qa.payload.get("narrative")
    narrative: str | None
    if raw_narrative is None:
        narrative = None
    elif isinstance(raw_narrative, str):
        narrative = raw_narrative
    else:
        raise KeyError(
            f"queued_action id={qa.id} payload.narrative must be a string or null; "
            f"got {type(raw_narrative).__name__}"
        )
    return append_intent(
        ticker=ticker,
        intent_kind=intent_kind,
        intent_value=intent_value,
        narrative=narrative,
        db_path=db_path,
    )


def _resolve_ticker(qa: QueuedActionRow, db_path: Path | None) -> str:
    """Ticker for the downstream row — taken from the parent ALERT.

    ``ticker`` is an alert-level property, so trigger-drafted payloads don't
    carry it; requiring it in the payload made every queued action
    un-approvable (KeyError → 0 ledger writes despite pending actions). The
    parent alert (``qa.alert_id``) always has it. A valid payload ``ticker``,
    if present, still wins as an explicit override (future-proofing); otherwise
    fall back to the alert. Raises ``LookupError`` if the parent alert is gone
    (caught by ``main`` and surfaced as a clean exit-1).
    """
    raw = qa.payload.get("ticker")
    if isinstance(raw, str) and raw:
        return raw
    return get_alert(qa.alert_id, db_path=db_path).ticker


def _require_str(payload: dict[str, object], key: str, qa: QueuedActionRow) -> str:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise KeyError(
            f"queued_action id={qa.id} payload missing required string field "
            f"{key!r} (action_kind={qa.action_kind!r})"
        )
    return raw


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------


def _err(msg: str) -> None:
    sys.stderr.write(f"approve_queued_action: error: {msg}\n")


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _resolve_db_path(repo_root: Path, override: Path | None) -> Path | None:
    if override is not None:
        return override
    candidate = repo_root.resolve() / "data" / "portfolio.db"
    return candidate if candidate.exists() else None


def _fetch_action_or_raise(
    action_id: int, db_path: Path | None
) -> QueuedActionRow:
    """Fetch one queued_action row, surfacing LookupError if missing.

    The CRUD module's transition functions already raise LookupError on
    missing IDs, but we need the row *before* mutating to know the
    action_kind and dispatch the downstream write.
    """
    return get_action(action_id, db_path=db_path)


if __name__ == "__main__":
    sys.exit(main())
