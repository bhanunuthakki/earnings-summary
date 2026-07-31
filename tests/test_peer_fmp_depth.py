"""Peer-depth contract for index_member FMP caching (2026-07-30 DB-size audit).

index_member peers get only the 8 shallow file families in
save_fmp_data.PEER_ENDPOINT_ALLOWLIST; the full catalog is unchanged for the
active universe (portfolio/watchlist/evaluation/none) and etf keeps its
skip-10-K-only behavior. execution/truncate_peer_fmp_cache.py trims the
pre-existing full-depth peer files and must stay in sync with the allowlist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

# save_fmp_data sys.exit(1)s at import if FMP_API_KEY is unset; seed a dummy
# key first (no test here touches the network).
os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.save_fmp_data as sfd
import execution.truncate_peer_fmp_cache as trunc

Job = dict[str, object]


def _jobs(list_type: str) -> list[Job]:
    return cast("list[Job]", sfd.per_ticker_jobs("FAKE", list_type=list_type))


def _suffixes(jobs: list[Job]) -> set[str]:
    return {cast("str", j["suffix"]) for j in jobs}


def _extra(jobs: list[Job], suffix: str) -> dict[str, object]:
    matches = [j for j in jobs if j["suffix"] == suffix]
    assert len(matches) == 1, f"expected exactly one {suffix} job, got {len(matches)}"
    return cast("dict[str, object]", matches[0]["extra"])


# ---------------------------------------------------------------------------
# per_ticker_jobs depth branching
# ---------------------------------------------------------------------------


def test_index_member_jobs_are_exactly_the_peer_allowlist() -> None:
    assert _suffixes(_jobs("index_member")) == set(sfd.PEER_ENDPOINT_ALLOWLIST)


def test_index_member_depths_match_consumer_needs() -> None:
    jobs = _jobs("index_member")
    # screens._rev_yoy_at(inc, 4) reads index 8 -> needs 9 quarters.
    assert _extra(jobs, "income_statement_quarterly")["limit"] == 9
    assert _extra(jobs, "key_metrics_quarterly")["limit"] == 4
    assert _extra(jobs, "balance_sheet_quarterly")["limit"] == 1
    # Market cap is windowed to ~140 calendar days (~90 trading rows).
    mc = _extra(jobs, "historical_market_cap")
    assert mc["from"] == sfd.PEER_MARKET_CAP_FROM
    assert mc["to"] == sfd.TODAY_STR


def test_portfolio_jobs_keep_full_depth() -> None:
    jobs = _jobs("portfolio")
    assert len(jobs) > 60
    assert _extra(jobs, "income_statement_quarterly")["limit"] == 100
    assert any(s.startswith("form_10k_") for s in _suffixes(jobs))


def test_etf_jobs_only_skip_10k() -> None:
    jobs = _jobs("etf")
    suffixes = _suffixes(jobs)
    assert not any(s.startswith("form_10k_") for s in suffixes)
    # Full depth otherwise — the peer allowlist does not apply to etf rows.
    assert _extra(jobs, "income_statement_quarterly")["limit"] == 100
    assert "price_chart_10y_div_adj" in suffixes


# ---------------------------------------------------------------------------
# Truncator <-> allowlist sync guard
# ---------------------------------------------------------------------------


def test_truncator_tables_match_allowlist() -> None:
    assert set(trunc.KEEP_FULL) | set(trunc.TRUNCATE_DEPTH) == set(sfd.PEER_ENDPOINT_ALLOWLIST)
    assert not set(trunc.KEEP_FULL) & set(trunc.TRUNCATE_DEPTH)
    # Depth-limited quarterly families keep exactly what the fetch limit pulls.
    for suffix in (
        "income_statement_quarterly",
        "key_metrics_quarterly",
        "balance_sheet_quarterly",
    ):
        allow = sfd.PEER_ENDPOINT_ALLOWLIST[suffix]
        assert allow is not None
        assert trunc.TRUNCATE_DEPTH[suffix] == allow["limit"]
    # KEEP_FULL families are single-record-ish and keep the catalog's params;
    # they must map to None in the allowlist.
    for suffix in trunc.KEEP_FULL:
        assert sfd.PEER_ENDPOINT_ALLOWLIST[suffix] is None


# ---------------------------------------------------------------------------
# Truncator filesystem behavior
# ---------------------------------------------------------------------------


def _seed_peer_files(fmp_dir: Path) -> None:
    fmp_dir.mkdir(parents=True, exist_ok=True)
    income = [
        {"date": f"20{25 - (i // 4)}-{(12 - 3 * (i % 4)):02d}-30", "revenue": 100 + i}
        for i in range(20)
    ]
    (fmp_dir / "ZZZT_income_statement_quarterly.json").write_text(
        json.dumps(income), encoding="utf-8"
    )
    (fmp_dir / "ZZZT_profile.json").write_text(
        json.dumps([{"companyName": "Zzz Test"}]), encoding="utf-8"
    )
    (fmp_dir / "ZZZT_form_10k_2020.json").write_text(
        json.dumps({"big": "x" * 100}), encoding="utf-8"
    )
    (fmp_dir / "ZZZT_as_reported_income_annual.json").write_text(json.dumps([]), encoding="utf-8")
    # A different ticker sharing the prefix letters must never be touched.
    (fmp_dir / "ZZZTX_form_10k_2020.json").write_text(json.dumps({}), encoding="utf-8")


def test_truncator_dry_run_changes_nothing(tmp_path: Path) -> None:
    _seed_peer_files(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = trunc.process_ticker(tmp_path, "ZZZT", apply=False)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert result.deleted_files == 2  # form_10k + as_reported
    assert result.truncated_files == 1
    assert result.deleted_bytes > 0


def test_truncator_apply_trims_to_contract(tmp_path: Path) -> None:
    _seed_peer_files(tmp_path)
    result = trunc.process_ticker(tmp_path, "ZZZT", apply=True)
    assert result.deleted_files == 2
    assert result.truncated_files == 1

    assert not (tmp_path / "ZZZT_form_10k_2020.json").exists()
    assert not (tmp_path / "ZZZT_as_reported_income_annual.json").exists()
    assert (tmp_path / "ZZZT_profile.json").exists()
    assert (tmp_path / "ZZZTX_form_10k_2020.json").exists()  # other ticker untouched

    kept_raw: object = json.loads(
        (tmp_path / "ZZZT_income_statement_quarterly.json").read_text("utf-8")
    )
    assert isinstance(kept_raw, list)
    kept = cast("list[dict[str, object]]", kept_raw)
    assert len(kept) == trunc.TRUNCATE_DEPTH["income_statement_quarterly"]
    dates = [str(r["date"]) for r in kept]
    assert dates == sorted(dates, reverse=True)  # newest-first retained

    # Idempotent: second pass finds nothing left to trim.
    again = trunc.process_ticker(tmp_path, "ZZZT", apply=True)
    assert again.deleted_files == 0
    assert again.truncated_files == 0
