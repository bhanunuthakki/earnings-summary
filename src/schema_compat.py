"""Small, dependency-free Alembic compatibility guard for write paths.

Readers intentionally remain tolerant of an older local snapshot.  Writers do
not: applying code that expects a different migration head can silently omit
provenance columns or constraints.  The guard is deliberately stdlib-only so
it is safe to call from low-level SQLite stores.

Two shapes, for two different callers:

``require_current_for_write`` is the per-connection guard.  It raises, which is
correct for a guarded writer — but a caller that swallows exceptions by design
(``llm_call_ledger.record_call`` is "best-effort … never raises") turns that
raise into a WARNING line and keeps going.  On 2026-08-02 that cost seven LLM
cost-ledger rows while every operator-visible surface still read healthy.

``describe_drift`` is the PREFLIGHT shape for those callers' owners: it answers
"is this database behind this checkout?" without needing a writer connection,
so a scheduled job can refuse to start and a dashboard panel can say so out
loud.  It never raises — a fork or an unreadable database is reported as drift
rather than thrown, because its callers run before the code that would handle
an exception.
"""

from __future__ import annotations

import ast
import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)


class SchemaRevisionMismatch(sqlite3.OperationalError):
    """The target database is not at this checkout's single Alembic head."""


def expected_head(project_root: Path | None = None) -> str:
    """Return the one Alembic leaf in this checkout, or fail loudly on forks.

    Memoized per resolved root for the life of the process: the parse walks
    every ``alembic/versions/*.py`` (253 files, ~0.5s on this machine), and
    ``require_current_for_write`` runs on EVERY guarded writer connection —
    uncached, one Home render's ~75 ``open_conn`` calls took ~42s and read as
    "the page never loads" (2026-07-31). A checkout's migration set cannot
    change under a running process, so caching preserves the guard exactly;
    the mismatch/fork failures still raise on every call (exceptions are
    never cached by ``lru_cache``)."""
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    return _expected_head_cached(root)[0]


def known_revisions(project_root: Path | None = None) -> frozenset[str]:
    """Every revision id this checkout's ``alembic/versions`` defines.

    Membership is what separates the two drift directions: a database revision
    this checkout KNOWS is simply un-applied (``alembic upgrade head`` fixes
    it), while one it has never heard of means the CHECKOUT is behind the
    database and upgrading would be the wrong move.
    """
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    return _expected_head_cached(root)[1]


@lru_cache(maxsize=8)
def _expected_head_cached(root: Path) -> tuple[str, frozenset[str]]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (root / "alembic" / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign | ast.Assign):
                continue
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    value_node = node.value
                    if value_node is None:
                        continue
                    with suppress(ValueError):
                        values[target.id] = ast.literal_eval(value_node)
        revision = values.get("revision")
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        down = values.get("down_revision")
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, tuple):
            tuple_down = cast(tuple[object, ...], down)
            parents.update(parent for parent in tuple_down if isinstance(parent, str))
    heads = revisions - parents
    if len(heads) != 1:
        raise SchemaRevisionMismatch(
            f"checkout has {len(heads)} Alembic heads ({sorted(heads)}); merge revisions before writes"
        )
    return heads.pop(), frozenset(revisions)


def require_current_for_write(conn: sqlite3.Connection) -> None:
    """Refuse a versioned DB whose revision differs from the code checkout.

    Minimal in-memory fixtures without ``alembic_version`` are intentionally
    left to their local table contracts; production DBs are versioned and must
    be upgraded before any mutation through a guarded store.
    """
    has_version = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if has_version is None:
        return
    rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    actual = {str(row[0]) for row in rows}
    expected = expected_head()
    if actual != {expected}:
        raise SchemaRevisionMismatch(
            "database schema revision does not match this checkout "
            f"(db={sorted(actual) or ['<none>']}, code={expected}); run `alembic upgrade head`"
        )


# SQLite conditions that say "try again later", not "this database is wrong".
# Reporting a busy WAL as schema drift would fail scheduled jobs for ordinary
# write contention, which is the opposite of a trustworthy detector.
_TRANSIENT_SQLITE_ERRORS = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_PROTOCOL"})

DRIFT_DB_BEHIND_CODE = "db_behind_code"
DRIFT_CHECKOUT_BEHIND_DB = "checkout_behind_db"
DRIFT_CHECKOUT_FORKED = "checkout_forked"
DRIFT_DB_UNREADABLE = "db_unreadable"

_FIX_COMMANDS: dict[str, str] = {
    DRIFT_DB_BEHIND_CODE: "alembic upgrade head",
    DRIFT_CHECKOUT_BEHIND_DB: "git pull (this checkout is older than the database)",
    DRIFT_CHECKOUT_FORKED: "merge the Alembic heads in alembic/versions",
    DRIFT_DB_UNREADABLE: "inspect the database file",
}


@dataclass(frozen=True, slots=True)
class SchemaDrift:
    """A database this checkout must not be allowed to write through.

    ``reason`` is one of the ``DRIFT_*`` constants so callers can branch and
    render without parsing prose; ``detail`` carries the human sentence.
    """

    db_path: str
    db_revisions: tuple[str, ...]
    expected_revision: str | None
    reason: str
    detail: str

    @property
    def fix_command(self) -> str:
        return _FIX_COMMANDS.get(self.reason, "inspect the database file")

    @property
    def message(self) -> str:
        db = ",".join(self.db_revisions) or "<none>"
        code = self.expected_revision or "<forked>"
        return f"{self.detail} (db={db}, code={code}, path={self.db_path}); fix: {self.fix_command}"


def _db_revisions(path: Path) -> tuple[str, ...] | None:
    """Read ``alembic_version`` read-only; ``None`` when the table is absent.

    Propagates ``sqlite3.Error`` — the caller decides whether the condition is
    transient contention or a database it must refuse to write to.
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        has_version = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if has_version is None:
            return None
        return tuple(
            sorted(str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version"))
        )
    finally:
        conn.close()


def describe_drift(db_path: str | Path, *, project_root: Path | None = None) -> SchemaDrift | None:
    """Report Alembic drift between *db_path* and this checkout, or ``None``.

    ``None`` means "cleared to proceed": the file is absent, carries no
    ``alembic_version`` (an unversioned fixture), sits exactly on this
    checkout's head, or could not be probed because the database was
    momentarily busy.  Every other outcome is a :class:`SchemaDrift` — this
    function does not raise, because its callers run BEFORE the work whose
    error handling would otherwise catch it.
    """
    path = Path(db_path)
    if not path.exists():
        return None
    versions = (project_root or Path(__file__).resolve().parents[1]).resolve() / "alembic/versions"
    if not versions.is_dir():
        # Not a checkout (a synthetic repo root in a test, a partial copy).
        # There is no expected head to compare against, so there is no verdict
        # to give — guarded writers still refuse drift on their own.
        log.warning({"event": "schema_drift_probe_skipped", "versions_dir": str(versions)})
        return None
    try:
        expected = expected_head(project_root)
        checkout_revisions = known_revisions(project_root)
    except SchemaRevisionMismatch as exc:
        return SchemaDrift(
            db_path=str(path),
            db_revisions=(),
            expected_revision=None,
            reason=DRIFT_CHECKOUT_FORKED,
            detail=str(exc),
        )
    try:
        actual = _db_revisions(path)
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorname", "") in _TRANSIENT_SQLITE_ERRORS:
            log.warning(
                {"event": "schema_drift_probe_deferred", "path": str(path), "error": str(exc)}
            )
            return None
        return SchemaDrift(
            db_path=str(path),
            db_revisions=(),
            expected_revision=expected,
            reason=DRIFT_DB_UNREADABLE,
            detail=f"database could not be read for a revision check: {type(exc).__name__}: {exc}",
        )
    if actual is None or actual == (expected,):
        return None
    unknown = [rev for rev in actual if rev not in checkout_revisions]
    if unknown:
        return SchemaDrift(
            db_path=str(path),
            db_revisions=actual,
            expected_revision=expected,
            reason=DRIFT_CHECKOUT_BEHIND_DB,
            detail=(
                "database is on a revision this checkout does not define "
                f"({','.join(unknown)}) — the CHECKOUT is stale, not the database"
            ),
        )
    return SchemaDrift(
        db_path=str(path),
        db_revisions=actual,
        expected_revision=expected,
        reason=DRIFT_DB_BEHIND_CODE,
        detail="database schema revision is behind this checkout",
    )
