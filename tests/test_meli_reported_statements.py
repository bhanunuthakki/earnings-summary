"""Displayed HTML source publication, using real migrated evidence authorities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from execution import publish_meli_reported_statements as cli
from provenance.meli_reported_statements import publish_meli_reported_statements
from sqlite_runtime import register_sqlite_integrity_functions
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
    assert (
        manifest["source_sha256"]
        == "b605310e398baa79b50ea488075c32d6522d8966438135cf25e8fa9f2a448924"
    )
    assert len(manifest["fragments"]) == 81


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
