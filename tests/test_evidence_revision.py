"""Regression contracts for the scoped Ask evidence cache revision."""

from __future__ import annotations

import sqlite3

from provenance.evidence_revision import legacy_fact_append_revision


def test_legacy_fact_appends_advance_the_compatibility_revision() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE financial_facts(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.execute("CREATE TABLE kpi_facts(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        assert legacy_fact_append_revision(conn) == (0, 0)

        conn.execute("INSERT INTO financial_facts DEFAULT VALUES")
        assert legacy_fact_append_revision(conn) == (1, 0)

        conn.execute("INSERT INTO kpi_facts DEFAULT VALUES")
        assert legacy_fact_append_revision(conn) == (1, 1)
    finally:
        conn.close()
