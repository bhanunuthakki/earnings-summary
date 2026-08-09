"""§6 IR documents — press release & presentation briefs by quarter.

Sources:
  - .tmp/{TICKER}_{Q}_{YEAR}_press_release_summary.txt
  - .tmp/{TICKER}_{Q}_{YEAR}_presentation_brief.txt
  - documents table (source_url for each underlying PDF, when present)
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Literal, cast

from report.models import (
    IrDocCard,
    IrDocsSection,
    SectionStatus,
)
from report.sections._common import has_table, missing, open_repo_db

_BRIEF_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?P<kind>press_release_summary|presentation_brief)\.txt$"
)

DocType = Literal["press_release", "presentation", "transcript"]

_KIND_TO_DOCTYPE: dict[str, DocType] = {
    "press_release_summary": "press_release",
    "presentation_brief": "presentation",
}


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> IrDocsSection:
    tmp_dir = repo_root / ".tmp"
    cards = _scan_briefs(tmp_dir, ticker)

    if not cards:
        return IrDocsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(process_ir_documents)",
                fix_command=f"python execution/process_ir_documents.py --ticker {ticker.upper()}",
                detail="No press-release / presentation briefs found under .tmp/.",
            ),
        )

    _attach_source_urls(cards, ticker, repo_root, conn=conn)
    return IrDocsSection(status=SectionStatus.OK, cards=cards)


def _scan_briefs(tmp_dir: Path, ticker: str) -> list[IrDocCard]:
    if not tmp_dir.exists():
        return []
    upper = ticker.upper()
    cards: list[IrDocCard] = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        m = _BRIEF_RX.match(path.name)
        if not m or m.group("ticker").upper() != upper:
            continue
        kind = m.group("kind")
        if kind not in _KIND_TO_DOCTYPE:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                summary = f.read()
        except OSError:
            continue
        cards.append(
            IrDocCard(
                quarter=f"Q{m.group('q')}",
                year=int(m.group("y")),
                doc_type=_KIND_TO_DOCTYPE[kind],
                summary_md=summary,
                local_path=str(path),
            )
        )
    cards.sort(key=lambda c: (c.year, c.quarter, c.doc_type))
    return cards


def _attach_source_urls(
    cards: list[IrDocCard],
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Best-effort: join cards to documents.source_url where the period_end matches."""
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None or not has_table(db_conn, "documents"):
        if db_conn is not None and conn is None:
            db_conn.close()
        return
    for card in cards:
        url = _lookup_source_url(db_conn, ticker, card)
        if url is not None:
            card.source_url = url
    if conn is None:
        db_conn.close()


def _lookup_source_url(conn: sqlite3.Connection, ticker: str, card: IrDocCard) -> str | None:
    cursor = conn.cursor()
    doc_type_filter = f"ir_{cast(str, card.doc_type)}"
    cursor.execute(
        """
        SELECT source_url
        FROM documents
        WHERE ticker = ?
          AND doc_type = ?
          AND strftime('%Y', period_end) = ?
        ORDER BY period_end DESC LIMIT 1
        """,
        (ticker.upper(), doc_type_filter, str(card.year)),
    )
    row = cursor.fetchone()
    return row["source_url"] if row else None
