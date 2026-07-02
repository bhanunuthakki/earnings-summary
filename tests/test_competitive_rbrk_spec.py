"""RBRK.json competitive spec ↔ code consistency, and holdings_sync round-trip.

The competitive spec (the ``competitive_tracking`` object + its tier-2 KPIs) is
authored by the owner; this instrumentation FEEDS those existing KPIs rather than
inventing new ones. These tests pin that contract so a rename on either side
fails loudly.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive import (  # noqa: E402
    OWNER_KPI_CATEGORY_SHARE,
    OWNER_KPI_INCREMENTAL,
    SYNCED_KPI_NAMES,
)
from competitive.category_share import ingest_category_share  # noqa: E402
from competitive.holdings_sync import resolve_synced_current, sync_holdings  # noqa: E402

from ._competitive_fixtures import kpi_conn  # noqa: E402

_RBRK = PROJECT_ROOT / "micro_thesis" / "holdings" / "RBRK.json"


def _load_rbrk() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(_RBRK.read_text(encoding="utf-8")))


def _dicts(value: object) -> list[dict[str, object]]:
    """Narrow a JSON value to a list of dict entries (pyright-strict friendly)."""
    assert isinstance(value, list)
    out: list[dict[str, object]] = []
    for item in cast("list[object]", value):
        assert isinstance(item, dict)
        out.append(cast("dict[str, object]", item))
    return out


def _strs(value: object) -> list[str]:
    assert isinstance(value, list)
    return [str(x) for x in cast("list[object]", value)]


def _tier_2_names(payload: dict[str, object]) -> set[str]:
    return {str(k["name"]) for k in _dicts(payload.get("tier_2_kpis"))}


def test_synced_kpi_names_are_owner_tier_2_entries() -> None:
    """Every KPI holdings_sync writes must be an existing owner tier-2 KPI — the
    instrumentation feeds the owner's spec, it does not invent parallel KPIs."""
    names = _tier_2_names(_load_rbrk())
    missing = [n for n in SYNCED_KPI_NAMES if n not in names]
    assert not missing, f"synced KPI names missing from RBRK.json tier_2: {missing}"


def test_competitive_tracking_reflects_now_plumbed_status() -> None:
    payload = _load_rbrk()
    ct = payload.get("competitive_tracking")
    assert isinstance(ct, dict)
    ctd = cast("dict[str, object]", ct)
    assert str(ctd.get("rival", "")).startswith("Cohesity")
    # The two items the owner labeled "needs_pipeline_plumbing" are now plumbed.
    assert _strs(ctd.get("needs_pipeline_plumbing")) == []
    pipelines = ctd.get("pipelines")
    assert isinstance(pipelines, dict)
    pdict = cast("dict[str, object]", pipelines)
    for key in ("category_share", "transcript_mentions", "s1_watch"):
        assert key in pdict, f"competitive_tracking.pipelines missing {key}"


def test_additive_only_existing_rules_untouched() -> None:
    payload = _load_rbrk()
    assert len(_dicts(payload.get("break_rules"))) == 3
    assert len(_dicts(payload.get("business_model_rules"))) == 7
    assert len(_dicts(payload.get("tier_1_kpis"))) >= 9


def test_holdings_sync_resolves_real_values_and_applies(tmp_path: Path) -> None:
    # Build a temp repo with the real RBRK.json + the real category-share seed.
    holdings_dir = tmp_path / "micro_thesis" / "holdings"
    comp_dir = tmp_path / "micro_thesis" / "competitive"
    holdings_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    shutil.copy(_RBRK, holdings_dir / "RBRK.json")
    shutil.copy(
        PROJECT_ROOT / "micro_thesis" / "competitive" / "RBRK_category_share.json",
        comp_dir / "RBRK_category_share.json",
    )

    conn = kpi_conn()
    ingest_category_share(conn, tmp_path, "RBRK")

    # The owner's composite category KPI now reads real stored values: RBRK's
    # Gartner MQ Leader position + Cohesity's 19% share.
    cat = resolve_synced_current(conn, "RBRK", OWNER_KPI_CATEGORY_SHARE)
    assert cat.has_value
    assert "Leader" in cat.current and "19%" in cat.current

    # The incremental-share KPI: S-1 watch live, not yet filed (no news table).
    inc = resolve_synced_current(conn, "RBRK", OWNER_KPI_INCREMENTAL)
    assert inc.has_value is False
    assert "WATCH LIVE" in inc.current

    # --apply mirrors the composed values into the owner tier-2 KPIs only.
    result = sync_holdings(conn, tmp_path, "RBRK", apply=True)
    assert result.applied == len(SYNCED_KPI_NAMES)

    updated = cast(
        "dict[str, object]",
        json.loads((holdings_dir / "RBRK.json").read_text(encoding="utf-8")),
    )
    by_name = {str(k["name"]): str(k["current"]) for k in _dicts(updated.get("tier_2_kpis"))}
    assert "Leader" in by_name[OWNER_KPI_CATEGORY_SHARE]
    # A non-competitive tier-2 KPI is left exactly as the analyst wrote it. The
    # analyst's Total Revenue Growth value evolves as new quarters print (Q1 FY27
    # etc.), so assert it is UNCHANGED from the source spec rather than hardcoding
    # a value that goes stale — the invariant is "sync leaves non-competitive
    # KPIs byte-for-byte as the analyst wrote them", not any specific number.
    src_by_name = {
        str(k["name"]): str(k["current"]) for k in _dicts(_load_rbrk().get("tier_2_kpis"))
    }
    assert by_name["Total Revenue Growth"] == src_by_name["Total Revenue Growth"]
