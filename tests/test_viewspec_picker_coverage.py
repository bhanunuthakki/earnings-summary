"""S5 (capture-every-number): the DIY picker surfaces EVERY extracted fact.

Covers the four picker-coverage seams S5 widened:

* the per-domain cap is lifted so the long tail isn't silently truncated;
* ``kpi_definitions.definition_origin`` (S1 / migration 0113) rides each kpi
  entry as ``origin`` — and a pre-0113 DB (no column) keeps its kpi domain
  rather than erroring it away;
* override-only facts (a company-doc figure FMP never carried) are unioned in
  from ``fact_overrides`` AND render with their override chip when picked;
* segment cells carry period-level document provenance.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from compute.kpi_resolver import kpi_group_key
from provenance.overrides import record_override
from timeseries.loaders import load_segment_junction_series_with_provenance
from viewspec.engine import execute_view, metric_catalog
from viewspec.spec import ViewSpec

# Minimal schema: just the tables the catalog + engine touch. definition_origin
# is added conditionally per test so both the post-0113 and pre-0113 worlds are
# exercised.
_BASE_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'millions'
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL,
    dim_type TEXT NOT NULL,
    dim_name TEXT NOT NULL,
    value NUMERIC NOT NULL,
    metric TEXT NOT NULL,
    unit TEXT
);
"""

# Mirrors alembic 0111 (the columns overrides.record_override writes).
_OVERRIDES_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    action TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    value_json TEXT,
    source_doc_type TEXT NOT NULL,
    source_accession TEXT,
    source_exhibit TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    source_doc_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    locator TEXT
);
"""

_DOC = (
    "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
    " fetched_at, fetch_status, source_url, source_quality_tier) VALUES"
    " (1, 'TST', 'fmp', 'fmp_income_statement', 'f.json', 'a', '2026-01-05 10:00:00',"
    " 'ok', 'https://fmp.example/f.json', 'fmp_normalized')"
)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_base(conn: sqlite3.Connection, *, with_origin: bool = False) -> None:
    conn.executescript(_BASE_DDL)
    # kpi_definitions is created here (not in _BASE_DDL) so the optional
    # definition_origin column can be added inline per test.
    origin_col = ", definition_origin TEXT NOT NULL DEFAULT 'analyst'" if with_origin else ""
    conn.execute(
        "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        f" ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual'{origin_col})"
    )
    conn.execute(_DOC)
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, unit, source_doc_id) VALUES ('TST','2025-09-30 00:00:00','Q3','revenue',"
        " '150','actual',1)"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# cap lift
# ---------------------------------------------------------------------------


def test_catalog_lifts_old_300_cap(tmp_path: Path) -> None:
    db = tmp_path / "cap.db"
    conn = _connect(db)
    _seed_base(conn)
    ins = (
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)"
    )
    # 360 distinct one-off line items — past the old hard cap of 300.
    for i in range(360):
        conn.execute(ins, ("TST", "2025-09-30 00:00:00", "Q3", f"oneoff_{i:04d}", "1", "actual", 1))
    conn.commit()
    conn.close()

    cat = metric_catalog(db, ["TST"])
    tokens = {str(e["token"]) for e in cat["fin"]}
    # 360 one-offs + 'revenue' all survive (the long tail is not truncated).
    assert len(cat["fin"]) == 361
    assert "fin:oneoff_0359" in tokens

    # The cap parameter is still honored when a caller asks for a small page.
    capped = metric_catalog(db, ["TST"], limit_per_domain=50)
    assert len(capped["fin"]) == 50


# ---------------------------------------------------------------------------
# definition_origin surfacing (+ pre-0113 tolerance)
# ---------------------------------------------------------------------------


def test_catalog_surfaces_definition_origin(tmp_path: Path) -> None:
    db = tmp_path / "origin.db"
    conn = _connect(db)
    _seed_base(conn, with_origin=True)
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, definition_origin)"
        " VALUES (1,'TST','ROE','analyst'), (2,'TST','One-off ratio','capture')"
    )
    kpi = (
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id,"
        " value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)"
    )
    conn.execute(kpi, ("TST", "2025-09-30 00:00:00", "Q3", 1, "11", "percent", 1))
    conn.execute(kpi, ("TST", "2025-09-30 00:00:00", "Q3", 2, "0.5", "actual", 1))
    conn.commit()
    conn.close()

    cat = metric_catalog(db, ["TST"])
    by_token = {str(e["token"]): e for e in cat["kpi"]}
    assert by_token["kpi:ROE"]["origin"] == "analyst"
    assert by_token["kpi:One-off ratio"]["origin"] == "capture"


def test_catalog_tolerates_pre_0113_db(tmp_path: Path) -> None:
    """A DB without definition_origin (the live DB lags the migration) keeps its
    kpi domain — the column guard must not let _catalog_query swallow it."""
    db = tmp_path / "pre0113.db"
    conn = _connect(db)
    _seed_base(conn, with_origin=False)
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1,'TST','ROE')")
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id,"
        " value, unit, source_doc_id) VALUES ('TST','2025-09-30 00:00:00','Q3',1,'11','percent',1)"
    )
    conn.commit()
    conn.close()

    cat = metric_catalog(db, ["TST"])
    tokens = {str(e["token"]) for e in cat["kpi"]}
    assert tokens == {"kpi:ROE"}
    assert "origin" not in cat["kpi"][0]  # no column → no key, not a crash


# ---------------------------------------------------------------------------
# override-only facts: pickable AND rendered
# ---------------------------------------------------------------------------


def _record(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    fact_kind: str,
    fact_key: str,
    action: str,
    value: float | None = None,
    unit: str | None = None,
    value_json: dict[str, object] | None = None,
) -> None:
    record_override(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        fact_kind=fact_kind,
        fact_key=fact_key,
        action=action,
        value=value,
        unit=unit,
        value_json=value_json,
        source_doc_type="sec_8k",
        created_by="manual:test",
        source_accession="0001-26-01",
        observed_at="2026-02-01 00:00:00",
    )
    conn.commit()


def test_override_only_fact_pickable_and_rendered(tmp_path: Path) -> None:
    db = tmp_path / "ov.db"
    conn = _connect(db)
    _seed_base(conn)
    conn.executescript(_OVERRIDES_DDL)
    # A company-published line item FMP never carried (no base financial_facts row).
    _record(
        conn,
        ticker="TST",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind="financial_fact",
        fact_key="remaining_performance_obligations",
        action="replace",
        value=4200.0,
        unit="actual",
    )
    conn.close()

    cat = metric_catalog(db, ["TST"])
    by_token = {str(e["token"]): e for e in cat["fin"]}
    # Override-only fact is listed and flagged; the base 'revenue' still there.
    assert by_token["fin:remaining_performance_obligations"]["override_only"] is True
    assert "fin:revenue" in by_token
    assert "override_only" not in by_token["fin:revenue"]

    # Picking it renders the company-doc value with the override chip.
    spec = ViewSpec.from_dict(
        {"tickers": ["TST"], "metrics": ["fin:remaining_performance_obligations"], "periods": 4}
    )
    result = execute_view(spec, db_path=db)
    (row,) = result.rows
    cell = row.cells[-1]
    assert cell.raw == 4200.0
    assert cell.source is not None
    assert cell.source.source == "sec_8k"
    assert cell.source.accession_number == "0001-26-01"


def test_override_for_existing_base_row_not_duplicated(tmp_path: Path) -> None:
    db = tmp_path / "ov2.db"
    conn = _connect(db)
    _seed_base(conn)
    conn.executescript(_OVERRIDES_DDL)
    # An override over the EXISTING revenue row — base resolution path owns it.
    _record(
        conn,
        ticker="TST",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind="financial_fact",
        fact_key="revenue",
        action="replace",
        value=151.0,
        unit="actual",
    )
    conn.close()

    cat = metric_catalog(db, ["TST"])
    rev = [e for e in cat["fin"] if e["token"] == "fin:revenue"]
    assert len(rev) == 1  # not double-listed by the union


def test_segment_cell_override_token_is_pickable(tmp_path: Path) -> None:
    db = tmp_path / "ovseg.db"
    conn = _connect(db)
    _seed_base(conn)
    conn.executescript(_OVERRIDES_DDL)
    _record(
        conn,
        ticker="TST",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind="segment",
        fact_key="product|Cloud|revenue",
        action="replace",
        value=64.0,
        unit="millions",
    )
    # A record-level segment override (no '|') names a whole dim — not one slice.
    _record(
        conn,
        ticker="TST",
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        fact_kind="segment",
        fact_key="geography",
        action="replace",
        value=None,
        value_json={"US": 10.0},
    )
    conn.close()

    cat = metric_catalog(db, ["TST"])
    seg_tokens = {str(e["token"]) for e in cat["seg"]}
    assert "seg:product:Cloud:revenue" in seg_tokens  # cell override → parseable token
    assert not any("geography" in t for t in seg_tokens)  # record-level skipped


# ---------------------------------------------------------------------------
# segment provenance loader
# ---------------------------------------------------------------------------


def test_segment_junction_provenance_loader(tmp_path: Path) -> None:
    db = tmp_path / "seg.db"
    conn = _connect(db)
    _seed_base(conn)
    conn.execute(
        "INSERT INTO segment_periods (id, ticker, period_end, fiscal_period_type,"
        " source_doc_id, unit) VALUES (1,'TST','2025-09-30 00:00:00','Q3',1,'millions')"
    )
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, value, metric)"
        " VALUES (1,'product','Cloud',50,'revenue')"
    )
    conn.commit()
    conn.close()

    obs = load_segment_junction_series_with_provenance(
        "TST", [("product", "Cloud")], "revenue", db_path=db
    )
    assert [o.value for o in obs] == [50.0]
    prov = obs[0].provenance
    assert prov["source"] == "fmp_normalized"
    assert prov["source_doc_id"] == 1
    assert prov["source_url"] == "https://fmp.example/f.json"


def test_segment_provenance_loader_missing_tables(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    conn = _connect(db)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert (
        load_segment_junction_series_with_provenance(
            "TST", [("product", "Cloud")], "revenue", db_path=db
        )
        == []
    )


# ---------------------------------------------------------------------------
# KPI de-fragmentation (§7a.3/.4): variants collapse to one comparable token,
# conservatively, and the token resolves to each ticker's own variant.
# ---------------------------------------------------------------------------

_SEP = " — "  # mirrors the capture-all section/axis/leaf separator (U+2014)


def _seed_kpis(
    db: Path,
    defs: list[tuple[str, str, int]],
    *,
    with_origin: bool = False,
    origins: dict[str, str] | None = None,
) -> None:
    """``defs`` = [(ticker, name, n_facts)]; one kpi_definition + n_facts each.

    Each fact lands in a DISTINCT calendar quarter (a quarter-end month, walking
    back a year every 4) so the richness count and the quarterly loader both see
    ``n_facts`` distinct observations. ``origins`` maps a name to its
    definition_origin.
    """
    conn = _connect(db)
    conn.executescript(_BASE_DDL)
    origin_col = ", definition_origin TEXT NOT NULL DEFAULT 'analyst'" if with_origin else ""
    conn.execute(
        "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        f" ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual'{origin_col})"
    )
    conn.execute(_DOC)
    origins = origins or {}
    for i, (ticker, name, n_facts) in enumerate(defs, start=1):
        if with_origin:
            conn.execute(
                "INSERT INTO kpi_definitions (id, ticker, name, definition_origin)"
                " VALUES (?,?,?,?)",
                (i, ticker, name, origins.get(name, "analyst")),
            )
        else:
            conn.execute(
                "INSERT INTO kpi_definitions (id, ticker, name) VALUES (?,?,?)", (i, ticker, name)
            )
        for f in range(n_facts):
            month = (3, 6, 9, 12)[f % 4]
            year = 2025 - (f // 4)
            pe = f"{year}-{month:02d}-28 00:00:00"
            conn.execute(
                "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id,"
                " value, unit, source_doc_id) VALUES (?,?,?,?,?,?,1)",
                (ticker, pe, f"Q{(f % 4) + 1}", i, str(10.0 + f), "actual"),
            )
    conn.commit()
    conn.close()


def test_kpi_group_key() -> None:
    # Section-qualified leaf and a clean variant collapse to the same key.
    assert kpi_group_key(f"Net Interest Income (Details){_SEP}Net interest margin") == (
        "net interest margin"
    )
    assert kpi_group_key("Net interest margin (annualized)") == "net interest margin"
    assert kpi_group_key("Net interest margin") == "net interest margin"
    # A generic single-word leaf is NOT peeled — distinct metrics keep distinct
    # keys (no false merge).
    assert kpi_group_key(f"Loans (Details){_SEP}Total") != kpi_group_key(
        f"Deposits (Details){_SEP}Total"
    )
    # True synonyms never merge (their normalized leaves differ).
    assert kpi_group_key("NIM") != kpi_group_key("Net interest margin")


def test_catalog_defragments_kpi_across_tickers(tmp_path: Path) -> None:
    db = tmp_path / "defrag.db"
    _seed_kpis(
        db,
        [
            ("NU", "Net interest margin", 3),
            ("MELI", f"Net Interest Income (Details){_SEP}Net interest margin", 2),
        ],
    )
    cat = metric_catalog(db, ["NU", "MELI"])
    nim = [e for e in cat["kpi"] if kpi_group_key(str(e["label"])) == "net interest margin"]
    # ONE comparable token for both tickers' variants...
    assert len(nim) == 1
    # ...representative is the shortest (cleanest) variant, coverage is both.
    assert nim[0]["label"] == "Net interest margin"
    assert nim[0]["tickers"] == 2


def test_catalog_defragments_within_ticker(tmp_path: Path) -> None:
    db = tmp_path / "within.db"
    # Same ticker reports a unit-qualified and a bare spelling of one metric.
    _seed_kpis(db, [("NU", "Monthly ARPAC (USD)", 4), ("NU", "Monthly ARPAC", 1)])
    cat = metric_catalog(db, ["NU"])
    arpac = [e for e in cat["kpi"] if "arpac" in str(e["label"]).lower()]
    assert len(arpac) == 1
    assert arpac[0]["label"] == "Monthly ARPAC"  # shortest representative
    assert arpac[0]["tickers"] == 1


def test_catalog_conservative_no_false_merge(tmp_path: Path) -> None:
    db = tmp_path / "conservative.db"
    _seed_kpis(
        db,
        [
            ("NU", f"Loans (Details){_SEP}Total", 2),
            ("NU", f"Deposits (Details){_SEP}Total", 2),
            ("NU", "NIM", 2),
            ("NU", "Net interest margin", 2),
        ],
    )
    cat = metric_catalog(db, ["NU"])
    labels = {str(e["label"]) for e in cat["kpi"]}
    # Generic-leaf "Total" tables stay distinct; NIM never merges with the
    # spelled-out metric. Four definitions → four tokens (a duplicate is fine, a
    # false merge is not).
    assert len(cat["kpi"]) == 4
    assert {"NIM", "Net interest margin"} <= labels


def test_catalog_origin_marks_capture_unless_any_analyst(tmp_path: Path) -> None:
    db = tmp_path / "defrag_origin.db"
    _seed_kpis(
        db,
        [("NU", "Net interest margin", 3), ("MELI", "Net interest margin (annualized)", 2)],
        with_origin=True,
        origins={"Net interest margin": "analyst", "Net interest margin (annualized)": "capture"},
    )
    cat = metric_catalog(db, ["NU", "MELI"])
    (nim,) = [e for e in cat["kpi"] if str(e["label"]).startswith("Net interest margin")]
    assert nim["origin"] == "analyst"  # any-analyst wins over capture in the group


def test_defragmented_token_resolves_each_ticker_variant(tmp_path: Path) -> None:
    db = tmp_path / "resolve.db"
    _seed_kpis(
        db,
        [
            ("NU", "Net interest margin", 3),
            ("MELI", f"Net Interest Income (Details){_SEP}Net interest margin", 2),
        ],
    )
    cat = metric_catalog(db, ["NU", "MELI"])
    (token,) = [
        str(e["token"])
        for e in cat["kpi"]
        if kpi_group_key(str(e["label"])) == "net interest margin"
    ]
    spec = ViewSpec.from_dict({"tickers": ["NU", "MELI"], "metrics": [token], "periods": 12})
    result = execute_view(spec, db_path=db)
    # Both tickers render from the ONE representative token — each resolved to
    # its own stored spelling — with no no-data warning.
    assert {r.ticker for r in result.rows} == {"NU", "MELI"}
    assert result.warnings == []


def test_defragmented_token_resolves_richest_within_ticker(tmp_path: Path) -> None:
    db = tmp_path / "richest.db"
    # Rich unit-qualified series (4 facts) + sparse bare one (1 fact). The
    # representative is the bare/short name, but execution must pull the RICH
    # series for that token.
    _seed_kpis(db, [("NU", "Monthly ARPAC (USD)", 4), ("NU", "Monthly ARPAC", 1)])
    spec = ViewSpec.from_dict({"tickers": ["NU"], "metrics": ["kpi:Monthly ARPAC"], "periods": 12})
    result = execute_view(spec, db_path=db)
    (row,) = result.rows
    populated = [c for c in row.cells if c.raw is not None]
    assert len(populated) == 4  # the 4-fact (rich) variant, not the 1-fact stub
