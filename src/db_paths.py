"""Canonical resolution of the active ``portfolio.db`` path.

This is a home for :func:`resolve_db_path` that is SAFE TO IMPORT AT MODULE TOP:
it does not import ``db`` at module load, so importing it never triggers
``db.init_db()`` (which runs as a side effect of importing the ``db`` module).

Roughly a dozen modules each used to define a private ``_resolve_db_path`` with
this exact body, precisely to dodge that import-time side effect via a lazy
in-function ``from db import DB_PATH``. This centralizes the logic without
re-introducing the side effect. Modules with a *different* resolution contract
(e.g. ``timeseries.loaders`` which takes ``repo_root`` + ``db_path``, or
``compute.segment_cache`` which derives the path from ``__file__`` to stay
db-import-free) keep their own resolvers — this helper covers only the
``override-or-db.DB_PATH`` shape.
"""

from __future__ import annotations

from pathlib import Path


def resolve_db_path(override: Path | str | None) -> Path | None:
    """Resolve the target DB path: an explicit ``override`` wins; otherwise
    ``db.DB_PATH`` if importable (the global a CLI's ``--db-path`` re-points via
    ``db.set_db_path``); else ``None`` when the caller has no DB context.

    ``db`` is imported lazily so importing *this* module stays side-effect-free.
    """
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH
    except ImportError:
        return None
    return Path(DB_PATH)
