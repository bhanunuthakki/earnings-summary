"""Inline-comment storage for workspace reports.

A comment is anchored to a STRUCTURED selector (KPI name, failure-mode index,
news item ID, segment name, etc.) rather than DOM XPath, so comments survive
re-renders and ticker/quarter changes. One JSON file per (ticker, report_date).

Per-report lifetime (per user preference): comments live with a specific
report build. Re-rendering the same date overwrites/keeps; building a new
date starts fresh. Old comments are not auto-migrated; user can copy
manually if they want continuity.

Storage layout:
  data/report_comments/<TICKER>/<YYYY-MM-DD>.json
  └── {
        "ticker": "NU",
        "report_date": "2026-05-18",
        "comments": [Comment, ...]
      }

Public surface (consumed by:
- `src/report/renderers/workspace_comments.js` (frontend reads via
  comments_server.py)
- `execution/comments_server.py` (Flask POST/GET endpoints)
- `execution/process_report_comments.py` (intent routers)):

  list_comments(repo_root, ticker, report_date) -> list[Comment]
  append_comment(repo_root, ticker, report_date, comment) -> Comment
  update_comment(repo_root, ticker, report_date, comment_id, **fields)
  clear_addressed(repo_root, ticker, report_date) -> int
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Anchor types — what a comment can be attached to in the rendered report.
# Renderers emit `data-anchor-type=<type> data-anchor-key=<key>` attributes
# that match the Anchor.type / Anchor.key fields below.
# ---------------------------------------------------------------------------

AnchorType = Literal[
    "kpi_ledger_row",  # key = kpi name (verbatim from thesis.kpi_ledger)
    "failure_mode",  # key = failure-mode hypothesis text (first 80 chars)
    "news_item",  # key = headline text
    "saydo_bullet",  # key = bullet text (first 80 chars)
    "saydo_card",  # key = "Q3 2025 -> Q4 2025"
    "line_item",  # key = line-item name (Revenue, EBIT, etc.)
    "segment",  # key = segment name (Brazil, Mexico, GCP, ...)
    "valuation_rationale",  # key = "valuation_rationale" (singular per report)
    "thesis_lede",  # key = "thesis_lede" (singular per report)
    "company_overview",  # key = "company_overview" (singular per report)
    "free_text",  # key = arbitrary selection text (fallback)
]

IntentType = Literal[
    "drop_kpi",
    "edit_thesis",
    "ask_question",
    "fix_data",
    "rewrite_section",
    None,  # not yet classified — processor will run an LLM bucketer first
]

CommentStatus = Literal["open", "addressed", "dismissed"]


class Anchor(BaseModel):
    """Structured pointer to the report element a comment is attached to.

    Two anchor regimes:

    1. **Structured** (kpi_ledger_row, failure_mode, ...): `key` is a
       semantic identifier (KPI name, segment name) the renderer emits as
       `data-anchor-key`. Comments survive any re-render that keeps the
       same identifier — even across quarters and code changes.

    2. **Free-text** (type == 'free_text'): the user highlighted arbitrary
       text. `key` is the selected text excerpt (up to 200 chars). The
       optional `parent_landmark` + `occurrence_index` fields let the JS
       re-find the same selection on later renders even if the text
       appears multiple times (occurrence_index is 0-based within the
       parent_landmark scope). Falls back gracefully when the underlying
       text changes — the highlight is silently dropped, the comment
       stays in the store as a record.
    """

    type: AnchorType
    key: str
    tab: str | None = None  # which tab (thesis / earnings / saydo / ...)
    parent_landmark: str | None = None  # nearest section/panel title (free_text only)
    occurrence_index: int | None = None  # which match if landmark has duplicates


class ThreadEntry(BaseModel):
    """One turn in a back-and-forth on a comment (e.g. user follows up
    after the processor's resolution)."""

    role: Literal["user", "assistant"]
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Comment(BaseModel):
    """One inline comment."""

    id: str  # cmt_<date>_<6-char-hex>
    anchor: Anchor
    selected_text: str | None = None  # what the user highlighted, for context
    comment: str
    intent: IntentType = None
    status: CommentStatus = "open"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    addressed_at: datetime | None = None
    resolution_note: str | None = None
    follow_up_thread: list[ThreadEntry] = Field(default_factory=list)


class CommentStore(BaseModel):
    """On-disk payload shape."""

    ticker: str
    report_date: date
    comments: list[Comment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Storage IO
# ---------------------------------------------------------------------------


def _store_path(repo_root: Path, ticker: str, report_date: date) -> Path:
    out = repo_root / "data" / "report_comments" / ticker.upper()
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{report_date.isoformat()}.json"


def load_store(repo_root: Path, ticker: str, report_date: date) -> CommentStore:
    """Read the on-disk comment store; return an empty store when missing."""
    path = _store_path(repo_root, ticker, report_date)
    if not path.exists():
        return CommentStore(ticker=ticker.upper(), report_date=report_date)
    try:
        return CommentStore.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Corrupt store — return empty rather than crash the report.
        return CommentStore(ticker=ticker.upper(), report_date=report_date)


def save_store(repo_root: Path, store: CommentStore) -> None:
    path = _store_path(repo_root, store.ticker, store.report_date)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")


def list_comments(
    repo_root: Path, ticker: str, report_date: date, status: CommentStatus | None = None
) -> list[Comment]:
    """List comments, optionally filtered by status."""
    store = load_store(repo_root, ticker, report_date)
    if status is None:
        return store.comments
    return [c for c in store.comments if c.status == status]


def append_comment(
    repo_root: Path,
    ticker: str,
    report_date: date,
    anchor: Anchor,
    text: str,
    selected_text: str | None = None,
    intent: IntentType = None,
) -> Comment:
    """Persist a new comment. Returns the created Comment with assigned id."""
    store = load_store(repo_root, ticker, report_date)
    comment = Comment(
        id=_next_id(report_date),
        anchor=anchor,
        selected_text=selected_text,
        comment=text,
        intent=intent,
    )
    store.comments.append(comment)
    save_store(repo_root, store)
    return comment


def update_comment(
    repo_root: Path,
    ticker: str,
    report_date: date,
    comment_id: str,
    *,
    status: CommentStatus | None = None,
    resolution_note: str | None = None,
    append_thread: ThreadEntry | None = None,
    intent: IntentType = None,
) -> Comment | None:
    """In-place update. Returns updated Comment or None if id not found."""
    store = load_store(repo_root, ticker, report_date)
    for c in store.comments:
        if c.id == comment_id:
            if status is not None:
                c.status = status
                if status == "addressed":
                    c.addressed_at = datetime.now(UTC)
            if resolution_note is not None:
                c.resolution_note = resolution_note
            if append_thread is not None:
                c.follow_up_thread.append(append_thread)
            if intent is not None:
                c.intent = intent
            save_store(repo_root, store)
            return c
    return None


def delete_comment(
    repo_root: Path, ticker: str, report_date: date, comment_id: str
) -> bool:
    """Hard-delete a single comment. Returns True if removed."""
    store = load_store(repo_root, ticker, report_date)
    before = len(store.comments)
    store.comments = [c for c in store.comments if c.id != comment_id]
    if len(store.comments) == before:
        return False
    save_store(repo_root, store)
    return True


def clear_addressed(repo_root: Path, ticker: str, report_date: date) -> int:
    """Drop all addressed + dismissed comments. Returns count removed."""
    store = load_store(repo_root, ticker, report_date)
    before = len(store.comments)
    store.comments = [c for c in store.comments if c.status == "open"]
    removed = before - len(store.comments)
    if removed > 0:
        save_store(repo_root, store)
    return removed


def to_json_payload(store: CommentStore) -> dict[str, object]:
    """Render for serialization to the frontend (snake_case JSON)."""
    return cast("dict[str, object]", json.loads(store.model_dump_json()))


def _next_id(report_date: date) -> str:
    return f"cmt_{report_date.isoformat()}_{secrets.token_hex(3)}"
