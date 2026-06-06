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
import os
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
    "edit_structured",  # mutate structured fields (break_rules, tier_*_kpis, etc.)
    "extract_kpi",  # supply a KPI value (with quote/source) via comment text
    "ask_question",
    "fix_data",
    "rewrite_section",
    "platform_change",  # cross-workspace bug or feature — not a single-ticker brief edit
    None,  # not yet classified — processor will run an LLM bucketer first
]

CommentStatus = Literal["open", "addressed", "dismissed"]


# ---------------------------------------------------------------------------
# Keyword shortcuts in the comment body
# ---------------------------------------------------------------------------
#
# Prefixing a comment with a slash-keyword sets the intent verbatim and
# skips Haiku auto-classification. Saves an LLM call and is more reliable
# than natural-language inference.
#
# Examples:
#   "/kpi SuperCore split made this irrelevant"
#       → intent=drop_kpi, comment="SuperCore split made this irrelevant"
#   "/q what's NU's NPL trend over 8 quarters?"
#       → intent=ask_question, comment="what's NU's NPL trend over 8 quarters?"
#   "/update revise to flag the FGTS regulation"
#       → intent=edit_thesis, comment="revise to flag the FGTS regulation"
#
# The keyword + leading space are stripped from the stored comment text.
# Explicit dropdown picks (caller passes `intent=...`) always override the
# keyword, so the dropdown still wins if you set both.

import re

_KEYWORD_TO_INTENT: dict[str, str] = {
    "kpi": "drop_kpi",
    "thesis": "edit_thesis",
    "update": "edit_thesis",  # alias — "update the thesis"
    "q": "ask_question",
    "ask": "ask_question",
    "fix": "fix_data",
    "rewrite": "rewrite_section",
    "platform": "platform_change",  # cross-workspace bug or feature
    "feature": "platform_change",
    "bug": "platform_change",
}

_KEYWORD_RX = re.compile(
    r"^/(" + "|".join(re.escape(k) for k in _KEYWORD_TO_INTENT) + r")\b[ \t:]*",
    re.IGNORECASE,
)


def extract_intent_from_text(text: str) -> tuple[str | None, str]:
    """Parse a leading `/keyword` from the comment body.

    Returns ``(intent, cleaned_text)``. When no keyword is present, returns
    ``(None, text)`` unchanged — caller falls back to the explicit
    dropdown pick or Haiku auto-classify.

    Match rules:
    - Keyword must be at the very start (no leading whitespace stripped first)
    - Followed by a word boundary plus optional ``:`` / space separator
    - Case-insensitive
    """
    m = _KEYWORD_RX.match(text)
    if not m:
        return (None, text)
    keyword = m.group(1).lower()
    cleaned = text[m.end() :].lstrip()
    return (_KEYWORD_TO_INTENT[keyword], cleaned)


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
    """Persist the store atomically.

    Writes to a sibling tempfile in the same directory (so the rename is on
    the same filesystem), fsyncs, then ``os.replace`` swaps it in. A reader
    will see either the prior store or the new one, never a partial file —
    matters when two threads of the Flask dev server (``threaded=True``)
    race on the same path. The threading lock from :func:`store_lock`
    composes on top: atomic-write protects against torn writes; the lock
    protects against lost updates.

    Windows quirk: on Win32 ``os.replace`` raises ``PermissionError`` if
    another handle (typically a concurrent ``load_store`` reader on a GET)
    has the destination open. The retry loop covers that microsecond race —
    POSIX behavior is unaffected because the first attempt always succeeds.
    """
    path = _store_path(repo_root, store.ticker, store.report_date)
    payload = store.model_dump_json(indent=2)
    # delete=False so we control the lifecycle — os.replace handles the swap.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Up to ~250ms of retry covers concurrent-reader windows on Windows.
        # Backoff doubles from 2ms so a long-held reader doesn't busy-loop.
        delay = 0.002
        for _ in range(7):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                threading.Event().wait(delay)
                delay *= 2
        else:
            os.replace(tmp, path)  # final attempt; propagate if it still fails
    except BaseException:
        # Clean up the tempfile on failure; missing_ok in case mkstemp itself
        # raised before creating the file (very rare).
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Concurrency — Flask runs threaded=True (see execution/comments_server.py),
# so two POST/PATCH/DELETE requests against the same (ticker, report_date)
# can interleave their load-mutate-save sequences. The store JSON is rewritten
# whole on each save, so without a lock the second writer wins and the first
# writer's update is silently lost.
#
# A process-wide dict of per-path locks lets concurrent updates on DIFFERENT
# tickers/dates proceed in parallel while serialising updates on the same
# file. The guard lock is tiny — held only long enough to look up / create
# the per-path lock.
# ---------------------------------------------------------------------------

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    """Return the lock for ``path``, creating it on first access."""
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager  # pyright: ignore[reportDeprecated]  # false-positive in pyright 1.1.409
def store_lock(repo_root: Path, ticker: str, report_date: date) -> Iterator[None]:
    """Serialise read-modify-write on a single comments file.

    Use around every mutating sequence: ``load_store → mutate → save_store``.
    A new lock per ``(ticker, report_date)`` lets unrelated stores update
    concurrently; same-store updates queue.
    """
    lock = _path_lock(_store_path(repo_root, ticker, report_date))
    with lock:
        yield


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
    """Persist a new comment. Returns the created Comment with assigned id.

    If the caller didn't pass an explicit ``intent`` and the comment text
    starts with a recognized slash-keyword (``/kpi``, ``/thesis``, ``/q``,
    ``/ask``, ``/fix``, ``/update``, ``/rewrite``), the keyword is extracted
    + stripped from the stored text and the matching intent is recorded.
    Explicit ``intent`` always wins over the keyword.
    """
    if intent is None:
        keyword_intent, cleaned = extract_intent_from_text(text)
        if keyword_intent is not None:
            intent = cast("IntentType", keyword_intent)
            text = cleaned
    with store_lock(repo_root, ticker, report_date):
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
    with store_lock(repo_root, ticker, report_date):
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


def delete_comment(repo_root: Path, ticker: str, report_date: date, comment_id: str) -> bool:
    """Hard-delete a single comment. Returns True if removed."""
    with store_lock(repo_root, ticker, report_date):
        store = load_store(repo_root, ticker, report_date)
        before = len(store.comments)
        store.comments = [c for c in store.comments if c.id != comment_id]
        if len(store.comments) == before:
            return False
        save_store(repo_root, store)
        return True


def clear_addressed(repo_root: Path, ticker: str, report_date: date) -> int:
    """Drop all addressed + dismissed comments. Returns count removed."""
    with store_lock(repo_root, ticker, report_date):
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
