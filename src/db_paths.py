"""Canonical resolution of the active ``portfolio.db`` path.

This is a home for :func:`resolve_db_path` that is safe to import at module top.
It does not import ``db`` until path resolution is actually requested, keeping
dependency loading light and avoiding unnecessary initialization work.

Roughly a dozen modules each used to define a private ``_resolve_db_path`` with
this exact body. This centralizes the logic while preserving lazy loading.
Modules with a *different* resolution contract
(e.g. ``timeseries.loaders`` which takes ``repo_root`` + ``db_path``, or
``compute.segment_cache`` which derives the path from ``__file__`` to stay
db-import-free) keep their own resolvers — this helper covers only the
``override-or-db.DB_PATH`` shape.

:func:`db_path_context` adds a scoped, thread-safe middle layer between the
explicit override and the ``db.DB_PATH`` global. A library entry point that
takes an explicit ``db_path`` (e.g. ``advisor.position_review.review_position``)
cannot thread it into every layer a governed LLM call touches — model pins,
prompt A/B, budget checks, and the ``llm_calls`` cost ledger all call
``resolve_db_path(None)`` internally. Without the context those reads/writes
silently target ``db.DB_PATH`` (a different DB when the caller never ran a
``db.set_db_path`` bootstrap): the "no such table: llm_calls" symptom, where
the LLM call succeeds but its cost row lands nowhere. Wrapping the call in
``with db_path_context(db_path):`` makes every internal ``resolve_db_path(None)``
see the caller's DB, without mutating process-global state the way
``db.set_db_path`` does.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from os import fspath
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

# Scoped override consulted by resolve_db_path between the explicit param and
# the db.DB_PATH global. ContextVar (not a bare global) so concurrent server
# threads / async tasks can each hold their own DB context without racing.
_AMBIENT_DB_PATH: ContextVar[str | None] = ContextVar("ambient_db_path", default=None)


@contextmanager
def db_path_context(db_path: Path | str | None) -> Generator[None]:
    """Scope every ``resolve_db_path(None)`` inside the block to ``db_path``.

    ``None`` is a no-op (callers can wrap unconditionally with an optional
    param). Nests: the innermost context wins, and exit always restores the
    previous value — unlike ``db.set_db_path``, nothing leaks past the block.
    An explicit non-None ``override`` passed to :func:`resolve_db_path` still
    beats the ambient value.
    """
    if db_path is None:
        yield
        return
    token = _AMBIENT_DB_PATH.set(fspath(db_path))
    try:
        yield
    finally:
        _AMBIENT_DB_PATH.reset(token)


def resolve_db_path(override: Path | str | None) -> Path | None:
    """Resolve the target DB path: an explicit ``override`` wins; then an
    active :func:`db_path_context`; otherwise ``db.DB_PATH`` if importable (the
    global a CLI's ``--db-path`` re-points via ``db.set_db_path``); else
    ``None`` when the caller has no DB context.

    ``db`` is imported lazily so importing *this* module stays side-effect-free.
    """
    if override is not None:
        return Path(override)
    ambient = _AMBIENT_DB_PATH.get()
    if ambient is not None:
        return Path(ambient)
    try:
        from db import DB_PATH
    except ImportError:
        return None
    return Path(DB_PATH)
