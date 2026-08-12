from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import DocType  # noqa: E402
from pipeline.approved_ir_catalog import (  # noqa: E402
    CatalogDisposition,
    IrCatalogError,
    build_catalog,
    classify_link,
)
from pipeline.approved_ir_rubrik import (  # noqa: E402
    RubrikLinkObservation,
    RubrikQuarterObservation,
    load_rubrik_row_observations,
    parse_rubrik_quarter_rows,
)
from pipeline.approved_ir_wix import (  # noqa: E402
    WixPanelObservation,
    load_wix_rendered_observations,
    parse_wix_visible_quarters,
)
from pipeline.source_policy import ir_url_is_authorized, issuer_policy  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "approved_ir"
RUBRIK_FIXTURE = FIXTURES / "rubrik_rows_sanitized.json"
WIX_FIXTURE = FIXTURES / "wix_rendered_sequence_sanitized.json"


def _rubrik_observations() -> tuple[RubrikQuarterObservation, ...]:
    return load_rubrik_row_observations(RUBRIK_FIXTURE.read_text(encoding="utf-8"))


def test_exact_endpoint_authorization_rejects_suffix_and_path_confusion() -> None:
    rubrik = issuer_policy("RBRK")
    assert ir_url_is_authorized(
        rubrik.ir,
        "https://ir.rubrik.com/static-files/q2.pdf?download=1",
    )
    assert not ir_url_is_authorized(rubrik.ir, "http://ir.rubrik.com/static-files/q2.pdf")
    assert not ir_url_is_authorized(rubrik.ir, "https://evil.ir.rubrik.com/static-files/q2.pdf")
    assert not ir_url_is_authorized(rubrik.ir, "https://ir.rubrik.com/static-files-evil/q2.pdf")
    assert not ir_url_is_authorized(rubrik.ir, "https://ir.rubrik.com/other/q2.pdf")
    assert not ir_url_is_authorized(
        rubrik.ir,
        "https://ir.rubrik.com/static-files/../other/q2.pdf",
    )
    assert not ir_url_is_authorized(
        rubrik.ir,
        "https://ir.rubrik.com/static-files/%2e%2e/other/q2.pdf",
    )
    assert not ir_url_is_authorized(
        rubrik.ir,
        "https://ir.rubrik.com/static-files/%252e%252e/other/q2.pdf",
    )
    assert not ir_url_is_authorized(
        rubrik.ir,
        "https://ir.rubrik.com/static-files/%252f..%252fother/q2.pdf",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov/Archives/../secrets/report.htm",
        "https://www.sec.gov/Archives/%2e%2e/secrets/report.htm",
        "https://www.sec.gov/Archives/%252e%252e/secrets/report.htm",
        "https://www.sec.gov/Archives/%252f..%252fsecrets/report.htm",
        "http://www.sec.gov/Archives/edgar/data/report.htm",
    ],
)
def test_sec_handoff_rejects_raw_and_encoded_traversal(url: str) -> None:
    with pytest.raises(IrCatalogError, match="escaped the approved SEC archive"):
        classify_link(
            issuer_policy("RBRK"),
            quarter_end=date(2026, 7, 31),
            title="10-Q",
            url=url,
            declared_kind="sec-filing",
            observation_key="rbrk-2026-07-31",
            evidence_locator="row > 10-Q",
        )


def test_link_classification_hands_off_sec_and_transcript_and_excludes_webcast() -> None:
    parsed = parse_rubrik_quarter_rows(_rubrik_observations(), policy=issuer_policy("RBRK"))
    dispositions = {entry.disposition for entry in parsed.entries}
    assert dispositions >= {
        CatalogDisposition.IR_DOCUMENT,
        CatalogDisposition.SEC_HANDOFF,
        CatalogDisposition.TRANSCRIPT_CANDIDATE,
    }
    transcript = next(
        entry
        for entry in parsed.entries
        if entry.disposition is CatalogDisposition.TRANSCRIPT_CANDIDATE
    )
    assert transcript.doc_type is DocType.IR_TRANSCRIPT
    assert transcript.observation_key == "rbrk-2026-07-31"
    assert transcript.evidence_locator
    assert parsed.excluded_webcast_count == 1


def test_rubrik_rows_keep_latest_five_quarters_and_dedupe() -> None:
    policy = issuer_policy("RBRK")
    parsed = parse_rubrik_quarter_rows(_rubrik_observations(), policy=policy)
    catalog = build_catalog(policy, parsed)
    assert catalog.reported_quarters == (
        date(2026, 7, 31),
        date(2026, 4, 30),
        date(2026, 1, 31),
        date(2025, 10, 31),
        date(2025, 7, 31),
    )
    assert len([entry for entry in catalog.entries if "presentation" in entry.url]) == 1
    assert catalog.authority_url == policy.ir.authority_url
    assert catalog.adapter_key == policy.ir.adapter_key.value
    assert catalog.adapter_version == "rubrik-quarter-rows-v1"
    assert len(catalog.observations) == 6


@pytest.mark.parametrize("only_webcast", [False, True])
def test_newest_observed_empty_period_displaces_sixth_period(only_webcast: bool) -> None:
    observations = _rubrik_observations()
    links: tuple[RubrikLinkObservation, ...] = ()
    if only_webcast:
        links = (
            RubrikLinkObservation(
                title="Webcast replay",
                url="https://events.example.test/newest",
                declared_kind="webcast",
                evidence_locator="row[newest] > webcast",
            ),
        )
    newest = RubrikQuarterObservation(
        observation_key="rbrk-2026-10-31",
        authority_url=issuer_policy("RBRK").ir.authority_url,
        raw_sha256="1111111111111111111111111111111111111111111111111111111111111111",
        quarter_end=date(2026, 10, 31),
        row_locator="quarter-row[Third Quarter Fiscal 2027]",
        links=links,
    )
    policy = issuer_policy("RBRK")
    catalog = build_catalog(
        policy,
        parse_rubrik_quarter_rows((newest, *observations), policy=policy),
    )
    assert catalog.reported_quarters[0] == date(2026, 10, 31)
    assert date(2025, 7, 31) not in catalog.reported_quarters


def test_catalog_hash_is_order_independent_and_bound_to_raw_and_policy_hashes() -> None:
    policy = issuer_policy("RBRK")
    parsed = parse_rubrik_quarter_rows(_rubrik_observations(), policy=policy)
    forward = build_catalog(policy, parsed)
    reverse = build_catalog(
        policy,
        parsed.model_copy(
            update={
                "entries": tuple(reversed(parsed.entries)),
                "observations": tuple(reversed(parsed.observations)),
            }
        ),
    )
    assert forward.catalog_sha256 == reverse.catalog_sha256
    changed_policy = policy.model_copy(update={"policy_version": "changed"})
    assert build_catalog(changed_policy, parsed).catalog_sha256 != forward.catalog_sha256
    changed_observation = parsed.observations[0].model_copy(
        update={"raw_sha256": "2222222222222222222222222222222222222222222222222222222222222222"}
    )
    changed_raw = parsed.model_copy(
        update={"observations": (changed_observation, *parsed.observations[1:])}
    )
    assert build_catalog(policy, changed_raw).catalog_sha256 != forward.catalog_sha256


def test_duplicate_evidence_selection_is_order_independent() -> None:
    policy = issuer_policy("RBRK")
    parsed = parse_rubrik_quarter_rows(_rubrik_observations(), policy=policy)
    original = next(
        entry for entry in parsed.entries if entry.disposition is CatalogDisposition.IR_DOCUMENT
    )
    original_observation = next(
        item for item in parsed.observations if item.observation_key == original.observation_key
    )
    second_observation = original_observation.model_copy(
        update={
            "observation_key": "same-period-second-observation",
            "raw_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
            "evidence_locator": "second-render > same-quarter",
        }
    )
    duplicate = original.model_copy(
        update={
            "observation_key": second_observation.observation_key,
            "evidence_locator": "second-render > same-link",
        }
    )
    expanded = parsed.model_copy(
        update={
            "entries": (*parsed.entries, duplicate),
            "observations": (*parsed.observations, second_observation),
        }
    )
    reversed_expanded = expanded.model_copy(
        update={
            "entries": tuple(reversed(expanded.entries)),
            "observations": tuple(reversed(expanded.observations)),
        }
    )
    assert (
        build_catalog(policy, expanded).catalog_sha256
        == build_catalog(policy, reversed_expanded).catalog_sha256
    )


def test_catalog_rejects_cross_disposition_duplicate_and_unbound_observation() -> None:
    policy = issuer_policy("RBRK")
    parsed = parse_rubrik_quarter_rows(_rubrik_observations(), policy=policy)
    document = next(
        entry for entry in parsed.entries if entry.disposition is CatalogDisposition.IR_DOCUMENT
    )
    conflicting = document.model_copy(
        update={
            "disposition": CatalogDisposition.TRANSCRIPT_CANDIDATE,
            "doc_type": DocType.IR_TRANSCRIPT,
        }
    )
    with pytest.raises(IrCatalogError, match="conflicting classification"):
        build_catalog(policy, parsed.model_copy(update={"entries": (*parsed.entries, conflicting)}))
    with pytest.raises(IrCatalogError, match="no matching source observation"):
        build_catalog(
            policy,
            parsed.model_copy(
                update={"entries": (document.model_copy(update={"observation_key": "missing"}),)}
            ),
        )


def test_wix_rendered_sequence_accepts_one_visible_panel_per_quarter() -> None:
    payload = WIX_FIXTURE.read_text(encoding="utf-8")
    observations = load_wix_rendered_observations(payload)
    assert len(observations) == 5
    parsed = parse_wix_visible_quarters(observations, policy=issuer_policy("WIX"))
    assert len(parsed.observed_reporting_periods) == 5
    assert all("stale" not in entry.url and "mobile" not in entry.url for entry in parsed.entries)
    assert parsed.excluded_webcast_count == 1
    assert {entry.observation_key for entry in parsed.entries} <= {
        item.observation_key for item in observations
    }


def test_rendered_observation_boundary_validates_raw_hash() -> None:
    payload = WIX_FIXTURE.read_text(encoding="utf-8").replace("a" * 64, "not-a-sha", 1)
    with pytest.raises(ValidationError, match="raw_sha256"):
        load_wix_rendered_observations(payload)


@pytest.mark.parametrize("visible_panel_count", [0, 2])
def test_wix_fails_closed_without_exactly_one_visible_selected_panel(
    visible_panel_count: int,
) -> None:
    observations = load_wix_rendered_observations(WIX_FIXTURE.read_text(encoding="utf-8"))
    first = observations[0]
    visible_panel = first.panels[0]
    hidden_panel = first.panels[1]
    panels: tuple[WixPanelObservation, ...]
    if visible_panel_count == 0:
        panels = (visible_panel.model_copy(update={"visible": False}), hidden_panel)
    else:
        panels = (
            visible_panel,
            hidden_panel.model_copy(
                update={
                    "selected": True,
                    "visible": True,
                    "quarter_end": first.requested_quarter_end,
                }
            ),
        )
    ambiguous = first.model_copy(update={"panels": panels})
    with pytest.raises(IrCatalogError, match="exactly one visible selected Wix panel"):
        parse_wix_visible_quarters((ambiguous,), policy=issuer_policy("WIX"))
