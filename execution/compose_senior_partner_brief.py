"""execution/compose_senior_partner_brief.py — generate (or cache-hit) this
ISO week's Senior Partner Brief (PRD
``docs/design/personal_investment_partner_prd.md`` §9.1, P2.2) and, when a
Telegram bot is configured, deliver it.

Idempotent per ISO-week: :func:`advisor.senior_partner_brief.compose_brief`
caches on ``(iso_week, every gathered artifact/snapshot/ping ref)`` via
``llm_artifacts.input_sha256`` — re-running this script the same week with
unchanged inputs is a cache hit (no LLM re-spend, no duplicate governor
drain, no duplicate Telegram send: delivery dedup rides ``standup_messages``
conventions, keyed the same way ``src/standup`` dedupes on
``signature_sha``).

**No cron/schedule is registered by this script.** Wiring a recurring
cadence for this purpose is an owner-authorization item for P3
(``directives/llm_quota_scheduling.md`` — every new scheduled LLM leg needs
an explicit owner-approved window registration before it can run
unattended). Run this manually, or from an ad hoc/owner-triggered job, until
that authorization lands.

Immediate-path (tier-1 ``decisive_alert_reason``) delivery is UNCHANGED by
this script — it rides the existing standup/governor plumbing
(``src/research/governor.py``'s ``falsifier_breach`` class,
``execution/run_coach_pings.py``) and is not touched here. This script is
the WEEKLY leg only.

CLI:
    python execution/compose_senior_partner_brief.py
    python execution/compose_senior_partner_brief.py --no-telegram
    python execution/compose_senior_partner_brief.py --repo-root . --db-path /tmp/x.db

Exit 0 on a persisted brief (LLM-authored OR a labeled deterministic
fallback — both are a successful compose; a Telegram send failure degrades
the outcome but does not flip the exit code, mirroring
``run_weekly_packet.py``'s "not configured / no chat id" degrade). A missing
private mobile Inbox URL refuses Telegram delivery and exits 1 rather than
sending a dead Review action. Exit 1 also covers a missing DB or a hard-stop
exception (auth/setup) propagating from the governed call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from advisor.senior_partner_brief import (  # noqa: E402
    SeniorPartnerBrief,
    build_telegram_keyboard,
    build_telegram_text,
    compose_brief,
    private_mobile_inbox_url,
)
from capture import token_store  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402
from user_state._db import now_naive_utc, open_conn  # noqa: E402


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}, default=str), file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="compose and persist only — skip the Telegram delivery leg entirely",
    )
    return parser.parse_args(argv)


def _signature_sha(iso_year: int, iso_week: int) -> str:
    return hashlib.sha256(f"senior_partner_brief:{iso_year}-W{iso_week:02d}".encode()).hexdigest()


def _telegram_reply_markup(
    brief: SeniorPartnerBrief,
    *,
    artifact_id: int | None,
    db_path: Path,
) -> dict[str, object]:
    """Build a Telegram keyboard only when Review is a real private URL."""
    inbox_url = private_mobile_inbox_url()
    if inbox_url is None:
        raise RuntimeError("private mobile Inbox URL is not configured; refusing Telegram delivery")
    return build_telegram_keyboard(
        brief,
        artifact_id=artifact_id,
        inbox_url=inbox_url,
        db_path=db_path,
    )


def _already_delivered_this_week(db_path: Path, *, iso_year: int, iso_week: int) -> bool:
    """standup_messages dedup (mirrors src/standup's own signature_sha
    convention): a 'delivered' row for this week's signature means the brief
    already went out — never re-send on a re-run."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM standup_messages WHERE signal_kind = 'senior_partner_brief' "
            "AND signature_sha = ? AND status = 'delivered'",
            (_signature_sha(iso_year, iso_week),),
        ).fetchone()
        return row is not None
    except Exception:
        return False  # pre-0107 schema — never block delivery on a missing table
    finally:
        conn.close()


def _record_standup_message(
    db_path: Path,
    *,
    iso_year: int,
    iso_week: int,
    status: str,
    headline: str,
    artifact_id: int | None,
) -> None:
    """Best-effort ``standup_messages`` receipt — the delivery/dedup ledger
    convention this purpose rides per the PRD (§9.1 "reuse standup_messages
    for delivery/dedup/cooldown"). A write failure never fails compose."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO standup_messages (user_id, ticker, signal_kind, signature_sha, "
            "status, headline, evidence_json, created_at) "
            "VALUES ('bhanu', NULL, 'senior_partner_brief', ?, ?, ?, ?, ?)",
            (
                _signature_sha(iso_year, iso_week),
                status,
                headline[:500],
                json.dumps({"artifact_id": artifact_id}),
                now_naive_utc().isoformat(),
            ),
        )
        conn.commit()
    except Exception as exc:  # pre-0107 schema / locked DB — never fail compose over this
        _log("standup_message_record_failed", error=str(exc))
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    if not db_path.exists():
        _log("halt", reason=f"no DB at {db_path}")
        return 1

    import db

    db.set_db_path(db_path)  # so LLM cost rows land in THIS DB's ledger

    try:
        result = compose_brief(db_path, repo_root)
    except Exception as exc:
        if is_hard_stop(exc):
            _log("hard_stop", error=str(exc))
            raise
        _log("compose_failed", error=str(exc))
        return 1

    brief = result.brief
    _log(
        "brief_composed",
        iso_year=brief.iso_year,
        iso_week=brief.iso_week,
        artifact_id=result.artifact_id,
        cache_hit=result.cache_hit,
        selection_mode=result.selection_mode,
        degraded_reasons=list(result.degraded_reasons),
        action_requested_count=len(brief.action_requested_items()),
        is_active_week=brief.is_active_week,
        routed_ping_count=len(brief.routed_ping_ids),
    )

    if args.no_telegram:
        _log("telegram_skipped", reason="--no-telegram")
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False}))
        return 0

    if _already_delivered_this_week(db_path, iso_year=brief.iso_year, iso_week=brief.iso_week):
        _log("telegram_skipped", reason="already delivered this ISO week")
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False, "dedup": True}))
        return 0

    try:
        token = token_store.load_token(repo_root / "data" / "secrets" / "telegram_bot_token")
    except token_store.CaptureSetupError as exc:
        _log("telegram_not_configured", error=str(exc))
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False}))
        return 0

    chat_id = token_store.load_chat_id(repo_root / "data" / "capture" / "telegram_chat_id.json")
    if chat_id is None:
        _log("telegram_no_chat_id")
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False}))
        return 0

    from capture import telegram

    headline = (
        brief.highest_priority_decision.title
        if brief.highest_priority_decision
        else (brief.what_changed[0].title if brief.what_changed else "Senior Partner Brief")
    )
    try:
        reply_markup = _telegram_reply_markup(
            brief,
            artifact_id=result.artifact_id,
            db_path=db_path,
        )
    except RuntimeError as exc:
        _log("telegram_mobile_inbox_not_configured", error=str(exc))
        _record_standup_message(
            db_path,
            iso_year=brief.iso_year,
            iso_week=brief.iso_week,
            status="compose_failed",
            headline=headline,
            artifact_id=result.artifact_id,
        )
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False}))
        return 1
    try:
        telegram.send_message(
            token,
            chat_id,
            build_telegram_text(brief),
            reply_markup=reply_markup,
        )
    except Exception as exc:
        _log("telegram_send_failed", error=str(exc))
        _record_standup_message(
            db_path,
            iso_year=brief.iso_year,
            iso_week=brief.iso_week,
            status="compose_failed",
            headline=headline,
            artifact_id=result.artifact_id,
        )
        print(json.dumps({"artifact_id": result.artifact_id, "delivered": False}))
        return 0

    _record_standup_message(
        db_path,
        iso_year=brief.iso_year,
        iso_week=brief.iso_week,
        status="delivered",
        headline=headline,
        artifact_id=result.artifact_id,
    )
    _log("telegram_delivered", artifact_id=result.artifact_id)
    print(json.dumps({"artifact_id": result.artifact_id, "delivered": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
