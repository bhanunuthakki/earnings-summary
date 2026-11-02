"""Read current process tenets, explicit notes and owner decisions for a chat.

This command makes no model call. It requires an explicit or configured database.
Its output is research context and does not authorize collection or state changes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clock import now_naive_utc
from db_paths import require_product_db_path
from synthesis.insights import InsightRow
from synthesis.tenets import list_tenets
from user_state._db import open_read_conn
from user_state.notes import AnalystNoteRow, list_notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Caps keep the pack scannable at session-start — a wall of history is worse
# than a capped, newest-first slice (mirrors the anchor loaders' char caps).
_TENET_CAP = 12
_QUESTION_CAP = 20
_DECISION_CAP = 15

# Tables/views this pack reads from, paired with the column that carries their
# most-recent write — the freshness probe in the header. Best-effort: a
# missing table/view is skipped, never fatal.
_FRESHNESS_PROBES: tuple[tuple[str, str], ...] = (
    ("insight_notes", "updated_at"),
    ("analyst_notes", "updated_at"),
    ("decisions", "made_at"),
)


def _log(event: dict[str, object]) -> None:
    print(json.dumps(event), file=sys.stderr)


def _flatten(text: str, *, cap: int = 240) -> str:
    """Collapse whitespace and cap length — the anchor-loader convention
    (``src/llm/anchors.py``) so a multi-paragraph body never blows out the
    pack's scannability."""
    body = " ".join(text.split())
    if len(body) > cap:
        body = body[: cap - 3].rstrip() + "..."
    return body


def _existing_tables_and_views(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    return {str(r[0]) for r in rows}


def _max_freshness(db_path: Path) -> str | None:
    """Best-effort MAX(timestamp) across every source this pack reads, so a
    stale pack is visible at a glance rather than silently trusted. Returns
    None when the DB is missing or no probed table/view exists yet."""
    try:
        conn = open_read_conn(db_path)
    except (FileNotFoundError, RuntimeError) as exc:
        _log({"event": "freshness_probe_db_unavailable", "error": str(exc)})
        return None
    try:
        existing = _existing_tables_and_views(conn)
        stamps: list[str] = []
        for table, col in _FRESHNESS_PROBES:
            if table not in existing:
                continue
            try:
                row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
            except sqlite3.Error as exc:
                _log({"event": "freshness_probe_query_failed", "table": table, "error": str(exc)})
                continue
            if row is not None and row[0]:
                stamps.append(str(row[0]))
        return max(stamps) if stamps else None
    finally:
        conn.close()


def _render_header(db_path: Path) -> list[str]:
    now = now_naive_utc().isoformat()
    freshness = _max_freshness(db_path)
    return [
        "# Research Session Context Pack",
        "",
        f"_Generated (machine, naive-UTC): {now}_",
        f"_DB freshness (max updated_at across sources): "
        f"{freshness or 'unknown — no readable source table/view'}_",
        "",
    ]


def _render_worldview(db_path: Path) -> list[str]:
    lines = ["## Worldview", ""]

    try:
        tenets: list[InsightRow] = list_tenets(status="current", db_path=db_path)
    except Exception as exc:  # never block the pack — degrade to empty
        _log({"event": "tenets_load_failed", "error": str(exc)})
        tenets = []
    lines.append("### Tenets")
    if tenets:
        for t in tenets[:_TENET_CAP]:
            lines.append(f"- {_flatten(t.body_md)} `[{t.scope_key}]` (as of {t.as_of[:10]})")
    else:
        lines.append("_None recorded._")
    lines.append("")

    return lines


def _render_open_questions(db_path: Path) -> list[str]:
    lines = ["## Open Questions & Wonderings", ""]
    try:
        notes: list[AnalystNoteRow] = list_notes(
            kind="question", status="open", limit=_QUESTION_CAP, db_path=db_path
        )
        notes += list_notes(kind="musing", status="open", limit=_QUESTION_CAP, db_path=db_path)
    except Exception as exc:  # never block the pack — degrade to empty
        _log({"event": "open_notes_load_failed", "error": str(exc)})
        notes = []
    if not notes:
        lines.append("_None open._")
        lines.append("")
        return lines
    notes.sort(key=lambda n: (n.created_at, n.id), reverse=True)
    notes = notes[:_QUESTION_CAP]
    for n in notes:
        ticker = f" ({n.ticker})" if n.ticker else ""
        lines.append(
            f"- [{n.kind}]{ticker} {_flatten(n.body)} (since {n.created_at.date().isoformat()})"
        )
    lines.append("")
    return lines


def _render_open_decisions(db_path: Path) -> list[str]:
    lines = ["## Open Decisions & Falsifiers (owner, ungraded)", ""]
    try:
        conn = open_read_conn(db_path)
    except (FileNotFoundError, RuntimeError) as exc:
        _log({"event": "decisions_db_unavailable", "error": str(exc)})
        lines.append("_Unavailable — DB not found._")
        lines.append("")
        return lines
    try:
        if "v_decision_journal" not in _existing_tables_and_views(conn):
            lines.append("_None — `v_decision_journal` is not present on this DB (pre-0179)._")
            lines.append("")
            return lines
        rows = conn.execute(
            "SELECT decision_id, ticker, recommendation_kind, conviction, falsifier, made_at "
            "FROM v_decision_journal "
            "WHERE decided_by = 'owner' AND outcome_label IS NULL "
            "ORDER BY made_at DESC LIMIT ?",
            (_DECISION_CAP,),
        ).fetchall()
    except sqlite3.Error as exc:
        _log({"event": "decisions_query_failed", "error": str(exc)})
        lines.append("_Unavailable — query failed against this DB._")
        lines.append("")
        return lines
    finally:
        conn.close()
    if not rows:
        lines.append("_None open._")
        lines.append("")
        return lines
    for r in rows:
        ticker = f"**{r['ticker']}**" if r["ticker"] else "_(portfolio-level)_"
        conviction = f" ({r['conviction']} conviction)" if r["conviction"] else ""
        falsifier = f" — falsifier: {_flatten(str(r['falsifier']))}" if r["falsifier"] else ""
        lines.append(
            f"- {ticker} {r['recommendation_kind']}{conviction} "
            f"(decision #{r['decision_id']}, {str(r['made_at'])[:10]}){falsifier}"
        )
    lines.append("")
    return lines


def build_pack(db_path: Path | str | None = None) -> str:
    """Assemble the full markdown context pack. Pure read, ZERO LLM calls —
    every section degrades to an explicit empty-state note on a missing
    table/column/DB rather than raising, so a pre-migration or
    partially-seeded DB still renders a usable pack instead of crashing a
    session's opening move."""
    resolved_db = require_product_db_path(
        PROJECT_ROOT, explicit=Path(db_path) if db_path is not None else None
    )

    sections: list[str] = []
    sections.extend(_render_header(resolved_db))
    sections.extend(_render_worldview(resolved_db))
    sections.extend(_render_open_questions(resolved_db))
    sections.extend(_render_open_decisions(resolved_db))

    _log({"event": "session_context_pack_built", "db_path": str(resolved_db)})
    return "\n".join(sections).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="override the portfolio DB")
    parser.add_argument("--out", type=Path, default=None, help="write to a file instead of stdout")
    args = parser.parse_args()
    pack = build_pack(args.db_path)
    if args.out is not None:
        args.out.write_text(pack, encoding="utf-8")
        _log({"event": "session_context_pack_written", "path": str(args.out)})
    else:
        print(pack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
