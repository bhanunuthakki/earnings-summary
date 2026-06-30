"""Piece 3 — Cohesity IPO S-1 watch via EDGAR full-text search."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.sec_watch import (  # noqa: E402
    SecWatch,
    check_s1_watch,
    load_watches,
    parse_efts_hits,
    run,
    s1_watch_status,
)

from ._competitive_fixtures import news_conn_path  # noqa: E402

_WATCH = SecWatch(
    entity_name="Cohesity",
    attributed_ticker="RBRK",
    fulltext_query="Cohesity",
    forms=["S-1"],
    cik=None,
    min_file_date="2026-01-01",
)


def _hit(
    *, display: str, form: str, file_date: str, adsh: str, cik: str, doc: str
) -> dict[str, object]:
    return {
        "_id": f"{adsh}:{doc}",
        "_source": {
            "display_names": [display],
            "ciks": [cik],
            "form": form,
            "root_forms": ["S-1"],
            "file_date": file_date,
            "adsh": adsh,
        },
    }


# A Rubrik-filed S-1 that MENTIONS Cohesity (the real false positive — Rubrik's
# 2024 S-1 names Cohesity dozens of times), here dated 2026 so ONLY the filer
# filter can exclude it.
_RUBRIK_HIT = _hit(
    display="Rubrik, Inc.  (RBRK)  (CIK 0001943896)",
    form="S-1",
    file_date="2026-02-01",
    adsh="0001193125-26-000111",
    cik="0001943896",
    doc="rbrk-s1.htm",
)
_COHESITY_S1 = _hit(
    display="Cohesity, Inc.  (CIK 0001821984)",
    form="S-1",
    file_date="2026-03-15",
    adsh="0001193125-26-000999",
    cik="0001821984",
    doc="coh-s1.htm",
)
_COHESITY_S1A = _hit(
    display="Cohesity, Inc.  (CIK 0001821984)",
    form="S-1/A",
    file_date="2026-04-01",
    adsh="0001193125-26-001222",
    cik="0001821984",
    doc="coh-s1a.htm",
)
_COHESITY_OLD = _hit(
    display="Cohesity, Inc.  (CIK 0001821984)",
    form="S-1",
    file_date="2025-06-01",  # before the watch's min_file_date
    adsh="0001193125-25-000001",
    cik="0001821984",
    doc="coh-old.htm",
)


def _payload(*hits: dict[str, object]) -> dict[str, object]:
    return {"hits": {"total": {"value": len(hits)}, "hits": list(hits)}}


# --------------------------------------------------------------------------- #
# Pure parse: filer filter is the load-bearing correctness guard
# --------------------------------------------------------------------------- #
def test_filer_filter_excludes_rubriks_own_s1_that_mentions_cohesity() -> None:
    rows = parse_efts_hits(_payload(_RUBRIK_HIT, _COHESITY_S1), _WATCH)
    assert len(rows) == 1
    row = rows[0]
    assert row.ticker == "RBRK"  # attributed to the affected holding, not the filer
    assert "Cohesity" in row.headline
    assert "1821984" in row.url and "1943896" not in row.url  # Cohesity's CIK, not Rubrik's
    assert row.source_feed == "edgar_s1_watch"


def test_form_prefix_matches_amendments() -> None:
    rows = parse_efts_hits(_payload(_COHESITY_S1, _COHESITY_S1A), _WATCH)
    forms_in_headlines = sorted(r.headline.split(" files ")[1].split(" ")[0] for r in rows)
    assert forms_in_headlines == ["S-1", "S-1/A"]  # S-1 watch also catches S-1/A


def test_min_file_date_excludes_older_filings() -> None:
    rows = parse_efts_hits(_payload(_COHESITY_OLD), _WATCH)
    assert rows == []


def test_published_at_is_canonical_from_file_date() -> None:
    rows = parse_efts_hits(_payload(_COHESITY_S1), _WATCH)
    assert rows[0].published_at == "2026-03-15 00:00:00"


def test_empty_or_malformed_payload_is_empty() -> None:
    assert parse_efts_hits({}, _WATCH) == []
    assert parse_efts_hits({"hits": {}}, _WATCH) == []
    assert parse_efts_hits("not a dict", _WATCH) == []


def test_cik_match_path() -> None:
    watch = _WATCH.model_copy(update={"cik": "0001821984"})
    # display name without "Cohesity" but matching CIK still counts as the filer.
    hit = _hit(
        display="CDOT Holdings (CIK 0001821984)",
        form="S-1",
        file_date="2026-03-15",
        adsh="0001193125-26-000999",
        cik="0001821984",
        doc="x.htm",
    )
    rows = parse_efts_hits(_payload(hit), watch)
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# check_s1_watch — injectable fetch, degrades on failure
# --------------------------------------------------------------------------- #
def test_check_s1_watch_returns_cohesity_rows_only() -> None:
    def fake_fetch(_url: str) -> object:
        return _payload(_RUBRIK_HIT, _COHESITY_S1, _COHESITY_S1A)

    rows = check_s1_watch([_WATCH], fetch_fn=fake_fetch)
    assert len(rows) == 2  # S-1 + S-1/A, Rubrik excluded
    assert all(r.ticker == "RBRK" for r in rows)


def test_check_s1_watch_degrades_on_fetch_error() -> None:
    def boom(_url: str) -> object:
        raise ValueError("network down")

    assert check_s1_watch([_WATCH], fetch_fn=boom) == []


def test_check_s1_watch_empty_when_no_hits() -> None:
    assert check_s1_watch([_WATCH], fetch_fn=lambda _u: _payload()) == []


# --------------------------------------------------------------------------- #
# run() + resolver — end to end against a temp news DB, using the REAL config
# --------------------------------------------------------------------------- #
def test_run_persists_and_status_flips(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    news_conn_path(str(db))

    # Before any filing: status is "not filed".
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    assert s1_watch_status(conn, entity="Cohesity", attributed_ticker="RBRK").filed is False
    conn.close()

    inserted, _deduped = run(
        PROJECT_ROOT,  # real committed sec_watch.json (Cohesity -> RBRK)
        db_path=str(db),
        fetch_fn=lambda _u: _payload(_RUBRIK_HIT, _COHESITY_S1),
    )
    assert inserted == 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    status = s1_watch_status(conn, entity="Cohesity", attributed_ticker="RBRK")
    conn.close()
    assert status.filed is True
    assert status.filed_date == "2026-03-15"
    assert status.url and "1821984" in status.url


def test_committed_watch_config_targets_cohesity() -> None:
    watches = load_watches(PROJECT_ROOT)
    assert any(w.entity_name == "Cohesity" and w.attributed_ticker == "RBRK" for w in watches)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
