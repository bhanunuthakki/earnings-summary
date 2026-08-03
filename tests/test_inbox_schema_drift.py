"""A drifted inbox must be distinguishable from an empty one.

``SchemaRevisionMismatch`` subclasses ``sqlite3.OperationalError``, so every
``except sqlite3.Error`` in the collect path used to swallow it and return [].
The result: during the 2026-08-02 prod drift the stream rendered empty while 21
pending alerts and 45 ledger entries sat in the tables, and nothing on the page
distinguished "cannot read your inbox" from "nothing new this morning".

These tests pin the distinction itself, not the plumbing — the assertion that
matters is that a database WITH data renders differently when drifted than a
database with no data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import fire_alert
from dashboard import render_alert_feed
from dashboard.inbox import collect_inbox, collect_inbox_counted, schema_drift_notice
from schema_compat import SchemaRevisionMismatch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"

#: The rendered ELEMENT, not the class name — ``.ix-degraded`` also appears in
#: every page's stylesheet, so a bare "ix-degraded" substring check matches
#: the CSS and passes on a healthy page too. It did, until this caught it.
_DEGRADED_EL = '<div class="ix-degraded"'


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """A healthy database carrying real, renderable inbox content."""
    db = tmp_path / "inbox_drift.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    fire_alert(
        ticker="NU",
        trigger_kind="kpi_inflection",
        fired_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        evidence_json=json.dumps({"summary": "ok", "memo": "a real pending alert"}),
        signature_sha="drift-sig-1",
        db_path=db,
    )
    return db


def _drift(db_path: Path) -> None:
    """Move the DB's recorded revision off this checkout's head."""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE alembic_version SET version_num = ' 0001_a_revision_from_the_future'")
    conn.commit()
    conn.close()


def test_drifted_db_raises_instead_of_reporting_an_empty_stream(populated_db: Path) -> None:
    """THE defect. Before the fix this returned ([], 0) — indistinguishable
    from a quiet morning, on a database that demonstrably had an alert."""
    items, total = collect_inbox_counted(populated_db)
    assert len(items) == 1 and total == 1, "precondition: the DB has content"

    _drift(populated_db)

    with pytest.raises(SchemaRevisionMismatch):
        collect_inbox_counted(populated_db)
    with pytest.raises(SchemaRevisionMismatch):
        collect_inbox(populated_db)


def test_feed_renders_a_degraded_notice_not_an_empty_list(populated_db: Path) -> None:
    """/feed must still render — but say it cannot be read."""
    healthy = render_alert_feed(db_path=populated_db)
    assert _DEGRADED_EL not in healthy

    _drift(populated_db)
    drifted = render_alert_feed(db_path=populated_db)

    assert _DEGRADED_EL in drifted
    assert "Inbox unavailable" in drifted
    # ...and it must NOT read as an ordinary empty stream.
    assert "No items match the current filters." not in drifted
    assert "Nothing new" not in drifted
    # The header must not state a count for a stream it could not count —
    # "0 shown" is the same lie in a different place.
    assert "0 shown" not in drifted
    assert "could not be read" in drifted


def test_degraded_notice_is_distinguishable_from_the_empty_state(tmp_path: Path) -> None:
    """The discriminating assertion: an EMPTY database and a DRIFTED one must
    not produce the same page. A guard that passed both ways would pin nothing.
    """
    empty = tmp_path / "empty.db"
    cfg = _build_config(empty)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    empty_html = render_alert_feed(db_path=empty)

    drifted = tmp_path / "drifted.db"
    cfg2 = _build_config(drifted)
    command.stamp(cfg2, PRIOR_HEAD)
    command.upgrade(cfg2, "head")
    _drift(drifted)
    drifted_html = render_alert_feed(db_path=drifted)

    assert _DEGRADED_EL not in empty_html
    assert _DEGRADED_EL in drifted_html
    assert empty_html != drifted_html


def test_notice_carries_the_engineering_receipt_without_leading_with_it() -> None:
    exc = SchemaRevisionMismatch(
        "database schema revision does not match this checkout "
        "(db=['0261_x'], code=0270_y); run `alembic upgrade head`"
    )
    html = schema_drift_notice(exc)
    # Owner language leads...
    assert "Inbox unavailable" in html
    assert "nothing has been lost" in html
    # ...the raw diagnostic rides in the hover, escaped, not in the body text.
    assert 'title="database schema revision does not match this checkout' in html
    assert not html.startswith("database schema revision")


def test_missing_database_returns_a_tuple_not_a_bare_list(tmp_path: Path) -> None:
    """Regression: ``collect_inbox_counted`` returned a bare ``[]`` on a missing
    database while its contract is ``(items, total)``. Every caller unpacks the
    result, so this raised "not enough values to unpack" — a crash, not a
    degraded render. Nothing caught it because the suites always pass a real DB.
    """
    absent = tmp_path / "does_not_exist.db"
    items, total = collect_inbox_counted(absent)  # must not raise ValueError
    assert items == [] and total == 0

    assert collect_inbox(absent) == []

    # The surface that unpacks it must survive too.
    html = render_alert_feed(db_path=absent)
    assert "Inbox feed" in html
    assert _DEGRADED_EL not in html, "a missing DB is empty, not drifted"


def test_an_absent_table_still_degrades_quietly(tmp_path: Path) -> None:
    """The tolerance these handlers were written for must survive: a database
    missing the inbox tables yields [], not an exception. Only DRIFT is loud."""
    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    items, total = collect_inbox_counted(bare)
    assert items == [] and total == 0
