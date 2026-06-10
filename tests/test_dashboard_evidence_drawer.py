"""Tests for src/dashboard/evidence_drawer.py.

Exercises the three rendering paths:
  * happy path — populated evidence with summary + citations (incl. a
    fact_id citation linked to brief_provenance per_metric)
  * malformed JSON — renders the structured notice rather than raising
  * default-expanded — `<details open>` is present
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from alerts import AlertRow
from dashboard.evidence_drawer import render_evidence_drawer


def _make_alert(
    *,
    evidence: str,
    alert_id: int = 1,
    ticker: str = "GOOG",
    trigger_kind: str = "kpi_inflection",
) -> AlertRow:
    return AlertRow(
        id=alert_id,
        user_id="bhanu",
        ticker=ticker,
        trigger_kind=trigger_kind,
        fired_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        status="pending",
        memo_artifact_id=None,
        evidence_json=evidence,
        signature_sha="sig-test",
        dismissed_at=None,
        approved_at=None,
    )


# ----------------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------------


def test_renders_summary_and_citations_with_provenance_link() -> None:
    evidence = json.dumps(
        {
            "summary": "Cloud operating margin breached the 18% threshold this quarter.",
            "citations": [
                {
                    "kind": "transcript_line",
                    "locator": "GOOG-2026Q1-call-line-142",
                    "excerpt": "Cloud margins compressed sequentially due to AI infra build.",
                },
                {
                    "kind": "fact_id",
                    "locator": "operating_income_q_latest",
                    "excerpt": "Q1 2026 operating income for Cloud segment",
                },
                {
                    "kind": "news_url",
                    "locator": "https://example.com/goog-cloud-margins",
                    "excerpt": "Headline from press release.",
                },
            ],
        }
    )
    alert = _make_alert(evidence=evidence)
    brief_provenance: dict[str, object] = {
        "sources_used": {
            "per_metric": {
                "operating_income_q_latest": {
                    "source": "sec_official",
                    "fact_id": 4242,
                    "fetched_at": "2026-04-15T10:00:00Z",
                }
            }
        }
    }

    html = render_evidence_drawer(alert, brief_provenance=brief_provenance)

    # Default-expanded
    assert "<details open" in html
    # The three sections
    assert "Why this fired" in html
    assert "Source citations" in html
    assert "Raw evidence" in html
    # The narrative summary is rendered
    assert "Cloud operating margin breached" in html
    # Each citation kind is labeled in human-readable form
    assert "Transcript line" in html
    assert "Financial fact" in html
    assert "News article" in html
    # Locators appear
    assert "GOOG-2026Q1-call-line-142" in html
    assert "operating_income_q_latest" in html
    # Provenance from brief_provenance per_metric appears on the fact_id row
    assert "sec_official" in html
    assert "2026-04-15T10:00:00Z" in html


def test_no_brief_provenance_marks_fact_id_provenance_as_unavailable() -> None:
    evidence = json.dumps(
        {
            "summary": "summary",
            "citations": [
                {
                    "kind": "fact_id",
                    "locator": "revenue_q_latest",
                    "excerpt": "Q1 revenue figure",
                }
            ],
        }
    )
    alert = _make_alert(evidence=evidence)
    html = render_evidence_drawer(alert, brief_provenance=None)
    # The drawer should still render the citation but with a clear "no prov" marker
    assert "Financial fact" in html
    assert "revenue_q_latest" in html
    assert "no brief provenance" in html


def test_fact_id_with_unmatched_locator_renders_no_match() -> None:
    evidence = json.dumps(
        {
            "citations": [
                {
                    "kind": "fact_id",
                    "locator": "ebitda_q_latest",
                    "excerpt": "EBITDA",
                }
            ],
        }
    )
    alert = _make_alert(evidence=evidence)
    brief_provenance: dict[str, object] = {"sources_used": {"per_metric": {"revenue_q_latest": {}}}}
    html = render_evidence_drawer(alert, brief_provenance=brief_provenance)
    assert "no match" in html


def test_default_open_false_renders_collapsed() -> None:
    """``default_open=False`` (the Holding rail's compact cards) drops the
    ``open`` attribute — same sections, one click away."""
    alert = _make_alert(evidence=json.dumps({"summary": "compact context"}))
    html = render_evidence_drawer(alert, default_open=False)
    assert '<details class="evidence-drawer">' in html
    assert "<details open" not in html
    assert "Why this fired" in html  # content unchanged, just collapsed


# ----------------------------------------------------------------------------
# Malformed JSON path
# ----------------------------------------------------------------------------


def test_malformed_evidence_does_not_raise() -> None:
    alert = _make_alert(evidence="not-valid-json{{")
    # The contract: it doesn't raise.
    html = render_evidence_drawer(alert)
    assert "Malformed evidence" in html
    # Raw evidence is still surfaced, just outside the parsed sections.
    assert "Raw evidence" in html
    # The raw blob shows up in the page.
    assert "not-valid-json" in html


def test_evidence_that_is_not_a_json_object_is_malformed() -> None:
    """A bare JSON array or string is technically valid JSON but doesn't have
    the dict shape the drawer expects. Treated as malformed for consistency."""
    alert = _make_alert(evidence='["just", "a", "list"]')
    html = render_evidence_drawer(alert)
    assert "Malformed evidence" in html


def test_empty_evidence_string_is_malformed() -> None:
    alert = _make_alert(evidence="")
    html = render_evidence_drawer(alert)
    assert "Malformed evidence" in html


# ----------------------------------------------------------------------------
# Empty / partial evidence dicts
# ----------------------------------------------------------------------------


def test_evidence_without_summary_renders_no_summary_text() -> None:
    """A valid JSON object with no summary key still renders the section header
    but emits a 'no summary' muted note instead of crashing on a None."""
    alert = _make_alert(evidence=json.dumps({"citations": []}))
    html = render_evidence_drawer(alert)
    assert "Why this fired" in html
    assert "No summary supplied" in html


def test_evidence_without_citations_renders_no_citations_note() -> None:
    alert = _make_alert(evidence=json.dumps({"summary": "ok"}))
    html = render_evidence_drawer(alert)
    assert "No citations supplied" in html


# ----------------------------------------------------------------------------
# brief_provenance shape tolerance
# ----------------------------------------------------------------------------


def test_brief_provenance_without_sources_used_is_treated_as_no_provenance() -> None:
    """A brief_provenance row that doesn't carry sources_used at the top
    shouldn't crash the drawer — it should degrade to 'no match' for any
    fact_id citation."""
    evidence = json.dumps(
        {"citations": [{"kind": "fact_id", "locator": "revenue_q_latest", "excerpt": ""}]}
    )
    alert = _make_alert(evidence=evidence)
    html = render_evidence_drawer(alert, brief_provenance={"unrelated": "field"})
    # The drawer is robust to brief_provenance payloads that don't have
    # the expected nested shape.
    assert "Financial fact" in html
    assert "Malformed evidence" not in html


def test_summary_section_present_when_drawer_default_expanded() -> None:
    """Sanity check that the drawer's outer wrapper is the `<details open>`
    block (not a `<div>`) — the open attribute is part of the contract."""
    alert = _make_alert(evidence=json.dumps({"summary": "abc"}))
    html = render_evidence_drawer(alert)
    assert html.startswith("<details open")


def test_citations_nested_under_shifts_render_with_composed_locator() -> None:
    """earnings_tone nests its citations PER-SHIFT (``shifts[].citations``) with
    ``{period, line_number}`` rather than a top-level ``citations[]`` carrying a
    ``locator``. The drawer must gather those nested citations and compose a
    locator, so the richest evidence renders instead of degrading to "No
    citations supplied" (the regrade memo's #1 Richness gap — the drawer was a
    shell for every real alert)."""
    alert = _make_alert(
        trigger_kind="earnings_tone",
        evidence=json.dumps(
            {
                "summary": "Risk-adjusted NIM contracted.",
                "shifts": [
                    {
                        "topic": "NIM",
                        "direction": "softer",
                        "citations": [
                            {
                                "kind": "transcript_line",
                                "line_number": 165,
                                "period": "Q1-2026",
                                "excerpt": "risk-adjusted NIM came in at 9.5%, down 100bps",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    html = render_evidence_drawer(alert)
    assert "No citations supplied" not in html
    assert "Source citations" in html
    assert "Transcript line" in html
    assert "Q1-2026" in html
    assert "line 165" in html
    assert "down 100bps" in html
