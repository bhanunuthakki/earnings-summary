"""Issuer registry kept in sync with the tracked-companies list.

The IR-document categorizer (:mod:`ir_uploads`) needs, per issuer:
  * a fiscal calendar id (for period attribution), and
  * issuer-name substrings (for content-based ticker detection).

Curated entries for the genuinely-tricky fiscal calendars (NVO's H1=Q2 quirk,
the 52/53-week names, etc.) live hand-maintained in
``ir_uploads.ISSUER_REGISTRY``. Every *other* active tracked ticker is
maintained here automatically: derived from ``tracked_companies`` (calendar from
``fiscal_year_end``, aliases from ``name``) into a persisted JSON store
(``data/issuer_registry.json``), kept current by onboard/remove triggers plus the
:func:`sync_all` reconcile (which also covers raw-SQL ``list_type`` flips).

The effective registry the categorizer consults is ``curated ∪ store`` with the
curated code entries winning on conflict — so a hand-tuned calendar is never
clobbered by the deriver, and a ``manual_override`` store row is never touched by
the auto-sync.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ir_uploads import ISSUER_REGISTRY, calendar_id_from_fye
from pipeline.queries import ANALYZED_LIST_TYPE_VALUES
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Active list types whose tickers get an auto registry entry. Mirrors
# db.ACTIVE_LIST_TYPES (kept local to avoid importing the heavier db module).
ACTIVE_LIST_TYPES: tuple[str, ...] = ANALYZED_LIST_TYPE_VALUES

_STORE_REL = ("data", "issuer_registry.json")
_DB_REL = ("data", "portfolio.db")

# Issuer-name suffixes stripped to produce a shorter alias (so "NVIDIA
# Corporation" also matches a bare "NVIDIA" on a cover page).
_SUFFIX_RX = re.compile(
    r",?\s+(?:Inc\.?|Corp\.?|Corporation|Co\.?|Company|Ltd\.?|Limited|Group|"
    r"Holdings|N\.V\.|S\.A\.|S\.A\.B\.|plc|AG|SE|LLC|L\.P\.)\s*$",
    re.IGNORECASE,
)


def _store_path(repo_root: Path) -> Path:
    return Path(repo_root).joinpath(*_STORE_REL)


def _db_path(repo_root: Path, db_path: Path | None) -> Path:
    return Path(db_path) if db_path is not None else Path(repo_root).joinpath(*_DB_REL)


def load_store(repo_root: Path) -> dict[str, dict]:
    """Read ``data/issuer_registry.json`` → ``{ticker: entry}``. ``{}`` if absent/corrupt."""
    path = _store_path(repo_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_store(repo_root: Path, store: dict[str, dict]) -> None:
    path = _store_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def derive_name_aliases(name: str | None, ticker: str) -> list[str]:
    """Issuer-name substrings for content detection: the full name plus a
    suffix-stripped short form. Falls back to the ticker when name is blank."""
    ticker = ticker.upper()
    if not name or not name.strip():
        return [ticker]
    full = name.strip()
    aliases = [full]
    short = _SUFFIX_RX.sub("", full).strip()
    if short and short != full:
        aliases.append(short)
    # de-dup preserving order
    return list(dict.fromkeys(aliases))


def _curated_tickers() -> set[str]:
    return {t for t, *_ in ISSUER_REGISTRY}


def _entry_from_db(name: str | None, fye: str | None) -> dict:
    return {
        "calendar": calendar_id_from_fye(fye),
        "name_aliases": derive_name_aliases(name, ""),
        "fiscal_year_end": fye,
        "source": "auto",
        "manual_override": False,
    }


def register_issuer(
    repo_root: Path,
    ticker: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: Path | None = None,
) -> bool:
    """Upsert a store entry for ``ticker`` from ``tracked_companies``.

    No-op (returns ``False``) when the ticker is curated in code, when its store
    row is a ``manual_override``, or when it is not present in
    ``tracked_companies``. Safe to call from the onboard path.
    """
    ticker = ticker.upper()
    if ticker in _curated_tickers():
        return False
    store = load_store(repo_root)
    if store.get(ticker, {}).get("manual_override"):
        return False
    owns = conn is None
    if owns:
        conn = connect_sqlite(
            _db_path(repo_root, db_path),
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
    try:
        row = conn.execute(
            "SELECT name, fiscal_year_end FROM tracked_companies WHERE ticker = ? LIMIT 1",
            (ticker,),
        ).fetchone()
    finally:
        if owns:
            conn.close()
    if row is None:
        return False
    name, fye = row[0], row[1]
    entry = _entry_from_db(name, fye)
    entry["name_aliases"] = derive_name_aliases(name, ticker)
    store[ticker] = entry
    save_store(repo_root, store)
    return True


def deregister_issuer(repo_root: Path, ticker: str) -> bool:
    """Drop ``ticker`` from the store (unless it's a ``manual_override``). Safe to
    call from the archive/remove path. Returns ``True`` if a row was removed."""
    ticker = ticker.upper()
    store = load_store(repo_root)
    entry = store.get(ticker)
    if entry is None or entry.get("manual_override"):
        return False
    del store[ticker]
    save_store(repo_root, store)
    return True


def sync_all(repo_root: Path, *, db_path: Path | None = None) -> dict:
    """Reconcile the store against active ``tracked_companies``.

    Adds/refreshes a row for every active, non-curated ticker; removes rows whose
    ticker is no longer active (orphans). ``manual_override`` rows are never
    touched. This is the drift-safety net — running it makes the store exactly
    reflect the current list regardless of how tickers were added/removed
    (including raw-SQL ``list_type`` edits the trigger path wouldn't catch).
    """
    conn = connect_sqlite(
        _db_path(repo_root, db_path),
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        placeholders = ",".join("?" * len(ACTIVE_LIST_TYPES))
        rows = conn.execute(
            "SELECT ticker, name, fiscal_year_end FROM tracked_companies "
            f"WHERE list_type IN ({placeholders}) AND archived_at IS NULL",
            ACTIVE_LIST_TYPES,
        ).fetchall()
    finally:
        conn.close()
    active = {str(r[0]).upper(): (r[1], r[2]) for r in rows}
    curated = _curated_tickers()
    store = load_store(repo_root)
    added, refreshed, removed, kept_manual = [], [], [], []
    for ticker, (name, fye) in active.items():
        if ticker in curated:
            continue
        if store.get(ticker, {}).get("manual_override"):
            kept_manual.append(ticker)
            continue
        entry = _entry_from_db(name, fye)
        entry["name_aliases"] = derive_name_aliases(name, ticker)
        (refreshed if ticker in store else added).append(ticker)
        store[ticker] = entry
    for ticker in list(store):
        if ticker not in active and not store[ticker].get("manual_override"):
            del store[ticker]
            removed.append(ticker)
    save_store(repo_root, store)
    return {
        "added": sorted(added),
        "refreshed": sorted(refreshed),
        "removed": sorted(removed),
        "kept_manual": sorted(kept_manual),
        "total": len(store),
    }


def effective_entries(repo_root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
    """The registry the categorizer consults: curated code entries plus store
    entries for any ticker not already curated (curated wins on conflict)."""
    curated = list(ISSUER_REGISTRY)
    curated_t = {t for t, *_ in curated}
    store = load_store(repo_root)
    extra = [
        (
            ticker,
            str(entry.get("calendar") or "calendar"),
            tuple(entry.get("name_aliases") or (ticker,)),
        )
        for ticker, entry in sorted(store.items())
        if ticker not in curated_t
    ]
    return curated + extra
