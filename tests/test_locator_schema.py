"""Tests for the locator schema (alembic 0075): migration round-trip on tmp
DBs, EDGAR accession derivation, the documents filing-identity backfill CLI,
and locator JSON round-trip through the fact stores.

Migration tests build the pre-0075 prod shape by hand (documents +
financial_facts + kpi_facts with the 0054 audit columns), stamp alembic at
0074, and upgrade to head — so 0075 runs exactly as it will against
production. The stamped-at-0072 empty-DB case pins the _has_table guards:
a partial-schema test DB must upgrade cleanly without the fact tables.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backfill_document_accessions import (  # noqa: E402
    accession_filed_map,
    backfill,
    derive_accession,
    normalize_accession,
)

from compute._common import insert_financial_facts  # noqa: E402
from models.facts import (  # noqa: E402
    Currency,
    FactLocator,
    FinancialFact,
    FiscalPeriodType,
    Unit,
)
from pipeline.restatement_detector import (  # noqa: E402
    insert_kpi_with_restatement_detection,
    insert_with_restatement_detection,
)
from provenance.evidence_ledger import (  # noqa: E402
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)

PERIOD_END = datetime(2025, 12, 31)

# Pre-0075 prod shape (column lists mirror the live DB post-0074): enough for
# the ALTERs, the views recreated by downgrade(), and the store helpers.
_PRE0075_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    period_start DATETIME,
    period_end DATETIME,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at DATETIME NOT NULL,
    fetch_status TEXT NOT NULL,
    http_code INTEGER,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    parent_document_id INTEGER,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    line_item_concept_id INTEGER,
    extracted_by TEXT,
    supersedes_id INTEGER
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    source_excerpt TEXT,
    concept_id INTEGER,
    confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by TEXT,
    supersedes_id INTEGER
);
"""


def _alembic_cfg(db: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def _create_pre0075_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_PRE0075_DDL)
    conn.close()


def _migrate_to_head(db: Path, *, stamp: str = "0074_analyst_notes") -> None:
    cfg = _alembic_cfg(db)
    command.stamp(cfg, stamp)
    command.upgrade(cfg, "head")


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_document(
    conn: sqlite3.Connection,
    *,
    ticker: str = "TST",
    source_type: str = "fmp",
    doc_type: str = "fmp_income_statement",
    file_path: str = "data/historical/fmp/TST_income_statement_quarterly.json",
    source_url: str | None = None,
    sha256: str | None = None,
    accession_number: str | None = None,
    filing_date: str | None = None,
) -> int:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    has_identity = "accession_number" in cols
    sha = sha256 if sha256 is not None else f"{abs(hash((ticker, file_path))):064d}"[:64]
    if has_identity:
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            " fetched_at, fetch_status, raw_bytes_size, source_url, "
            " accession_number, filing_date) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', 0, ?, ?, ?)",
            (
                ticker,
                source_type,
                doc_type,
                file_path,
                sha,
                datetime(2026, 6, 1),
                source_url,
                accession_number,
                filing_date,
            ),
        )
    else:
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            " fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', 0, ?)",
            (ticker, source_type, doc_type, file_path, sha, datetime(2026, 6, 1), source_url),
        )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _bind_document_evidence(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    document_id: int,
) -> None:
    """Give a migrated legacy document the evidence required by current head."""

    row = conn.execute(
        "SELECT sha256, raw_bytes_size FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    assert row is not None
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    blob_sha = str(row[0])
    config_sha = hashlib.sha256(b"locator-schema-test").hexdigest()
    output_sha = hashlib.sha256(f"output:{ticker}:{document_id}".encode()).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=int(row[1]),
            media_type="application/json",
            storage_uri=f"file:///test/{ticker}-{document_id}.json",
            recorded_at=stamp,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"source:{ticker}:{document_id}",
            idempotency_key=f"source:{ticker}:{document_id}",
            source_kind="vendor_api",
            source_url=f"https://example.test/{ticker}/{document_id}",
            blob_sha256=blob_sha,
            source_published_at=stamp,
            filing_at=None,
            accepted_at=None,
            observed_at=stamp,
            retrieved_at=stamp,
            retrieval_config_sha256=config_sha,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=f"document:{ticker}:{document_id}",
            document_key=f"{ticker}:vendor:{document_id}",
            version_sequence=1,
            observation_id=f"source:{ticker}:{document_id}",
            blob_sha256=blob_sha,
            issuer_id=f"issuer:{ticker}",
            ticker=ticker,
            document_type="vendor_statement",
            form_type="vendor_json",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=stamp,
            as_of_at=stamp,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=document_id,
            recorded_at=stamp,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=f"run:{ticker}:{document_id}",
            idempotency_key=f"run:{ticker}:{document_id}",
            document_version_id=f"document:{ticker}:{document_id}",
            input_sha256=blob_sha,
            extractor_name="test-fixture",
            extractor_config_sha256=config_sha,
            extractor_code_version="test@1",
            output_sha256=output_sha,
            started_at=stamp,
            completed_at=stamp,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id=f"node:{ticker}:{document_id}",
            evidence_key=f"node:{ticker}:{document_id}",
            revision=1,
            extraction_run_id=f"run:{ticker}:{document_id}",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"{ticker} vendor statement",
            locator=None,
            recorded_at=stamp,
        )
    )


# ----------------------------------------------------------------------------
# migration round-trip
# ----------------------------------------------------------------------------


def test_upgrade_adds_columns_and_index(tmp_path: Path) -> None:
    db = tmp_path / "prod_shape.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)

    assert {"accession_number", "filing_date"} <= _columns(db, "documents")
    assert "locator" in _columns(db, "financial_facts")
    assert "locator" in _columns(db, "kpi_facts")

    conn = sqlite3.connect(db)
    try:
        idx = {row[1] for row in conn.execute("PRAGMA index_list(documents)")}
    finally:
        conn.close()
    assert "ix_documents_accession_number" in idx


def test_upgrade_noops_when_fact_tables_absent(tmp_path: Path) -> None:
    """A tmp DB stamped pre-0073 and upgraded to head never creates the fact
    tables — 0075's _has_table guards must let the upgrade through cleanly."""
    db = tmp_path / "empty.db"
    _migrate_to_head(db, stamp="0072_kpi_reporting_cadence")

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        (version,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert "documents" not in tables
    assert "financial_facts" not in tables
    # The chain must run through to the CURRENT head — not a hardcoded
    # revision, which broke on the first migration added after 0075.
    assert version == ScriptDirectory.from_config(_alembic_cfg(db)).get_current_head()


def test_upgrade_idempotent_on_partially_applied_schema(tmp_path: Path) -> None:
    db = tmp_path / "partial.db"
    _create_pre0075_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE documents ADD COLUMN accession_number TEXT")
    conn.execute("ALTER TABLE kpi_facts ADD COLUMN locator TEXT")
    conn.commit()
    conn.close()

    _migrate_to_head(db)

    assert {"accession_number", "filing_date"} <= _columns(db, "documents")
    assert "locator" in _columns(db, "financial_facts")
    assert "locator" in _columns(db, "kpi_facts")


def test_downgrade_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "roundtrip.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)

    command.downgrade(_alembic_cfg(db), "0074_analyst_notes")

    assert {"accession_number", "filing_date"} & _columns(db, "documents") == set()
    assert "locator" not in _columns(db, "financial_facts")
    assert "locator" not in _columns(db, "kpi_facts")
    # The batch rebuild recreates the financial_facts-dependent views from the
    # canonical DDL (same dance as 0054) — they must exist and stay queryable.
    conn = sqlite3.connect(db)
    try:
        views = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        conn.execute("SELECT * FROM metrics LIMIT 1").fetchall()
    finally:
        conn.close()
    assert {"metrics", "ratios", "v_quarters"} <= views


# ----------------------------------------------------------------------------
# FactLocator model
# ----------------------------------------------------------------------------


def test_fact_locator_json_round_trip() -> None:
    loc = FactLocator(section="Income Taxes", transcript_line=42, pdf_page=7, json_path="[3].eps")
    raw = loc.to_json()
    assert raw is not None
    assert json.loads(raw) == {
        "section": "Income Taxes",
        "transcript_line": 42,
        "pdf_page": 7,
        "json_path": "[3].eps",
    }
    assert FactLocator.from_json(raw) == loc


def test_fact_locator_empty_and_malformed() -> None:
    assert FactLocator().to_json() is None
    assert FactLocator.from_json(None) is None
    assert FactLocator.from_json("   ") is None
    with pytest.raises(ValidationError):
        FactLocator.from_json("not json")
    with pytest.raises(ValidationError):
        FactLocator.from_json('{"transcript_line": "forty-two"}')


# ----------------------------------------------------------------------------
# EDGAR accession derivation
# ----------------------------------------------------------------------------


def test_normalize_accession() -> None:
    assert normalize_accession("0001628280-21-010389") == "0001628280-21-010389"
    assert normalize_accession("000032019323000106") == "0000320193-23-000106"
    assert normalize_accession(" 0001628280-21-010389 ") == "0001628280-21-010389"
    assert normalize_accession("00016282802101038") is None  # 17 digits
    assert normalize_accession("0001628280210103890") is None  # 19 digits
    assert normalize_accession("not-an-accession") is None


def test_derive_accession_from_file_path_fragment() -> None:
    # The sec_xbrl ingestion convention (src/pipeline/sec_xbrl.py).
    acc = derive_accession(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=ABNB&type=10-Q",
        "data/historical/sec/ABNB_companyfacts.json#accn=0001628280-21-010389",
    )
    assert acc == "0001628280-21-010389"


def test_derive_accession_from_file_path_segment() -> None:
    acc = derive_accession(None, "data/historical/sec/FRVO/0001628280-26-033127/filing.htm")
    assert acc == "0001628280-26-033127"


def test_derive_accession_from_edgar_urls() -> None:
    # Dashed accession in an Archives URL (the sec_s1 shape in prod).
    assert (
        derive_accession("https://www.sec.gov/Archives/edgar/data/0001628280-26-033127", None)
        == "0001628280-26-033127"
    )
    # 18-digit no-dash Archives directory normalizes to the dashed form.
    assert (
        derive_accession(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl.htm",
            None,
        )
        == "0000320193-23-000106"
    )


def test_derive_accession_underivable_shapes() -> None:
    # browse-edgar query URLs carry no accession.
    assert (
        derive_accession(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=NU&type=20-F",
            "data/historical/fmp/NU_income_statement_quarterly.json",
        )
        is None
    )
    # An 18-digit run outside sec.gov is NOT an accession.
    assert (
        derive_accession("https://s22.q4cdn.com/529358580/files/123456789012345678.pdf", None)
        is None
    )
    assert derive_accession(None, None) is None


# ----------------------------------------------------------------------------
# backfill CLI
# ----------------------------------------------------------------------------

ACCN_A = "0001628280-21-010389"
ACCN_B = "0001559720-22-000006"


def _write_companyfacts(repo_root: Path, ticker: str) -> Path:
    path = repo_root / "data" / "historical" / "sec" / f"{ticker}_companyfacts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cik": 1,
        "entityName": "Test Co",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"end": "2021-03-31", "val": 1, "accn": ACCN_A, "filed": "2021-05-14"},
                            {"end": "2021-12-31", "val": 2, "accn": ACCN_B, "filed": "2022-02-25"},
                        ]
                    }
                }
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_accession_filed_map(tmp_path: Path) -> None:
    path = _write_companyfacts(tmp_path, "TST")
    assert accession_filed_map(path) == {ACCN_A: "2021-05-14", ACCN_B: "2022-02-25"}
    assert accession_filed_map(tmp_path / "missing.json") == {}
    junk = tmp_path / "junk.json"
    junk.write_text("[1, 2, 3]", encoding="utf-8")
    assert accession_filed_map(junk) == {}


def test_backfill_derives_and_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    db = tmp_path / "backfill.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)
    _write_companyfacts(repo, "TST")

    conn = _connect(db)
    sec_id = _insert_document(
        conn,
        source_type="sec_xbrl",
        doc_type="sec_10q",
        file_path=f"data/historical/sec/TST_companyfacts.json#accn={ACCN_A}",
        source_url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=TST",
        sha256="a" * 64,
    )
    s1_id = _insert_document(
        conn,
        source_type="sec_s1",
        doc_type="sec_s1",
        file_path="data/sec_text/TST_s1_2026.txt",
        source_url=f"https://www.sec.gov/Archives/edgar/data/{ACCN_B}",
        sha256="b" * 64,
    )
    fmp_id = _insert_document(conn, sha256="c" * 64)
    _insert_document(
        conn,
        doc_type="fmp_balance_sheet",
        file_path="data/historical/fmp/TST_balance_sheet_quarterly.json",
        sha256="d" * 64,
        accession_number="0000000000-00-000000",
        filing_date="2020-01-01",
    )
    conn.commit()
    conn.close()

    result = backfill(repo, db, log=False)
    assert result.scanned == 3  # the complete row is never scanned
    assert result.accession_set == 2
    assert result.filing_date_set == 1  # companyfacts filed date for the sec_xbrl row
    assert result.accession_underivable == 1
    assert dict(result.underivable_by_source) == {"fmp": 1}
    assert result.filing_date_underivable == 1  # S-1: accession known, no filed source

    conn = _connect(db)
    rows = {
        row["id"]: row
        for row in conn.execute("SELECT id, accession_number, filing_date FROM documents")
    }
    conn.close()
    assert rows[sec_id]["accession_number"] == ACCN_A
    assert rows[sec_id]["filing_date"] == "2021-05-14"
    assert rows[s1_id]["accession_number"] == ACCN_B
    assert rows[s1_id]["filing_date"] is None
    assert rows[fmp_id]["accession_number"] is None
    assert rows[fmp_id]["filing_date"] is None

    again = backfill(repo, db, log=False)
    assert again.scanned == 2  # the fmp row + the S-1 row stay incomplete
    assert (again.accession_set, again.filing_date_set) == (0, 0)


def test_backfill_dry_run_and_ticker_filter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    db = tmp_path / "dry.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)
    _write_companyfacts(repo, "TST")

    conn = _connect(db)
    _insert_document(
        conn,
        source_type="sec_xbrl",
        doc_type="sec_10q",
        file_path=f"data/historical/sec/TST_companyfacts.json#accn={ACCN_A}",
        sha256="a" * 64,
    )
    _insert_document(conn, ticker="OTH", sha256="b" * 64)
    conn.commit()
    conn.close()

    dry = backfill(repo, db, dry_run=True, log=False)
    assert (dry.accession_set, dry.filing_date_set) == (1, 1)
    conn = _connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE accession_number IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert n == 0  # dry run never writes

    only_oth = backfill(repo, db, only_ticker="oth", dry_run=True, log=False)
    assert only_oth.scanned == 1
    assert only_oth.accession_underivable == 1


def test_backfill_requires_migrated_schema(tmp_path: Path) -> None:
    db = tmp_path / "stale.db"
    _create_pre0075_db(db)  # pre-0075: no accession_number/filing_date
    with pytest.raises(RuntimeError, match="alembic upgrade"):
        backfill(tmp_path / "repo", db, log=False)


# ----------------------------------------------------------------------------
# locator round-trip through the fact stores
# ----------------------------------------------------------------------------


def test_locator_round_trips_through_financial_fact_store(tmp_path: Path) -> None:
    db = tmp_path / "facts.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)

    conn = _connect(db)
    doc_id = _insert_document(conn)
    _bind_document_evidence(conn, ticker="TST", document_id=doc_id)
    loc = FactLocator(section="Consolidated Statements of Oper", json_path="[3].netIncome")

    new_id, _ = insert_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type="Q4",
        line_item="net_income",
        value=Decimal("123.45"),
        currency="USD",
        unit="actual",
        source_doc_id=doc_id,
        extracted_by="fmp",
        locator=loc.to_json(),
    )
    assert new_id is not None
    raw = conn.execute("SELECT locator FROM financial_facts WHERE id = ?", (new_id,)).fetchone()[
        "locator"
    ]
    assert FactLocator.from_json(raw) == loc

    # The bulk model-level helper serializes FinancialFact.locator itself.
    fact = FinancialFact(
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type=FiscalPeriodType.Q4,
        line_item="revenue",
        value=Decimal("999"),
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        source_doc_id=doc_id,
        locator=FactLocator(pdf_page=12),
    )
    assert insert_financial_facts(conn, [fact], extracted_by="fmp") == 1
    raw = conn.execute(
        "SELECT locator FROM financial_facts WHERE line_item = 'revenue'"
    ).fetchone()["locator"]
    assert FactLocator.from_json(raw) == FactLocator(pdf_page=12)

    # No locator -> NULL column, not "{}".
    new_id2, _ = insert_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type="Q4",
        line_item="gross_profit",
        value=Decimal("1"),
        currency="USD",
        unit="actual",
        source_doc_id=doc_id,
    )
    assert new_id2 is not None
    raw = conn.execute("SELECT locator FROM financial_facts WHERE id = ?", (new_id2,)).fetchone()[
        "locator"
    ]
    assert raw is None
    conn.close()


def test_locator_round_trips_through_kpi_fact_store(tmp_path: Path) -> None:
    db = tmp_path / "kpis.db"
    _create_pre0075_db(db)
    _migrate_to_head(db)

    conn = _connect(db)
    doc_id = _insert_document(conn)
    _bind_document_evidence(conn, ticker="TST", document_id=doc_id)
    loc = FactLocator(transcript_line=42)

    new_id, _ = insert_kpi_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type="Q4",
        kpi_definition_id=7,
        value=Decimal("11.3"),
        unit="percent",
        source_doc_id=doc_id,
        extracted_by="llm:test",
        locator=loc.to_json(),
    )
    assert new_id is not None
    raw = conn.execute("SELECT locator FROM kpi_facts WHERE id = ?", (new_id,)).fetchone()[
        "locator"
    ]
    assert FactLocator.from_json(raw) == loc
    conn.close()


def test_locator_silently_dropped_on_pre0075_schema(tmp_path: Path) -> None:
    """Schema tolerance: fixtures predating 0075 must keep working — the
    locator is dropped, the insert succeeds (the extracted_by precedent)."""
    db = tmp_path / "legacy.db"
    _create_pre0075_db(db)  # audit columns present, no locator column

    conn = _connect(db)
    doc_id = _insert_document(conn)
    new_id, _ = insert_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type="Q4",
        line_item="net_income",
        value=Decimal("1"),
        currency="USD",
        unit="actual",
        source_doc_id=doc_id,
        locator=FactLocator(pdf_page=3).to_json(),
    )
    assert new_id is not None
    kpi_id, _ = insert_kpi_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=PERIOD_END,
        fiscal_period_type="Q4",
        kpi_definition_id=1,
        value=Decimal("2"),
        unit="ratio",
        source_doc_id=doc_id,
        locator=FactLocator(section="Debt").to_json(),
    )
    assert kpi_id is not None
    conn.close()
