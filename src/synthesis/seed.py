"""Day-one seeding of the Ledger corpus.

Two paths, so the Ledger is useful the moment it boots instead of empty for weeks:

  * ``seed_from_json`` — DETERMINISTIC ingest of the seeding-interview artifact
    (``data/ledger_seed/seed.json``, produced by the spawned interview session):
    its musings land as ``kind='musing'`` notes, its decisions as ``kind='decision'``
    notes, its themes as ``kind='theme'`` insights. No LLM — the interview already
    did the structuring. Idempotent on a per-item ``source_ref``.

  * ``seed_cluster_themes`` — an optional governed ``theme_seed_cluster`` pass that
    groups the existing musings into themes (kind='theme' insights) the owner
    hadn't named explicitly. Injectable ``call`` for testing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from synthesis.insights import record_insight
from user_state.notes import AnalystNoteRow, create_note, list_notes

# (musings) -> list of {slug, title, description, tickers, note_ids}.
ClusterCall = Callable[[Sequence[AnalystNoteRow]], "list[dict[str, object]]"]


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ticker(value: object) -> str | None:
    t = _str(value).upper()
    return t or None


def _decision_body(decision: dict[str, object]) -> str:
    action = _str(decision.get("action")) or "decision"
    ticker = _ticker(decision.get("ticker")) or "?"
    rationale = _str(decision.get("rationale"))
    head = f"{action.upper()} {ticker}"
    return f"{head} — {rationale}" if rationale else head


def seed_from_json(db_path: Path | str | None, seed_path: Path | str) -> dict[str, int]:
    """Ingest the interview seed artifact deterministically. Idempotent on a
    per-item ``source_ref`` (re-running skips already-seeded items)."""
    path = Path(seed_path)
    counts = {"musings": 0, "decisions": 0, "themes": 0, "skipped": 0}
    if not path.exists():
        return counts
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return counts
    if not isinstance(raw, dict):
        return counts
    data = cast("dict[str, object]", raw)

    musings = data.get("musings")
    if isinstance(musings, list):
        for idx, item in enumerate(cast("list[object]", musings)):
            if not isinstance(item, dict):
                continue
            m = cast("dict[str, object]", item)
            body = _str(m.get("body"))
            if not body:
                continue
            if _seed_note(
                db_path,
                ticker=_ticker(m.get("ticker")),
                kind="musing",
                source="capture",
                body=body,
                source_ref=f"seed:musing:{idx}",
                context={
                    "ledger": "musing",
                    "seeded": True,
                    "approx_date": _str(m.get("approx_date")),
                },
            ):
                counts["musings"] += 1
            else:
                counts["skipped"] += 1

    decisions = data.get("decisions")
    if isinstance(decisions, list):
        for idx, item in enumerate(cast("list[object]", decisions)):
            if not isinstance(item, dict):
                continue
            d = cast("dict[str, object]", item)
            if _seed_note(
                db_path,
                ticker=_ticker(d.get("ticker")),
                kind="decision",
                source="manual",
                body=_decision_body(d),
                source_ref=f"seed:decision:{idx}",
                context={
                    "seeded": True,
                    "action": _str(d.get("action")),
                    "conviction": _str(d.get("conviction")),
                    "falsifier": _str(d.get("falsifier")),
                    "approx_date": _str(d.get("approx_date")),
                },
            ):
                counts["decisions"] += 1
            else:
                counts["skipped"] += 1

    themes = data.get("themes")
    if isinstance(themes, list):
        for item in cast("list[object]", themes):
            if not isinstance(item, dict):
                continue
            t = cast("dict[str, object]", item)
            slug = _str(t.get("slug"))
            if not slug:
                continue
            tickers = t.get("tickers")
            record_insight(
                scope_key=f"theme:{slug}",
                kind="theme",
                body_md=_str(t.get("description")),
                source_note_ids=[],
                watermark_id=None,
                meta={
                    "slug": slug,
                    "title": _str(t.get("title")) or slug,
                    "tickers": tickers if isinstance(tickers, list) else [],
                },
                provenance="owner",
                db_path=db_path,
            )
            counts["themes"] += 1

    return counts


def _seed_note(
    db_path: Path | str | None,
    *,
    ticker: str | None,
    kind: str,
    source: str,
    body: str,
    source_ref: str,
    context: dict[str, object],
) -> bool:
    """Create a seeded note; return False (skipped) if its source_ref already
    exists (the partial-unique index makes re-seeding idempotent)."""
    try:
        create_note(
            ticker=ticker,
            kind=kind,
            body=body,
            source=source,
            source_ref=source_ref,
            context=context,
            db_path=db_path,
        )
    except sqlite3.IntegrityError:
        return False
    return True


def seed_cluster_themes(
    db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID, call: ClusterCall
) -> dict[str, int]:
    """Group existing musings into themes via the injected clustering call;
    record each as a kind='theme' insight scoped 'theme:<slug>'."""
    musings = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=10_000)
    counts = {"clustered": 0}
    if not musings:
        return counts
    valid_ids = {n.id for n in musings}
    for cluster in call(musings):
        slug = _str(cluster.get("slug"))
        if not slug:
            continue
        raw_ids = cluster.get("note_ids")
        ids = (
            [int(i) for i in cast("list[object]", raw_ids) if isinstance(i, int) and i in valid_ids]
            if isinstance(raw_ids, list)
            else []
        )
        tickers = cluster.get("tickers")
        record_insight(
            scope_key=f"theme:{slug}",
            kind="theme",
            body_md=_str(cluster.get("description")),
            source_note_ids=ids,
            watermark_id=max(ids) if ids else None,
            meta={
                "slug": slug,
                "title": _str(cluster.get("title")) or slug,
                "tickers": tickers if isinstance(tickers, list) else [],
            },
            db_path=db_path,
        )
        counts["clustered"] += 1
    return counts
