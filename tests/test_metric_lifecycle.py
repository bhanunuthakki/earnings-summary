"""Tests for the P1 discontinued-metric detector (``src/filings/metric_lifecycle.py``
+ ``src/filings/metric_triage.py``).

Weighted toward the four noise sources the empirical calibration session
measured (docs/design/disclosure_change_signals.md §2), because a
happy-path-only suite here would pass while the detector reproduces the
175-candidates-for-META noise:

1. an annual-only tag must never be judged against the quarterly axis;
2. an irregular-cadence tag must not flag within its own historical
   tolerance, but MUST flag once it exceeds it;
3. a taxonomy relabel (MELI's PP&E-tag rename, reproduced synthetically
   here) must classify as ``metric_relabeled``, never ``metric_discontinued``;
4. an accounting-standard transition — a tag stopped by several OTHER
   cached tickers within the same window — must classify as
   ``metric_standard_transition``, while a genuinely company-specific stop
   (shared by no one else) must NOT be swept up by that gate.

Also covers: the "(Deprecated ...)" taxonomy-annotation strip (without it,
a real tag's relabel match is missed on containment alone), idempotent
writes, and that an LLM triage failure degrades loudly (no candidate is
ever silently marked "plumbing").
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import metric_lifecycle as ml  # noqa: E402
from filings.metric_lifecycle import (  # noqa: E402
    Axis,
    CandidateKind,
    build_standard_transition_corpus,
    candidate_to_event,
    group_by_concept,
    list_cached_tickers,
    load_tag_series,
    run_lifecycle_detection,
    write_lifecycle_events,
)
from filings.models import HardStopError  # noqa: E402

_DISCLOSURE_EVENTS_SCHEMA = """
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    form VARCHAR(8),
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    prior_fiscal_year INTEGER,
    prior_fiscal_period VARCHAR(4),
    source_ref VARCHAR(255),
    source_doc_id INTEGER,
    canonical_id VARCHAR(64),
    subject VARCHAR(255) NOT NULL,
    subject_label TEXT,
    prior_excerpt TEXT,
    current_excerpt TEXT,
    evidence_quote TEXT,
    materiality FLOAT,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified',
    interpretation_md TEXT,
    confidence FLOAT,
    detector_version VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_disclosure_events UNIQUE
        (ticker, event_type, fiscal_year, fiscal_period, subject, detector_version)
);
"""


def _events_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_DISCLOSURE_EVENTS_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Synthetic companyfacts fixtures
# ---------------------------------------------------------------------------


def _annual_entry(fy: int, val: float, accn: str | None = None) -> dict[str, object]:
    return {
        "end": f"{fy}-12-31",
        "val": val,
        "accn": accn or f"acc-{fy}-10k",
        "fy": fy,
        "fp": "FY",
        "form": "10-K",
        "filed": f"{fy + 1}-02-15",
    }


def _quarterly_entry(fy: int, q: int, val: float, accn: str | None = None) -> dict[str, object]:
    month = {1: 3, 2: 6, 3: 9}[q]
    return {
        "end": f"{fy}-{month:02d}-28",
        "val": val,
        "accn": accn or f"acc-{fy}-q{q}",
        "fy": fy,
        "fp": f"Q{q}",
        "form": "10-Q",
        "filed": f"{fy}-{month + 1:02d}-15",
    }


def _companyfacts(tags: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {
        "cik": "0000000001",
        "entityName": "SYNTHETIC INC",
        "facts": {
            "us-gaap": {
                tag: {"label": tag, "units": {"USD": entries}} for tag, entries in tags.items()
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {"label": "shares", "units": {"shares": []}}
            },
        },
    }


def _write_companyfacts(
    repo_root: Path, ticker: str, tags: dict[str, list[dict[str, object]]]
) -> None:
    sec_dir = repo_root / "data" / "historical" / "sec"
    sec_dir.mkdir(parents=True, exist_ok=True)
    import json

    (sec_dir / f"{ticker}_companyfacts.json").write_text(
        json.dumps(_companyfacts(tags)), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Noise source 1 — axis conflation
# ---------------------------------------------------------------------------


def test_annual_only_tag_not_flagged_on_quarterly_axis(tmp_path: Path) -> None:
    tags = {
        # Establishes the QUARTERLY axis reference at 2024 Q3.
        "AlwaysQuarterly": [
            _quarterly_entry(fy, q, 100.0 + fy) for fy in range(2019, 2025) for q in (1, 2, 3)
        ],
        # Establishes the ANNUAL axis reference at FY2023 (annual filings lag).
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2015, 2024)],
        # Annual-only tag, current as of FY2023 -- must NOT be judged against
        # the quarterly axis's much more recent 2024 Q3 reference, which
        # would make it look wildly, nonsensically silent.
        "AnnualOnlyCurrentTag": [_annual_entry(fy, 500.0 + fy) for fy in range(2015, 2024)],
    }
    _write_companyfacts(tmp_path, "AXIS", tags)

    result = run_lifecycle_detection(tmp_path, "AXIS")
    assert result is not None
    flagged = {c.qualified_name for c in result.candidates} | {
        c.qualified_name for c in result.relabeled_pairs
    }
    assert "us-gaap:AnnualOnlyCurrentTag" not in flagged


# ---------------------------------------------------------------------------
# Noise source 2 — irregular cadence, calibrated per-tag
# ---------------------------------------------------------------------------


def test_irregular_cadence_tag_not_flagged_within_own_tolerance(tmp_path: Path) -> None:
    tags = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2025)],
        # Appears at 2015,2016,2018,2019,2021,2022 -- gaps of 1,2,1,2,1, so
        # this tag's own historical max gap is 2. Its last appearance is
        # FY2022 and the axis reference is FY2024, so current silence is
        # exactly 2 -- NOT more than its own precedent -- must not flag.
        "IrregularWithinTolerance": [
            _annual_entry(fy, 50.0 + fy) for fy in (2015, 2016, 2018, 2019, 2021, 2022)
        ],
    }
    _write_companyfacts(tmp_path, "IRRG", tags)

    result = run_lifecycle_detection(tmp_path, "IRRG")
    assert result is not None
    flagged = {c.qualified_name for c in result.candidates}
    assert "us-gaap:IrregularWithinTolerance" not in flagged


def test_irregular_cadence_tag_flagged_once_silence_exceeds_tolerance(tmp_path: Path) -> None:
    tags = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2025)],
        # Same gap profile (max gap 2) as above, but last appearance is
        # FY2021 -- current silence is 2024-2021 = 3, exceeding the tag's own
        # historical max gap of 2. Must flag.
        "IrregularTooSilent": [
            _annual_entry(fy, 50.0 + fy) for fy in (2014, 2015, 2016, 2018, 2019, 2021)
        ],
    }
    _write_companyfacts(tmp_path, "IRRG2", tags)

    result = run_lifecycle_detection(tmp_path, "IRRG2")
    assert result is not None
    flagged = {c.qualified_name: c for c in result.candidates}
    assert "us-gaap:IrregularTooSilent" in flagged
    cand = flagged["us-gaap:IrregularTooSilent"]
    assert cand.historical_max_gap == 2
    assert cand.current_silence == 3
    assert cand.kind is CandidateKind.DISCONTINUED


# ---------------------------------------------------------------------------
# Noise source 3 — taxonomy relabels (MELI's PP&E-tag rename, reproduced)
# ---------------------------------------------------------------------------


def test_relabeled_tag_classifies_as_metric_relabeled_not_discontinued(tmp_path: Path) -> None:
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysQuarterly": [
            _quarterly_entry(fy, q, 100.0 + fy) for fy in range(2019, 2025) for q in (1, 2, 3)
        ],
        # Old tag: reports every quarter 2020Q1..2022Q3, then stops. Values
        # stay in the ~110-130 range (flat across years) so the magnitude
        # comparison against the ~135-140 replacement tag is meaningful --
        # NOT scaled by fiscal year, which would make the two incomparable.
        # Tag NAME (this fixture uses label == tag) deliberately shares
        # "Payments"/"Acquire" vocabulary with its replacement below, exactly
        # as MELI's real relabel did -- an unrelated tag name would correctly
        # fail the label-containment gate even at the right period/magnitude.
        "PaymentsToAcquirePropertyPlantAndEquipment": [
            _quarterly_entry(fy, q, 100.0 + q * 10) for fy in (2020, 2021, 2022) for q in (1, 2, 3)
        ],
        # New tag debuts at FY2022 (rank = 2022*4+4 = one unified period
        # after the old tag's last, 2022 Q3 = 2022*4+3) with a comparable
        # value, then continues -- a pure rename (the real MELI tag names).
        "PaymentsToAcquireProductiveAssets": [
            _annual_entry(2022, 135.0, accn="acc-2022-10k-new"),
            _quarterly_entry(2023, 1, 137.0, accn="acc-2023-q1-new"),
            _quarterly_entry(2023, 2, 140.0, accn="acc-2023-q2-new"),
        ],
    }
    _write_companyfacts(tmp_path, "RELBL", tags)

    result = run_lifecycle_detection(tmp_path, "RELBL")
    assert result is not None
    old_name = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
    new_name = "us-gaap:PaymentsToAcquireProductiveAssets"
    discontinued_names = {c.qualified_name for c in result.candidates}
    relabeled_names = {c.qualified_name: c for c in result.relabeled_pairs}

    assert old_name not in discontinued_names
    assert old_name in relabeled_names
    match = relabeled_names[old_name]
    assert match.kind is CandidateKind.RELABELED
    assert match.relabeled_to == new_name
    assert match.relabel_period_gap is not None
    assert abs(match.relabel_period_gap) <= ml.RELABEL_PERIOD_WINDOW


def test_meta_ppe_relabel_reproduced_from_directive_example(tmp_path: Path) -> None:
    """Reproduces the exact case named in the design doc: a PP&E-style tag
    stops and an ASU-842-style combined tag picks it up at a comparable
    magnitude -- confirms both suppression conditions (period proximity +
    magnitude) independently, not just that SOME match was found."""
    tags: dict[str, list[dict[str, object]]] = {
        # Extends TWO years past the old tag's stop (2020 Q3) so the old tag
        # reads as genuinely silent against the ticker's own later filings,
        # not merely current-as-of-its-last-appearance.
        "AlwaysQuarterly": [
            _quarterly_entry(fy, q, 100.0 + fy) for fy in range(2015, 2022) for q in (1, 2, 3)
        ],
        # Regular Q1-Q3 cadence through 2020 (so its OWN historical max gap
        # is only the systematic Q3-to-next-Q1 jump, not an artifact of a
        # skipped quarter), with the real value on its final appearance.
        "PropertyPlantAndEquipmentNet": [
            _quarterly_entry(fy, q, 2000.0 + q) for fy in range(2015, 2020) for q in (1, 2, 3)
        ]
        + [
            _quarterly_entry(2020, 1, 2001.0),
            _quarterly_entry(2020, 2, 2002.0),
            _quarterly_entry(2020, 3, 42291.0),
        ],
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization": [
            _annual_entry(2020, 45000.0, accn="acc-2020-10k-new"),
            _quarterly_entry(2021, 1, 46000.0, accn="acc-2021-q1-new"),
        ],
    }
    _write_companyfacts(tmp_path, "PPETEST", tags)

    result = run_lifecycle_detection(tmp_path, "PPETEST")
    assert result is not None
    relabeled_names = {c.qualified_name: c for c in result.relabeled_pairs}
    assert "us-gaap:PropertyPlantAndEquipmentNet" in relabeled_names
    discontinued_names = {c.qualified_name for c in result.candidates}
    assert "us-gaap:PropertyPlantAndEquipmentNet" not in discontinued_names


def test_deprecated_annotation_stripped_before_relabel_containment(tmp_path: Path) -> None:
    """Real case found in coordinator review: the US-GAAP taxonomy annotates
    a retired element's OWN label with "(Deprecated YYYY-MM-DD)"
    (``AdvertisingRevenue`` -> "Advertising Revenue (Deprecated 2018-01-31)",
    replaced by ``RevenueFromContractWithCustomerExcludingAssessedTax`` —
    sharing only the "revenue" token, exactly reproduced here). Left
    unstripped, "deprecated" inflates the OLD tag's token denominator from 2
    to 3: containment drops from 1/2 = 0.5 (passes 0.4) to 1/3 = 0.33
    (fails), missing an otherwise-valid match."""
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2020)],
        "AlwaysQuarterly": [
            _quarterly_entry(fy, q, 100.0 + fy) for fy in range(2010, 2020) for q in (1, 2, 3)
        ],
        "AdvertisingRevenue": [_annual_entry(fy, 40.0 + fy - 2010) for fy in range(2010, 2018)],
        # Debuts in the FIRST QUARTERLY filing after adoption (unified rank
        # is only +1 from a prior calendar year's ANNUAL rank, since annual
        # entries sit at "quarter 4" of the unified rank and a Q1 entry the
        # next year is the very next slot) — exactly how the real
        # AdvertisingRevenue -> RevenueFromContractWithCustomer... pairing
        # actually lines up (its earliest appearance is a 10-Q, not a 10-K).
        # A PURE annual-to-annual pairing one calendar year apart is 4
        # unified-rank units apart and would NOT clear this gate — an
        # accepted, documented shape of the window, not a bug.
        "RevenueFromContractExcludingTax": [
            _quarterly_entry(2018, 1, 41.0, accn="acc-2018-q1-new"),
            _annual_entry(2018, 42.0, accn="acc-2018-10k-new"),
            _annual_entry(2019, 43.0, accn="acc-2019-new"),
        ],
    }
    _write_companyfacts(tmp_path, "DEPR", tags)

    # Patch in the real taxonomy annotation this test exists to strip
    # (the auto-generated fixture label is just the bare tag name).
    import json

    facts_path = tmp_path / "data" / "historical" / "sec" / "DEPR_companyfacts.json"
    payload = json.loads(facts_path.read_text())
    payload["facts"]["us-gaap"]["AdvertisingRevenue"]["label"] = (
        "Advertising Revenue (Deprecated 2018-01-31)"
    )
    facts_path.write_text(json.dumps(payload))

    # Confirm the WITHOUT-strip scenario actually fails containment (a plain
    # word-split, not the module's own tokenizer, so this checks the claim
    # independently rather than trivially): {advertising, revenue,
    # deprecated, 2018-01-31} shares only "revenue" with the new label's
    # vocabulary -> well below the 0.4 threshold.
    unstripped_words = {
        w.lower().strip("(),")
        for w in ["Advertising", "Revenue", "(Deprecated", "2018-01-31)"]
        if len(w.strip("(),")) > 2
    }
    assert (
        len(unstripped_words & {"revenue"}) / len(unstripped_words)
        < ml.RELABEL_LABEL_CONTAINMENT_MIN
    )

    result = run_lifecycle_detection(tmp_path, "DEPR")
    assert result is not None
    relabeled_names = {c.qualified_name: c for c in result.relabeled_pairs}
    assert "us-gaap:AdvertisingRevenue" in relabeled_names, (
        "the (Deprecated ...) annotation must not block an otherwise-valid relabel match"
    )
    assert (
        relabeled_names["us-gaap:AdvertisingRevenue"].relabeled_to
        == "us-gaap:RevenueFromContractExcludingTax"
    )


# ---------------------------------------------------------------------------
# Noise source 4 — cross-sectional accounting-standard transitions
# ---------------------------------------------------------------------------


def test_tag_stopped_by_several_tickers_classifies_as_standard_transition(
    tmp_path: Path,
) -> None:
    """Three tickers independently stop the SAME tag in the SAME fiscal
    year (the ASU-842-style signature: many companies, one adoption
    window) — the corpus-built cross-sectional gate must reclassify it as
    ``metric_standard_transition``, never ``metric_discontinued``."""
    shared_tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2024)],
        "SharedScheduleTag": [_annual_entry(fy, 10.0 + fy) for fy in range(2012, 2019)],
    }
    for ticker in ("STDA", "STDB", "STDC"):
        _write_companyfacts(tmp_path, ticker, shared_tags)

    corpus = build_standard_transition_corpus(tmp_path, ["STDA", "STDB", "STDC"])
    assert corpus.tickers_covered == ["STDA", "STDB", "STDC"]
    assert "us-gaap:SharedScheduleTag" in corpus.stop_events
    assert len(corpus.stop_events["us-gaap:SharedScheduleTag"]) == 3

    result = run_lifecycle_detection(tmp_path, "STDA", standard_transition_corpus=corpus)
    assert result is not None
    assert result.standard_transition_gate_ran is True
    assert result.standard_transition_comparison_tickers == 2  # STDB, STDC

    st_names = {c.qualified_name: c for c in result.standard_transition_pairs}
    assert "us-gaap:SharedScheduleTag" in st_names
    assert st_names["us-gaap:SharedScheduleTag"].kind is CandidateKind.STANDARD_TRANSITION
    assert st_names["us-gaap:SharedScheduleTag"].standard_transition_other_tickers == 2
    discontinued_names = {c.qualified_name for c in result.candidates}
    assert "us-gaap:SharedScheduleTag" not in discontinued_names


def test_company_specific_stop_not_swept_into_standard_transition(tmp_path: Path) -> None:
    """A tag only ONE ticker ever reports must NOT be reclassified — the
    gate requires >= ``STANDARD_TRANSITION_MIN_OTHER_TICKERS`` OTHER
    tickers, and a company-specific event has zero."""
    shared_tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2024)],
        "SharedScheduleTag": [_annual_entry(fy, 10.0 + fy) for fy in range(2012, 2019)],
    }
    for ticker in ("STDA", "STDB", "STDC"):
        _write_companyfacts(tmp_path, ticker, shared_tags)
    # UNIQ has its OWN one-off stopped tag that nobody else shares.
    uniq_tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2024)],
        "UniqueCompanyTag": [_annual_entry(fy, 5.0 + fy) for fy in range(2012, 2019)],
    }
    _write_companyfacts(tmp_path, "UNIQ", uniq_tags)

    corpus = build_standard_transition_corpus(tmp_path, ["STDA", "STDB", "STDC", "UNIQ"])
    result = run_lifecycle_detection(tmp_path, "UNIQ", standard_transition_corpus=corpus)
    assert result is not None
    st_names = {c.qualified_name for c in result.standard_transition_pairs}
    assert "us-gaap:UniqueCompanyTag" not in st_names
    discontinued_names = {c.qualified_name for c in result.candidates}
    assert "us-gaap:UniqueCompanyTag" in discontinued_names


def test_standard_transition_gate_degrades_honestly_without_corpus(tmp_path: Path) -> None:
    """No corpus supplied (single-ticker call, no cross-sectional data) must
    NOT silently read as "confirmed company-specific" — the result records
    ``standard_transition_gate_ran=False`` and the candidate stays
    ``metric_discontinued`` (the safe default: never promoted without a
    confirming corpus)."""
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2024)],
        "SharedScheduleTag": [_annual_entry(fy, 10.0 + fy) for fy in range(2012, 2019)],
    }
    _write_companyfacts(tmp_path, "STDA", tags)

    result = run_lifecycle_detection(tmp_path, "STDA")  # no standard_transition_corpus
    assert result is not None
    assert result.standard_transition_gate_ran is False
    assert result.standard_transition_comparison_tickers == 0
    assert result.standard_transition_pairs == []
    discontinued_names = {c.qualified_name for c in result.candidates}
    assert "us-gaap:SharedScheduleTag" in discontinued_names


def test_list_cached_tickers_finds_every_companyfacts_file(tmp_path: Path) -> None:
    for ticker in ("AAA", "BBB", "CCC"):
        _write_companyfacts(tmp_path, ticker, {"SomeTag": [_annual_entry(2020, 1.0)]})
    assert list_cached_tickers(tmp_path) == ["AAA", "BBB", "CCC"]


def test_list_cached_tickers_empty_when_no_data_dir(tmp_path: Path) -> None:
    assert list_cached_tickers(tmp_path) == []


# ---------------------------------------------------------------------------
# Materiality + before/after count plumbing
# ---------------------------------------------------------------------------


def test_naive_count_is_at_least_calibrated_count(tmp_path: Path) -> None:
    """The naive baseline (mixed axes, no relabel suppression) must never
    under-count relative to the calibrated pipeline -- it is the noisier,
    pre-suppression measure by construction."""
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysQuarterly": [
            _quarterly_entry(fy, q, 100.0 + fy) for fy in range(2019, 2025) for q in (1, 2, 3)
        ],
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2015, 2024)],
        "AnnualOnlyCurrentTag": [_annual_entry(fy, 500.0 + fy) for fy in range(2015, 2024)],
    }
    _write_companyfacts(tmp_path, "NAIVE", tags)
    result = run_lifecycle_detection(tmp_path, "NAIVE")
    assert result is not None
    assert result.naive_count >= result.after_gap_calibration >= result.after_relabel_suppression


def test_no_companyfacts_returns_none(tmp_path: Path) -> None:
    assert run_lifecycle_detection(tmp_path, "NOPE") is None


def test_dropped_malformed_entries_do_not_crash_ticker(tmp_path: Path) -> None:
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2015, 2024)],
        "Malformed": [{"end": "2020-12-31"}, "not-a-dict", _annual_entry(2020, 1.0)],  # type: ignore[list-item]
    }
    _write_companyfacts(tmp_path, "DROP", tags)
    series = load_tag_series(tmp_path, "DROP")
    assert series is not None
    concepts = group_by_concept(series)
    # The two garbage entries are dropped; the one well-formed entry survives.
    assert len(concepts[("us-gaap", "Malformed")].observations) == 1


# ---------------------------------------------------------------------------
# Idempotent writes
# ---------------------------------------------------------------------------


def test_write_lifecycle_events_idempotent(tmp_path: Path) -> None:
    tags: dict[str, list[dict[str, object]]] = {
        "AlwaysAnnual": [_annual_entry(fy, 1000.0 + fy) for fy in range(2010, 2025)],
        "IrregularTooSilent": [
            _annual_entry(fy, 50.0 + fy) for fy in (2014, 2015, 2016, 2018, 2019, 2021)
        ],
    }
    _write_companyfacts(tmp_path, "IDEMP", tags)
    result = run_lifecycle_detection(tmp_path, "IDEMP")
    assert result is not None and result.candidates

    events = [candidate_to_event(c) for c in result.candidates]
    conn = _events_conn()
    try:
        first = write_lifecycle_events(conn, events)
        second = write_lifecycle_events(conn, events)
        assert first == len(events)
        assert second == len(events)
        (count,) = conn.execute("SELECT COUNT(*) FROM disclosure_events").fetchone()
        assert count == len(events)
    finally:
        conn.close()


def test_write_lifecycle_events_missing_table_raises_hard_stop() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HardStopError):
            write_lifecycle_events(
                conn,
                [
                    candidate_to_event(
                        ml.MetricCandidate(
                            ticker="X",
                            taxonomy="us-gaap",
                            tag="Foo",
                            label="Foo",
                            axis=Axis.ANNUAL,
                            last_observation=ml.XbrlObservation(
                                fiscal_year=2020,
                                fiscal_period="FY",
                                form="10-K",
                                end="2020-12-31",
                                val=1.0,
                                accn="acc",
                                filed="2021-02-01",
                            ),
                            current_silence=3,
                            historical_max_gap=1,
                        )
                    )
                ],
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM triage degrades honestly
# ---------------------------------------------------------------------------


def test_triage_degrades_loudly_on_llm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filings import metric_triage

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated transient LLM failure")

    monkeypatch.setattr(metric_triage, "call_llm_structured", _boom)

    candidate = ml.MetricCandidate(
        ticker="X",
        taxonomy="us-gaap",
        tag="SomeMetric",
        label="Some Metric",
        axis=Axis.ANNUAL,
        last_observation=ml.XbrlObservation(
            fiscal_year=2020,
            fiscal_period="FY",
            form="10-K",
            end="2020-12-31",
            val=1.0,
            accn="acc",
            filed="2021-02-01",
        ),
        current_silence=3,
        historical_max_gap=1,
    )
    outcome = metric_triage.triage_candidates("X", [candidate])
    assert outcome.degraded is True
    assert outcome.verdicts == {}  # never a fabricated verdict on any candidate


def test_triage_propagates_hard_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from filings import metric_triage
    from llm.cli import LLMSetupError

    def _boom(*args: object, **kwargs: object) -> object:
        raise LLMSetupError("claude CLI not found")

    monkeypatch.setattr(metric_triage, "call_llm_structured", _boom)

    candidate = ml.MetricCandidate(
        ticker="X",
        taxonomy="us-gaap",
        tag="SomeMetric",
        label="Some Metric",
        axis=Axis.ANNUAL,
        last_observation=ml.XbrlObservation(
            fiscal_year=2020,
            fiscal_period="FY",
            form="10-K",
            end="2020-12-31",
            val=1.0,
            accn="acc",
            filed="2021-02-01",
        ),
        current_silence=3,
        historical_max_gap=1,
    )
    with pytest.raises(LLMSetupError):
        metric_triage.triage_candidates("X", [candidate])
