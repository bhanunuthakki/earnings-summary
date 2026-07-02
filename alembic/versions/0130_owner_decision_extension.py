"""Owner-decision schema extension — the calibration ledger learns the owner's shape.

2026-07-02 grill decisions (thought-partner deep-dive): the ``decisions`` table
becomes the ONE calibration ledger for both streams — advisor-extracted
recommendations (all existing rows) and the owner's own decisions (the seed
retro-grade backfill + the decision_capture channels). The owner's corpus
thinks in dollars, Roth-vs-taxable, LEAPs, multi-tranche entries, and
portfolio-level moves (the XLV dry-powder sleeve) — none representable before
this, and five years of history accrued in the wrong shape is a retrofit
nightmare, so the extension lands BEFORE any backfill.

decisions:
- ``decided_by`` 'advisor' (default — every existing row) | 'owner'
- ``instrument`` / ``account`` / ``size_usd`` / ``size_pct`` / ``falsifier``
  (write-once at the writer) / ``tranche_of`` (id of the first decision in the
  same multi-tranche intent; plain INTEGER, NO REFERENCES — the repo-wide
  FK-poisoning invariant) / ``scope`` 'ticker'|'portfolio'
- ``ticker`` becomes NULLABLE; ``ck_decisions_ticker_scope`` guards that only
  portfolio-scope rows may omit it
- ``ck_decisions_source_present`` widens: an owner decision needs no
  artifact/memo provenance

analyst_notes:
- ``kind`` CHECK widens to admit ``'intent'`` — standing intents (e.g. a LEAP
  sleeve) carry a lifecycle (NOTE_STATUSES + closure provenance in
  context_json) instead of living forever as prose. The batch recreate DROPS
  the analyst_notes_fts sync triggers (the documented 0125/0128 gotcha), so
  they are restored verbatim + the index rebuilt, per the 0128 pattern.

Revision ID: 0130_owner_decision_extension
Revises: 0129_commitment_scan_log
"""

from __future__ import annotations

import contextlib

import sqlalchemy as sa

from alembic import op

revision: str = "0130_owner_decision_extension"
down_revision: str | None = "0129_commitment_scan_log"
branch_labels = None
depends_on = None

_KINDS_PRIOR = "('question', 'decision', 'watch', 'assumption', 'observation', 'musing')"
_KINDS_WITH_INTENT = (
    "('question', 'decision', 'watch', 'assumption', 'observation', 'musing', 'intent')"
)

_SOURCE_CHECK_PRIOR = (
    "source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL "
    "OR recommendation_kind = 'avoid'"
)
_SOURCE_CHECK_NEW = (
    "source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL "
    "OR recommendation_kind = 'avoid' OR decided_by = 'owner'"
)
_TICKER_SCOPE_CHECK = "scope = 'portfolio' OR ticker IS NOT NULL"

_NEW_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("decided_by", sa.String(16), nullable=False, server_default="advisor"),
    sa.Column("instrument", sa.String(24), nullable=True),
    sa.Column("account", sa.String(16), nullable=True),
    sa.Column("size_usd", sa.Float(), nullable=True),
    sa.Column("size_pct", sa.Float(), nullable=True),
    sa.Column("falsifier", sa.Text(), nullable=True),
    sa.Column("tranche_of", sa.Integer(), nullable=True),
    sa.Column("scope", sa.String(16), nullable=False, server_default="ticker"),
)

# Verbatim from 0118/0128 — the external-content FTS5 sync triggers. Any
# batch_alter_table('analyst_notes') drops them; every such migration must
# re-run this restore (0128's documented gotcha for future migrations).
_AI = (
    "CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
)
_AD = (
    "CREATE TRIGGER analyst_notes_fts_ad AFTER DELETE ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
    "VALUES ('delete', old.id, old.body); END"
)
_AU = (
    "CREATE TRIGGER analyst_notes_fts_au AFTER UPDATE ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
    "VALUES ('delete', old.id, old.body); "
    "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
)

# The two partial UNIQUE indexes — batch reflection can lose a partial WHERE,
# so they are re-created explicitly after every decisions batch recreate.
_PARTIAL_INDEXES = (
    (
        "uq_decisions_source_memo",
        "CREATE UNIQUE INDEX uq_decisions_source_memo ON decisions (source_memo_id) "
        "WHERE source_memo_id IS NOT NULL",
    ),
    (
        "uq_decisions_source_dismissal",
        "CREATE UNIQUE INDEX uq_decisions_source_dismissal ON decisions (source_dismissal_id) "
        "WHERE source_dismissal_id IS NOT NULL",
    ),
)


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _restore_fts_triggers(bind: sa.Connection) -> None:
    names = set(sa.inspect(bind).get_table_names())
    if "analyst_notes" not in names or "analyst_notes_fts" not in names:
        return  # FTS5 unavailable at 0118 → reader uses LIKE; nothing to restore
    for trig in ("analyst_notes_fts_ai", "analyst_notes_fts_ad", "analyst_notes_fts_au"):
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            op.execute(f"DROP TRIGGER IF EXISTS {trig}")
    with contextlib.suppress(sa.exc.SQLAlchemyError):
        op.execute("INSERT INTO analyst_notes_fts(analyst_notes_fts) VALUES ('rebuild')")
    op.execute(_AI)
    op.execute(_AD)
    op.execute(_AU)


def _recreate_partial_indexes(bind: sa.Connection) -> None:
    for name, ddl in _PARTIAL_INDEXES:
        existing = bind.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
            {"n": name},
        ).fetchone()
        if existing is not None and "WHERE" in str(existing[0] or "").upper():
            continue  # reflection preserved the partial index — leave it
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(ddl)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "decisions"):
        with op.batch_alter_table("decisions") as batch:
            for col in _NEW_COLUMNS:
                batch.add_column(col)
            batch.alter_column("ticker", existing_type=sa.String(16), nullable=True)
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_decisions_source_present", type_="check")
            batch.create_check_constraint("ck_decisions_source_present", _SOURCE_CHECK_NEW)
            batch.create_check_constraint("ck_decisions_ticker_scope", _TICKER_SCOPE_CHECK)
        _recreate_partial_indexes(bind)

    if _has_table(insp, "analyst_notes"):
        with op.batch_alter_table("analyst_notes") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_analyst_notes_kind", type_="check")
            batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_WITH_INTENT}")
        _restore_fts_triggers(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "decisions"):
        owner_rows = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM decisions WHERE decided_by != 'advisor' OR ticker IS NULL"
            )
        ).scalar()
        if owner_rows:
            raise RuntimeError(
                f"cannot downgrade 0130: {owner_rows} owner/portfolio-scope decision rows "
                "exist — they have no representation in the prior schema"
            )
        with op.batch_alter_table("decisions") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_decisions_ticker_scope", type_="check")
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_decisions_source_present", type_="check")
            batch.create_check_constraint("ck_decisions_source_present", _SOURCE_CHECK_PRIOR)
            batch.alter_column("ticker", existing_type=sa.String(16), nullable=False)
            for col in reversed(_NEW_COLUMNS):
                batch.drop_column(col.name)
        _recreate_partial_indexes(bind)

    if _has_table(insp, "analyst_notes"):
        intent_rows = bind.execute(
            sa.text("SELECT COUNT(*) FROM analyst_notes WHERE kind = 'intent'")
        ).scalar()
        if intent_rows:
            raise RuntimeError(
                f"cannot downgrade 0130: {intent_rows} intent notes exist — archive or "
                "repoint them first"
            )
        with op.batch_alter_table("analyst_notes") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_analyst_notes_kind", type_="check")
            batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_PRIOR}")
        _restore_fts_triggers(bind)
