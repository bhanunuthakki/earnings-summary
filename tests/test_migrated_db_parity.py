"""``migrated_db`` templates are faithful, isolated, and current-head shaped.

The cache in ``conftest.migrated_db`` amortises the migration chain by building
one template per session and copying it per test. Converting the remaining
direct builders to it is only safe while three properties hold, and none of them
was checked before:

* **fidelity** — a copied template is indistinguishable from a database built by
  replaying the chain, so a converted test sees the schema, revision, and seed
  row counts it used to build by hand. Seed *values* are deliberately excluded:
  several active migrations seed with ``CURRENT_TIMESTAMP``, so two builds a
  second apart differ there for reasons that say nothing about the cache;
* **isolation** — a test that writes to its copy cannot reach the shared
  template or another test's copy, so conversions cannot couple tests together;
* **current-head shape** — on the active graph the cache ignores ``stamp`` and
  always builds to ``target``.

The third one is the conversion rule, not a detail. A legacy fixture shaped like
``command.stamp(cfg, PRIOR_HEAD)`` followed by an upgrade to head produces a
*partial* schema, because stamping only writes ``alembic_version`` and skips the
DDL of every migration it claims to have run. ``migrated_db`` instead runs the
whole chain, so a converted test gains the tables its old fixture never created.
That is safe for a test wanting the current head and wrong for one asserting an
intermediate schema, which is why historical migration tests stay direct
builders.

Comparison here is semantic, not textual, because the chain is **not**
byte-reproducible: a table rebuild emits its constraints in unordered sequence,
so ``alerts`` alone has two DDL spellings that differ purely in whether
``ck_alerts_status`` precedes the foreign key. Both carry identical constraints.
Sorting each definition's clauses absorbs that spelling while still catching an
added, removed, or altered column or constraint, and ``PRAGMA table_info``
covers the column order that sorting would otherwise hide.

This module deliberately contains no chain build of its own: the un-amortised
control lives in the ``direct_chain_db`` fixture, so the direct-chain builder
inventories gain no entry. The probe DDL below still registers this file as a
hand-DDL builder, which is the intended classification.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

#: type, name, tbl_name, and the definition's clauses in a canonical order.
SchemaObject = tuple[str, str, str, tuple[str, ...]]
#: cid, name, declared type, notnull, primary-key position.
ColumnInfo = tuple[int, str, str, int, int]


def _canonical_clauses(sql: str) -> tuple[str, ...]:
    """Return a definition's clauses in a spelling-independent order."""
    return tuple(
        sorted(line.strip().rstrip(",").strip() for line in sql.splitlines() if line.strip())
    )


def _schema(db: Path) -> list[SchemaObject]:
    """Return every schema object, ignoring names SQLite assigns itself."""
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_autoindex_%' "
            "ORDER BY type, name, tbl_name"
        ).fetchall()
    return [
        (str(row[0]), str(row[1]), str(row[2]), _canonical_clauses(str(row[3]))) for row in rows
    ]


def _table_names(db: Path) -> set[str]:
    return {name for kind, name, _, _ in _schema(db) if kind == "table"}


def _columns(db: Path) -> dict[str, list[ColumnInfo]]:
    """Return each table's declared columns in order, which sorting would hide.

    Deliberately omits ``dflt_value``: a changed default alters the definition's
    text, so ``_schema`` already catches it.
    """
    columns: dict[str, list[ColumnInfo]] = {}
    with closing(sqlite3.connect(db)) as connection:
        for name in sorted(_table_names(db)):
            quoted = name.replace('"', '""')
            columns[name] = [
                (int(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
            ]
    return columns


def _row_counts(db: Path) -> dict[str, int]:
    """Return per-table row counts so migration seed inserts are compared too.

    Counts rather than values: several active migrations seed with
    ``CURRENT_TIMESTAMP`` or ``datetime('now')``, so two builds a second apart
    legitimately hold different values in the same rows.
    """
    counts: dict[str, int] = {}
    names = sorted(_table_names(db))
    with closing(sqlite3.connect(db)) as connection:
        for name in names:
            quoted = name.replace('"', '""')
            counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
    return counts


def _revisions(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _add_table(db: Path, name: str) -> None:
    quoted = name.replace('"', '""')
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(f'CREATE TABLE "{quoted}" (id INTEGER PRIMARY KEY)')
        connection.commit()


def test_a_copied_template_matches_an_unamortised_chain_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    direct_chain_db: Callable[..., Path],
) -> None:
    cached = migrated_db(tmp_path / "cached.db")
    direct = direct_chain_db(tmp_path / "direct.db")

    cached_schema = _schema(cached)
    # A comparison against an empty or near-empty build would pass for the wrong
    # reason; the chain produces well over a thousand objects.
    assert len(cached_schema) > 1000
    assert cached_schema == _schema(direct)
    assert _columns(cached) == _columns(direct)


def test_a_copied_template_carries_the_same_seed_row_counts_as_a_chain_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    direct_chain_db: Callable[..., Path],
) -> None:
    cached = migrated_db(tmp_path / "cached.db")
    direct = direct_chain_db(tmp_path / "direct.db")

    cached_counts = _row_counts(cached)
    assert sum(cached_counts.values()) > 0, "migration seeds are missing entirely"
    assert cached_counts == _row_counts(direct)


def test_a_copied_template_reports_the_same_revision_as_a_chain_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    direct_chain_db: Callable[..., Path],
) -> None:
    cached = migrated_db(tmp_path / "cached.db")
    direct = direct_chain_db(tmp_path / "direct.db")
    assert _revisions(cached) == _revisions(direct)
    assert len(_revisions(cached)) == 1


def test_writing_to_one_copy_does_not_reach_another(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    first = migrated_db(tmp_path / "first.db")
    second = migrated_db(tmp_path / "second.db")
    assert first.resolve() != second.resolve()

    _add_table(first, "isolation_probe")

    assert "isolation_probe" in _table_names(first)
    assert "isolation_probe" not in _table_names(second)


def test_corrupting_a_copy_leaves_the_shared_template_intact(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    """A converted test must not be able to poison later tests in its session."""
    victim = migrated_db(tmp_path / "victim.db")
    _add_table(victim, "template_probe")
    with closing(sqlite3.connect(victim)) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    # Confirm the damage landed, so a later clean copy is real evidence of
    # isolation rather than of a write that quietly did nothing.
    victim_tables = _table_names(victim)
    assert "template_probe" in victim_tables
    assert "alembic_version" not in victim_tables

    later = migrated_db(tmp_path / "later.db")
    later_tables = _table_names(later)
    assert "template_probe" not in later_tables
    assert "alembic_version" in later_tables


def test_the_active_graph_cache_ignores_the_compatibility_stamp(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    """Active-graph builds reach current head whatever ``stamp`` claims.

    Legacy fixtures pass a prior head; conversions rely on that argument being
    inert rather than silently selecting a narrower schema.
    """
    default = migrated_db(tmp_path / "default.db")
    stamped = migrated_db(tmp_path / "stamped.db", stamp="0006_add_ask_proposal_approval")
    assert _schema(default) == _schema(stamped)
    assert _columns(default) == _columns(stamped)

    # Pin the revision too, so this holds on its own rather than relying on the
    # fidelity tests above to notice a stamp that selected a narrower build.
    assert _revisions(stamped) == _revisions(default)
    assert _revisions(stamped) != ["0006_add_ask_proposal_approval"]
