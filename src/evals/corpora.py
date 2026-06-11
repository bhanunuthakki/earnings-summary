"""Audit-mode corpora: real production outputs for the rubric judge (mode B).

Mode A (golden sets) generates fresh outputs through the production call path;
mode B audits what production has ALREADY written. Each loader returns the
artifacts of one purpose as ``AuditItem`` rows, newest first, so
``--limit N`` grades the freshest N and ``--since-days D`` scopes a weekly
cron run to the artifacts that changed since the last one
(directives/llm_evals_plan.md §2.1, PR 2).

Loaders are read-only and tolerant of missing sources: a repo without
``data/bear_case/`` or without the ``advisor_memos`` table simply has nothing
to audit (empty corpus), which the runner reports as "nothing to grade" —
distinct from a grading failure.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Defensive cap on the text handed to the judge. The real artifacts sit far
# below it (bear cases ~8-15K chars, summaries ~3-6K, memos ~4K); the cap only
# bites pathological files, and the truncation is marked so the judge (and the
# persisted transcript) can see it happened.
MAX_CONTENT_CHARS = 30_000

# Mirrors _SUMMARY_RX in src/report/sections/earnings.py (the §5 reader of the
# same files). Replicated rather than imported: that module pulls the pydantic
# report stack, which the eval harness doesn't need.
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)


@dataclass(frozen=True, slots=True)
class AuditItem:
    """One production artifact queued for rubric judgment.

    ``item_id`` maps onto eval_case_results.case_id; ``label`` onto its
    question column (it names the source, so the failed-case drill-down reads
    well); ``produced_at`` is naive-UTC per repo convention and drives the
    ``--since-days`` freshness filter (None = age unknown ⇒ excluded by any
    since filter, kept in unfiltered runs).
    """

    item_id: str
    label: str
    ticker: str | None
    content: str
    produced_at: datetime | None = None


CorpusLoader = Callable[[Path], list[AuditItem]]


def _clip(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[:MAX_CONTENT_CHARS] + f"\n...[truncated from {len(text)} chars]"


def _mtime_naive_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(tzinfo=None)
    except OSError:
        return None


def load_bear_case_corpus(repo_root: Path) -> list[AuditItem]:
    """Every ``data/bear_case/<TICKER>.json`` sidecar, newest mtime first.

    The sidecar is the parsed JSON the §7 section cached (the same content
    the llm_artifacts row carries); unreadable/unparseable files are skipped
    with a log line — a corrupt cache file is the section's problem, not a
    quality score.
    """
    out: list[AuditItem] = []
    base = repo_root / "data" / "bear_case"
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.json")):
        ticker = path.stem.upper()
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                {
                    "event": "eval_corpus_skip_unreadable",
                    "purpose": "bear_case",
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        out.append(
            AuditItem(
                item_id=ticker,
                label=f"bear_case/{ticker} (data/bear_case/{path.name})",
                ticker=ticker,
                content=_clip(json.dumps(payload, indent=2, ensure_ascii=False)),
                produced_at=_mtime_naive_utc(path),
            )
        )
    out.sort(key=lambda i: i.produced_at or datetime.min, reverse=True)
    return out


def load_transcript_summary_corpus(repo_root: Path) -> list[AuditItem]:
    """Every per-quarter summary in ``.tmp/``, newest fiscal quarter first.

    Matches the same filename grammar §5 reads (``_SUMMARY_RX``), so the
    corpus is exactly the set of notes the report surfaces.
    """
    keyed: list[tuple[int, int, AuditItem]] = []
    tmp_dir = repo_root / ".tmp"
    if not tmp_dir.is_dir():
        return []
    for path in sorted(tmp_dir.iterdir()):
        m = _SUMMARY_RX.match(path.name)
        if not m:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                {
                    "event": "eval_corpus_skip_unreadable",
                    "purpose": "transcript_summary",
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if not text.strip():
            continue
        ticker = m.group("ticker")
        q, y = int(m.group("q")), int(m.group("y"))
        keyed.append(
            (
                y,
                q,
                AuditItem(
                    item_id=f"{ticker}_Q{q}_{y}",
                    label=f"transcript_summary/{ticker} Q{q}'{y % 100:02d} (.tmp/{path.name})",
                    ticker=ticker,
                    content=_clip(text),
                    produced_at=_mtime_naive_utc(path),
                ),
            )
        )
    # Fiscal recency beats file mtime here: a re-rendered old quarter should
    # not outrank the newest print under --limit.
    keyed.sort(key=lambda t: (t[0], t[1], t[2].item_id), reverse=True)
    return [item for _, _, item in keyed]


def load_advisor_next_dollar_corpus(repo_root: Path) -> list[AuditItem]:
    """Every ``advisor_memos`` row with kind='next_dollar', newest first.

    Missing DB / missing table ⇒ empty corpus (nothing has been generated to
    audit), logged at info — the weekly cron must not fail on a repo that
    hasn't run the advisor yet.
    """
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='advisor_memos'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_advisor_memos_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, ticker, title, body_md, created_at
            FROM advisor_memos
            WHERE kind = 'next_dollar' AND body_md IS NOT NULL AND body_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_advisor_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for memo_id, ticker, title, body_md, created_at in rows:
        produced_at: datetime | None = None
        if isinstance(created_at, str):
            try:
                produced_at = datetime.fromisoformat(created_at)
            except ValueError:
                produced_at = None
        out.append(
            AuditItem(
                item_id=f"memo:{memo_id}",
                label=f"advisor_next_dollar/memo:{memo_id} — {str(title)[:80]}",
                ticker=str(ticker) if ticker else None,
                content=_clip(str(body_md)),
                produced_at=produced_at,
            )
        )
    return out


# purpose -> loader. The rubric runner resolves its corpus here; adding an
# audit purpose = one rubric file + one loader + one entry (+ registry/model
# wiring asserted by tests).
CORPUS_LOADERS: dict[str, CorpusLoader] = {
    "bear_case": load_bear_case_corpus,
    "transcript_summary": load_transcript_summary_corpus,
    "advisor_next_dollar": load_advisor_next_dollar_corpus,
}


def filter_since(items: list[AuditItem], since_days: int | None) -> list[AuditItem]:
    """Keep items produced within the window. ``None`` = no filter. Items
    with unknown age are excluded by an active filter (a weekly cron judging
    "fresh artifacts" must not re-grade undatable ones every week)."""
    if since_days is None:
        return items
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)
    return [i for i in items if i.produced_at is not None and i.produced_at >= cutoff]
