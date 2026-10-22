"""Displayed HTML source publication, using real migrated evidence authorities."""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from execution import publish_meli_reported_statements as cli
from provenance import meli_reported_statements as statements
from provenance.evidence_ledger import DocumentVersion
from provenance.meli_reported_statements import publish_meli_reported_statements
from provenance.meli_reported_tables import ReportedTableRequest, publish_meli_reported_tables
from provenance.source_coverage import ExpectedDocument
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_meli_reported_tables as table_fixtures
from tests.test_meli_reported_tables import END, STAMP, START, URL, seed_reported_html

FIXTURES = Path(__file__).parent / "fixtures" / "meli_reported_statements"


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(migrated_db(tmp_path / "statements.db"))
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _raw() -> bytes:
    return (FIXTURES / "h1-2026-snippet.html").read_bytes()


def test_fixture_commitment() -> None:
    manifest = json.loads((FIXTURES / "provenance.json").read_text())
    assert hashlib.sha256(_raw()).hexdigest() == manifest["snippet_sha256"]
    assert manifest["source_url"] == URL
    # This fixed public SEC source SHA also appears in the retained table provenance.
    assert (
        manifest["source_sha256"]
        == "b605310e398baa79b50ea488075c32d6522d8966438135cf25e8fa9f2a448924"  # pragma: allowlist secret
    )
    assert len(manifest["fragments"]) == 81
    line_hashes = {hashlib.sha256(line).hexdigest() for line in _raw().splitlines()}
    assert {fragment["sha256"] for fragment in manifest["fragments"]} <= line_hashes


def test_real_native_dry_apply_replay(database: sqlite3.Connection, tmp_path: Path) -> None:
    request = seed_reported_html(database, tmp_path, _raw())
    before = database.total_changes
    preview = publish_meli_reported_statements(database, request)
    assert database.total_changes == before
    assert preview.source_population_complete, [
        (p.population_key, p.reason_code) for p in preview.population if p.status == "rejected"
    ]
    assert preview.captured_count == 18 and preview.rejected_count == 0
    assert preview.document_completeness == "not_claimed"
    values = {p.population_key: p.normalized_numeric_value for p in preview.population}
    assert values == {
        "commerce_revenues_current_h1": "10630000000",
        "commerce_revenues_prior_h1": "7142000000",
        "total_revenues_current_h1": "19014000000",
        "total_revenues_prior_h1": "12725000000",
        "fintech_revenues_current_h1": "8384000000",
        "fintech_revenues_prior_h1": "5583000000",
        "credit_revenues_current_h1": "4290000000",
        "credit_revenues_prior_h1": "2472000000",
        "operating_income_current_h1": "1294000000",
        "operating_income_prior_h1": "1588000000",
        "depreciation_and_amortization_current_h1": "538000000",
        "depreciation_and_amortization_prior_h1": "371000000",
        "productive_asset_expenditures_current_h1": "712000000",
        "productive_asset_expenditures_prior_h1": "559000000",
        "gross_loans_receivable_current": "16375000000",
        "diluted_weighted_average_shares_current_h1": "50697299",
        "current_operating_lease_liabilities_current": "513000000",
        "noncurrent_operating_lease_liabilities_current": "2037000000",
    }
    applied = publish_meli_reported_statements(database, request.model_copy(update={"apply": True}))
    assert applied.publication_id and not applied.exact_replay
    assert applied.canonical_admission == "not_performed"
    rows = database.execute(
        "SELECT c.concept_name,c.period_start,c.period_end,c.unit_key,s.dimension_set_json,o.raw_lexical_value,o.source_locator_json FROM fact_cells_v2 c JOIN fact_observations_v2 o USING(fact_cell_id) JOIN fact_cell_identity_seals_v2 s USING(fact_cell_id)"
    ).fetchall()
    assert len(rows) == 18
    for concept, start, end, unit, dimensions, lexical, raw_locator in rows:
        locator = json.loads(raw_locator)
        assert dimensions == "[]"
        assert locator["scope_representation"] == "displayed_table_labels_not_xbrl_dimensions"
        assert locator["source_node_ids"] and locator["definition_node_ids"]
        assert locator["displayed_scope"]["geography"] in {"Total", "consolidated"}
        if concept == "diluted_weighted_average_shares":
            assert start.startswith("2026-01-01") and end.startswith("2026-06-30")
            assert unit == "shares" and lexical == "50,697,299"
        if concept == "productive_asset_expenditures":
            assert lexical.startswith("(")
            assert locator["normalization"]["multiplier"] == "-1000000"
    assert database.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert database.execute("SELECT count(*) FROM canonical_metrics").fetchone()[0] == 0
    assert (
        database.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == 0
    )
    assert tuple(
        database.execute(
            "SELECT expected_node_count,observed_node_count,reported_fact_count FROM fact_extraction_run_completeness_seals_v2"
        ).fetchone()
    ) == (18, 18, 18)
    before = database.total_changes
    replay = publish_meli_reported_statements(
        database,
        request.model_copy(update={"apply": True, "recorded_at": STAMP + timedelta(days=1)}),
    )
    assert replay.exact_replay and replay.publication_id == applied.publication_id
    assert database.total_changes == before
    current = next(p for p in preview.population if p.concept == "diluted_weighted_average_shares")
    assert (current.period_start, current.period_end) == (START, END)


@pytest.mark.parametrize(
    ("old", "new", "concept"),
    [
        (
            b"accounting principles generally accepted in the U.S.",
            b"International Financial Reporting Standards",
            "commerce_revenues",
        ),
        (b"are stated in U.S. dollars", b"are stated in Brazilian reais", "gross_loans_receivable"),
        (b"(In millions)", b"(In millions of Brazilian reais)", "fintech_revenues"),
        (
            b"(In millions of U.S. dollars, except for share data)",
            b"(In thousands of U.S. dollars, except for share data)",
            "diluted_weighted_average_shares",
        ),
        (
            b"Weighted average of outstanding common shares",
            b"Weighted average of outstanding common shares (in millions)",
            "diluted_weighted_average_shares",
        ),
        (b"Six Months Ended", b"Three Months Ended", "commerce_revenues"),
        (b"Other Countries", b"Other Geographies", "fintech_revenues"),
        (b"Total commerce revenues", b"Domestic commerce revenues", "commerce_revenues"),
        (b"Credit revenues", b"Lending cash collections", "credit_revenues"),
        (
            b"Diluted earnings per share",
            b"Basic earnings per share",
            "diluted_weighted_average_shares",
        ),
        (b"Loans receivable, net", b"Gross loans receivable", "gross_loans_receivable"),
        (b"(In millions)", b"(In thousands)", "gross_loans_receivable"),
        (b"except for share data", b"including share data", "diluted_weighted_average_shares"),
        (
            b"Non-current liabilities:",
            b"Non-current assets:",
            "noncurrent_operating_lease_liabilities",
        ),
        (
            b"Includes interest earned on loans and advances",
            b"Includes principal received on loans and advances",
            "credit_revenues",
        ),
    ],
)
def test_source_coordinate_changes_reject(
    database: sqlite3.Connection, tmp_path: Path, old: bytes, new: bytes, concept: str
) -> None:
    raw = _raw()
    assert old in raw
    request = seed_reported_html(database, tmp_path, raw.replace(old, new))
    result = publish_meli_reported_statements(database, request.model_copy(update={"apply": True}))
    assert not result.source_population_complete
    assert all(p.status == "rejected" for p in result.population if p.concept == concept)
    assert result.rejected_count > 0
    assert (
        database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0]
        == result.captured_count
    )


def test_changed_values_are_parsed_not_asserted(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    raw = _raw().replace(b">10,630<", b">10,631<")
    assert raw != _raw()
    request = seed_reported_html(database, tmp_path, raw)
    result = publish_meli_reported_statements(database, request)
    member = next(
        p for p in result.population if p.population_key == "commerce_revenues_current_h1"
    )
    assert member.normalized_numeric_value == "10631000000"


def test_conflicting_duplicate_is_rejected(database: sqlite3.Connection, tmp_path: Path) -> None:
    # Only one presentation of the consolidated total changes; the disaggregation
    # total retains its independently displayed number.
    raw = _raw().replace(b">19,014<", b">19,015<", 1)
    assert raw != _raw()
    request = seed_reported_html(database, tmp_path, raw)
    result = publish_meli_reported_statements(database, request)
    member = next(p for p in result.population if p.population_key == "total_revenues_current_h1")
    assert member.status == "rejected" and member.reason_code == "conflicting_displayed_duplicates"


def test_changed_raw_bytes_refuse_before_publication(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = seed_reported_html(database, tmp_path, _raw())
    (tmp_path / "source.html").write_bytes(_raw() + b" ")
    before = database.total_changes
    with pytest.raises(ValueError, match="byte commitment"):
        publish_meli_reported_statements(database, request.model_copy(update={"apply": True}))
    assert database.total_changes == before


def test_cli_help_names_source_only(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])
    assert error.value.code == 0
    assert "18-member" in capsys.readouterr().out


@pytest.mark.parametrize("unit", [b"%", b"$"])
def test_share_row_unit_override_is_not_ignored(
    database: sqlite3.Connection, tmp_path: Path, unit: bytes
) -> None:
    raw = _raw()
    at = raw.index(b'id="f-221"')
    end = raw.index(b"</tr>", at)
    raw = raw[:end] + b"<td>" + unit + b"</td>" + raw[end:]
    request = seed_reported_html(database, tmp_path, raw)
    result = publish_meli_reported_statements(database, request)
    member = next(p for p in result.population if p.concept == "diluted_weighted_average_shares")
    assert member.status == "rejected"


def test_partial_population_replays_without_new_writes(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = seed_reported_html(
        database, tmp_path, _raw().replace(b"Credit revenues", b"Lending cash collections")
    ).model_copy(update={"apply": True})
    first = publish_meli_reported_statements(database, request)
    # Every revenue member depends on the exact business/product population.
    # A renamed component invalidates all eight revenue observations.
    assert first.captured_count == 10 and first.rejected_count == 8
    assert {item.concept for item in first.population if item.status == "rejected"} == {
        "commerce_revenues",
        "total_revenues",
        "fintech_revenues",
        "credit_revenues",
    }
    assert not first.source_population_complete and first.publication_id
    assert tuple(
        database.execute(
            "SELECT expected_node_count,observed_node_count,reported_fact_count "
            "FROM fact_extraction_run_completeness_seals_v2"
        ).fetchone()
    ) == (18, 18, 10)
    before = database.total_changes
    replay = publish_meli_reported_statements(database, request)
    assert replay.exact_replay and replay.publication_id == first.publication_id
    assert not replay.source_population_complete
    assert database.total_changes == before


def test_publication_failure_rolls_back_statement_evidence(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from provenance.source_fact_repository import SourceFactPublication, SourceFactRepository

    request = seed_reported_html(database, tmp_path, _raw()).model_copy(update={"apply": True})

    def fail(_self: SourceFactRepository, _publication: SourceFactPublication) -> None:
        raise ValueError("injected statement publication failure")

    monkeypatch.setattr(SourceFactRepository, "publish", fail)
    with pytest.raises(ValueError, match="injected statement publication failure"):
        publish_meli_reported_statements(database, request)
    assert not database.in_transaction
    assert (
        database.execute(
            "SELECT count(*) FROM evidence_extraction_runs WHERE extractor_name='meli-reported-statements'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT count(*) FROM evidence_nodes WHERE node_id LIKE 'meli-statement-node:%'"
        ).fetchone()[0]
        == 0
    )
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_caller_transaction_is_preserved(database: sqlite3.Connection, tmp_path: Path) -> None:
    request = seed_reported_html(database, tmp_path, _raw()).model_copy(update={"apply": True})
    database.execute("BEGIN")
    with pytest.raises(ValueError, match="idle connection"):
        publish_meli_reported_statements(database, request)
    assert database.in_transaction
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0
    database.rollback()


def test_real_cli_dry_apply_replay(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = seed_reported_html(database, tmp_path, _raw())
    path = str(database.execute("PRAGMA database_list").fetchone()["file"])

    def now(_tz: object) -> datetime:
        return STAMP

    monkeypatch.setattr("execution.publish_meli_reported_tables.datetime", SimpleNamespace(now=now))
    argv = [
        "--db",
        path,
        "--inventory-key",
        request.inventory_key,
        "--accession",
        request.accession_number,
        "--document-version-id",
        request.document_version_id,
        "--fulltext-run-id",
        request.fulltext_run_id,
        "--content-root",
        str(tmp_path),
    ]
    assert cli.main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "dry_run" and preview["captured_count"] == 18
    assert preview["publication_id"] is None
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0
    assert cli.main([*argv, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["publication_id"] and not applied["exact_replay"]
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 18
    assert cli.main([*argv, "--apply"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["exact_replay"] and replay["publication_id"] == applied["publication_id"]
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 18


ANNUAL_URL = "https://www.sec.gov/Archives/edgar/data/1099590/000109959026000006/meli-20251231.htm"
ANNUAL_ACCESSION = "0001099590-26-000006"
ANNUAL_END = datetime(2025, 12, 31, tzinfo=UTC)


def _seed_annual(
    conn: sqlite3.Connection, tmp_path: Path, raw: bytes, monkeypatch: pytest.MonkeyPatch
) -> ReportedTableRequest:
    # Reuse the native synthetic inventory fixture; change metadata before its
    # append-only authorities persist it. This fixture is not a real SEC capture.
    def annual_document(**data: object) -> DocumentVersion:
        return DocumentVersion.model_validate({**data, "form_type": "10-K"})

    def annual_expected(**data: object) -> ExpectedDocument:
        return ExpectedDocument.model_validate(
            {**data, "form_type": "10-K", "primary_document": "meli-20251231.htm"}
        )

    with monkeypatch.context() as patch:
        patch.setattr(table_fixtures, "END", ANNUAL_END)
        patch.setattr(table_fixtures, "START", ANNUAL_END.replace(month=1, day=1))
        patch.setattr(table_fixtures, "URL", ANNUAL_URL)
        patch.setattr(table_fixtures, "ACCESSION", ANNUAL_ACCESSION)
        patch.setattr(table_fixtures, "DocumentVersion", annual_document)
        patch.setattr(table_fixtures, "ExpectedDocument", annual_expected)
        return table_fixtures.seed_reported_html(
            conn, tmp_path, raw, document_accession=ANNUAL_ACCESSION
        )


def test_annual_statement_gate_keeps_management_tables_h1_only(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _seed_annual(database, tmp_path, _raw(), monkeypatch)
    # The annual source metadata is allowed only by the statement publisher.
    # An H1 body inside this synthetic annual document must not become FY facts.
    preview = publish_meli_reported_statements(database, request)
    assert preview.recipe == "meli-fy-displayed-statements.v1"
    assert preview.expected_count == 7
    assert preview.captured_count == 0 and preview.rejected_count == 7
    assert {p.population_key for p in preview.population} == {
        concept + "_current_fy"
        for concept in (
            "commerce_revenues",
            "total_revenues",
            "fintech_revenues",
            "credit_revenues",
            "operating_income",
            "depreciation_and_amortization",
            "productive_asset_expenditures",
        )
    }
    assert preview.canonical_admission == "not_performed"
    assert preview.document_completeness == "not_claimed"
    with pytest.raises(ValueError, match="H1 recipe"):
        publish_meli_reported_tables(database, request)
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def _annual_raw() -> bytes:
    """Synthetic HTML with the exact qualified 2025 headings and selected rows.

    Values/labels come from the retained annual layout diagnostic (source SHA
    3f2ddbb4d2f4d770cc90b1081dfca2d899824c8df84162ad7e28759c0bc2efb2).
    Generated wrappers and omitted rows make this a parser fixture, not a capture.
    """

    def table(rows: list[list[str]]) -> str:
        return (
            "<table>"
            + "".join(
                "<tr>" + "".join("<td>" + html.escape(cell) + "</td>" for cell in row) + "</tr>"
                for row in rows
            )
            + "</table>"
        )

    income = table(
        [
            ["Year Ended December 31,"],
            ["2025", "2024", "2023 (1)"],
            ["Net revenues and financial income", "28,893", "20,777", "15,107"],
            ["Operating expenses:"],
            ["Income from operations", "3,201", "2,631", "2,207"],
        ]
    )
    cashflow = table(
        [
            ["Year Ended December 31,"],
            ["2025", "2024", "2023"],
            ["Cash flows from operations:"],
            ["Adjustments to reconcile net income to net cash provided by operating activities:"],
            ["Depreciation and amortization", "818", "617", "524"],
            ["Cash flows from investing activities:"],
            [
                "Investments in property and equipment, intangible assets and intangible assets at fair value",
                "(1,343)",
                "(860)",
                "(509)",
            ],
        ]
    )
    revenue = table(
        [
            ["Year Ended December 31, 2025"],
            ["Brazil", "Mexico", "Argentina", "Other Countries (7)", "Total"],
            ["(In millions)"],
            ["Commerce services (1)", "6,859", "3,432", "1,722", "737", "12,750"],
            ["Commerce products sales (2)", "2,325", "753", "312", "154", "3,544"],
            ["Total commerce revenues", "9,184", "4,185", "2,034", "891", "16,294"],
            ["Financial services and income (3)", "2,829", "897", "2,609", "343", "6,678"],
            ["Credit revenues (4)", "3,158", "1,370", "1,315", "15", "5,858"],
            ["Fintech products sales (5)", "30", "23", "4", "6", "63"],
            ["Total fintech revenues", "6,017", "2,290", "3,928", "364", "12,599"],
            [
                "Total net revenues and financial income",
                "15,201",
                "6,475",
                "5,962",
                "1,255",
                "28,893",
            ],
        ]
    )
    # The annual KPI capex population differs from the full productive-asset
    # cash-flow row. It must never substitute for that model input.
    kpi = table(
        [
            ["Year Ended December 31, (1)"],
            ["2025", "2024", "2023"],
            ["(In millions, except percentages)"],
            ["Capital expenditures", "1,327", "860", "509"],
            ["Depreciation and amortization", "818", "617", "524"],
        ]
    )
    passages = [
        "The accompanying audited consolidated financial statements are prepared in conformity with accounting principles generally accepted in the U.S. (",
        "These audited consolidated financial statements are stated in U.S. dollars, except for where otherwise indicated.",
        "Includes final value fees and flat fees paid by sellers derived from intermediation services and related shipping and storage fees, classified fees derived from classified advertising services, ad sales and membership subscription fees.",
        "Includes revenues from inventory sales and related shipping fees.",
        "Includes revenues from commissions the Company charges for transactions off-platform derived from use of the Company",
        "Includes interest earned on loans and advances granted to merchants and consumers, and interest and commissions earned on Mercado Pago credit card transactions.",
        "Includes sales of mobile point of sales devices.",
    ]
    raw = (
        "<html><body><p><span>Consolidated Statements of Income</span></p>"
        "<p><span>(In millions of U.S. dollars, except for share data)</span></p>"
        + income
        + "<p><span>Consolidated Statements of Cash Flows</span></p><p><span>(In millions of U.S. dollars)</span></p>"
        + cashflow
        + revenue
        + kpi
        + "".join("<p><span>" + html.escape(text) + "</span></p>" for text in passages)
        + "</body></html>"
    )
    return raw.encode()


def test_annual_seven_flows_dry_apply_replay(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _seed_annual(database, tmp_path, _annual_raw(), monkeypatch)
    before = database.total_changes
    preview = publish_meli_reported_statements(database, request)
    assert database.total_changes == before
    assert preview.source_population_complete, [
        (p.population_key, p.reason_code) for p in preview.population
    ]
    assert preview.recipe == statements.FY_RECIPE and preview.expected_count == 7
    assert preview.captured_count == 7 and preview.rejected_count == 0
    assert {p.population_key: p.normalized_numeric_value for p in preview.population} == {
        "commerce_revenues_current_fy": "16294000000",
        "total_revenues_current_fy": "28893000000",
        "fintech_revenues_current_fy": "12599000000",
        "credit_revenues_current_fy": "5858000000",
        "operating_income_current_fy": "3201000000",
        "depreciation_and_amortization_current_fy": "818000000",
        "productive_asset_expenditures_current_fy": "1343000000",
    }
    assert all(
        p.period_start == ANNUAL_END.replace(month=1, day=1) and p.period_end == ANNUAL_END
        for p in preview.population
    )
    applied = publish_meli_reported_statements(database, request.model_copy(update={"apply": True}))
    assert applied.publication_id and not applied.exact_replay
    assert applied.canonical_admission == "not_performed"
    assert applied.document_completeness == "not_claimed"
    assert tuple(
        database.execute(
            "SELECT expected_node_count,observed_node_count,reported_fact_count FROM fact_extraction_run_completeness_seals_v2"
        ).fetchone()
    ) == (7, 7, 7)
    assert {r[0] for r in database.execute("SELECT fiscal_period FROM fact_cells_v2")} == {"FY"}
    assert database.execute("SELECT count(*) FROM canonical_metrics").fetchone()[0] == 0
    assert {r[0] for r in database.execute("SELECT method_version FROM fact_observations_v2")} == {
        statements.FY_RECIPE
    }
    before = database.total_changes
    replay = publish_meli_reported_statements(
        database,
        request.model_copy(update={"apply": True, "recorded_at": STAMP + timedelta(days=1)}),
    )
    assert replay.exact_replay and replay.publication_id == applied.publication_id
    assert database.total_changes == before


@pytest.mark.parametrize(
    ("old", "new", "concept"),
    [
        (b"Year Ended December 31, 2025", b"Year Ended December 31, 2024", "commerce_revenues"),
        (b"Other Countries (7)", b"Other Geographies (7)", "fintech_revenues"),
        (b"(In millions)", b"(In thousands)", "credit_revenues"),
        (b"Commerce products sales (2)", b"Other product sales (2)", "commerce_revenues"),
        (
            b"accounting principles generally accepted in the U.S.",
            b"International Financial Reporting Standards",
            "operating_income",
        ),
        (
            b"are stated in U.S. dollars",
            b"are stated in Brazilian reais",
            "depreciation_and_amortization",
        ),
        (
            b"(In millions of U.S. dollars)",
            b"(In thousands of U.S. dollars)",
            "productive_asset_expenditures",
        ),
        (b"merchants and consumers", b"selected borrowers", "credit_revenues"),
        (b">(1,343)<", b">1,343<", "productive_asset_expenditures"),
        (b"<td>2023 (1)</td>", b"<td>2023</td>", "operating_income"),
    ],
)
def test_annual_context_changes_reject(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old: bytes,
    new: bytes,
    concept: str,
) -> None:
    raw = _annual_raw()
    assert old in raw
    request = _seed_annual(database, tmp_path, raw.replace(old, new), monkeypatch)
    result = publish_meli_reported_statements(database, request)
    assert not result.source_population_complete
    member = next(p for p in result.population if p.concept == concept)
    assert member.status == "rejected"
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_annual_conflicting_total_rejects(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _annual_raw().replace(b">28,893<", b">28,894<", 1)
    request = _seed_annual(database, tmp_path, raw, monkeypatch)
    result = publish_meli_reported_statements(database, request)
    member = next(p for p in result.population if p.concept == "total_revenues")
    assert member.status == "rejected" and member.reason_code == "conflicting_displayed_duplicates"
