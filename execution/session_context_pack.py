"""execution/session_context_pack.py — the Claude-session bridge's READ surface (B8).

Deep research now splits by depth (owner ruling #9, 2026-07-19 review): thin
in-app Ask for quick pulls, deep research in a Claude Code session via a
platform bridge. The bridge's INGEST half already exists
(``execution/land_session_notes.py``: musing / close-intent / decision /
B4's ``transcript`` kind). This script is the READ half — what a fresh Claude
session should see BEFORE it starts researching, so it doesn't re-derive
context the platform already has or contradict a standing belief it never saw.

Pure read, ZERO LLM calls — safe to run at any hour, no quota interaction, no
``llm_budgets`` touch (the 18:00 ``session_distill`` sweep, not this script,
owns the only LLM leg in the bridge). Prints a markdown context pack:

  - the owner's current Worldview (tenets + stances + themes)
  - open questions / wonderings (analyst_notes, capped, wondering-flagged via
    a linked research_tasks row)
  - open owner decisions + falsifiers, ungraded (v_decision_journal)
  - research-task prompt blocks explicitly marked for a Claude session (the
    ``session_prompt`` value inside the legacy ``research_tasks.run_id``
    object; read defensively so a DB without the metadata column or with an
    invalid value renders an empty section instead of crashing)
  - the owner-profile anchor (affirmed capacity/appetite facts)

Every section degrades to an explicit empty-state note on a missing table /
column / DB rather than raising — the pack must always render, even against a
DB that predates half of these features.

Usage:
    python execution/session_context_pack.py
    python execution/session_context_pack.py --db-path data/portfolio.db
    python execution/session_context_pack.py --out .tmp/session_pack.md

Structured JSON events (best-effort load failures, the final build event) go
to stderr; the markdown pack goes to stdout by default.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from clock import now_naive_utc  # noqa: E402
from synthesis.insights import InsightRow, list_insights  # noqa: E402
from synthesis.tenets import list_tenets  # noqa: E402
from user_state._db import open_conn  # noqa: E402
from user_state.notes import AnalystNoteRow, list_notes  # noqa: E402

# Caps keep the pack scannable at session-start — a wall of history is worse
# than a capped, newest-first slice (mirrors the anchor loaders' char caps).
_TENET_CAP = 12
_STANCE_CAP = 12
_THEME_CAP = 12
_QUESTION_CAP = 20
_DECISION_CAP = 15
_RESEARCH_TASK_CAP = 10

# Tables/views this pack reads from, paired with the column that carries their
# most-recent write — the freshness probe in the header. Best-effort: a
# missing table/view is skipped, never fatal.
_FRESHNESS_PROBES: tuple[tuple[str, str], ...] = (
    ("insight_notes", "updated_at"),
    ("analyst_notes", "updated_at"),
    ("research_tasks", "updated_at"),
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
        conn = open_conn(db_path)
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

    try:
        stances = list_insights(kind="stance", status="current", db_path=db_path)
    except Exception as exc:
        _log({"event": "stances_load_failed", "error": str(exc)})
        stances = []
    lines.append("### Stances")
    if stances:
        for s in stances[:_STANCE_CAP]:
            lines.append(f"- **{s.scope_key}** — {_flatten(s.body_md)} (as of {s.as_of[:10]})")
    else:
        lines.append("_None recorded._")
    lines.append("")

    try:
        themes = list_insights(kind="theme", status="current", db_path=db_path)
    except Exception as exc:
        _log({"event": "themes_load_failed", "error": str(exc)})
        themes = []
    lines.append("### Themes")
    if themes:
        for th in themes[:_THEME_CAP]:
            lines.append(f"- {_flatten(th.body_md)} `[{th.scope_key}]` (as of {th.as_of[:10]})")
    else:
        lines.append("_None recorded._")
    lines.append("")
    return lines


def _wondering_note_ids(db_path: Path, note_ids: list[int]) -> set[int]:
    """Which of ``note_ids`` have a linked research_tasks row — the wondering
    flag (a 'wondering' intent verdict creates a research_tasks row keyed on
    note_id; it never patches the note's own context, see
    ``research.proposals.detect_and_create_task``)."""
    if not note_ids:
        return set()
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return set()
    try:
        placeholders = ", ".join("?" * len(note_ids))
        rows = conn.execute(
            f"SELECT DISTINCT note_id FROM research_tasks WHERE note_id IN ({placeholders})",
            note_ids,
        ).fetchall()
        return {int(r[0]) for r in rows if r[0] is not None}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


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
    wondering_ids = _wondering_note_ids(db_path, [n.id for n in notes])
    for n in notes:
        flag = " `[wondering]`" if n.id in wondering_ids else ""
        ticker = f" ({n.ticker})" if n.ticker else ""
        lines.append(
            f"- [{n.kind}]{ticker}{flag} {_flatten(n.body)} "
            f"(since {n.created_at.date().isoformat()})"
        )
    lines.append("")
    return lines


def _render_open_decisions(db_path: Path) -> list[str]:
    lines = ["## Open Decisions & Falsifiers (owner, ungraded)", ""]
    try:
        conn = open_conn(db_path)
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


def _render_research_prompts(db_path: Path) -> list[str]:
    lines = ["## Research Tasks -> Claude Session", ""]
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError) as exc:
        _log({"event": "research_tasks_db_unavailable", "error": str(exc)})
        lines.append("_Unavailable — DB not found._")
        lines.append("")
        return lines
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(research_tasks)")}
        if "run_id" not in cols:
            # Older DB: the metadata column doesn't exist yet. Render an
            # empty section with a note rather than crashing — the pack must
            # always render against a partially migrated DB.
            lines.append(
                "_None — this DB has no `run_id` metadata column; "
                "no research-task prompts to show yet._"
            )
            lines.append("")
            return lines
        rows = conn.execute(
            "SELECT id, ticker, claim, "
            "json_extract(run_id, '$.session_prompt') AS session_prompt, status "
            "FROM research_tasks "
            "WHERE CASE WHEN json_valid(run_id) THEN "
            "json_type(run_id) = 'object' "
            "AND json_type(run_id, '$.session_prompt') = 'text' "
            "AND TRIM(json_extract(run_id, '$.session_prompt')) != '' "
            "ELSE 0 END "
            "ORDER BY id DESC LIMIT ?",
            (_RESEARCH_TASK_CAP,),
        ).fetchall()
    except sqlite3.Error as exc:
        _log({"event": "research_tasks_query_failed", "error": str(exc)})
        lines.append("_Unavailable — query failed against this DB._")
        lines.append("")
        return lines
    finally:
        conn.close()
    for r in rows:
        ticker = f" ({r['ticker']})" if r["ticker"] else ""
        lines.append(f"### Task #{r['id']}{ticker} — {r['status']}")
        lines.append(f"_Claim:_ {_flatten(str(r['claim']))}")
        lines.append("")
        lines.append(str(r["session_prompt"]).strip())
        lines.append("")
    if not rows:
        lines.append("_None pending._")
        lines.append("")
    return lines


def _render_owner_profile(resolved_db: Path) -> list[str]:
    lines = ["## Owner Profile (affirmed)", ""]
    try:
        from llm.anchors import load_owner_profile_anchor

        # anchors.py resolves its own DB path as repo_root/"data"/"portfolio.db".
        # Walk back up two segments from the resolved DB so the anchor loader
        # reads the SAME db this pack is built against (both production
        # ``<repo>/data/portfolio.db`` and every test fixture follow that
        # <root>/data/portfolio.db layout) rather than silently falling back
        # to PROJECT_ROOT's own DB when ``--db-path`` points elsewhere.
        anchor = load_owner_profile_anchor(resolved_db.parent.parent)
    except Exception as exc:  # never block the pack — degrade to empty
        _log({"event": "owner_profile_anchor_failed", "error": str(exc)})
        anchor = ""
    if anchor.strip():
        lines.append(anchor.strip())
    else:
        lines.append("_None affirmed yet._")
    lines.append("")
    return lines


def build_pack(db_path: Path | str | None = None) -> str:
    """Assemble the full markdown context pack. Pure read, ZERO LLM calls —
    every section degrades to an explicit empty-state note on a missing
    table/column/DB rather than raising, so a pre-migration or
    partially-seeded DB still renders a usable pack instead of crashing a
    session's opening move."""
    resolved_db = Path(db_path) if db_path is not None else PROJECT_ROOT / "data" / "portfolio.db"

    sections: list[str] = []
    sections.extend(_render_header(resolved_db))
    sections.extend(_render_worldview(resolved_db))
    sections.extend(_render_open_questions(resolved_db))
    sections.extend(_render_open_decisions(resolved_db))
    sections.extend(_render_research_prompts(resolved_db))
    sections.extend(_render_owner_profile(resolved_db))

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
