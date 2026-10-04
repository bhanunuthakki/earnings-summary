"""Reviewed native SEC values use migrated, isolated source authorities."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.reviewed_sec_financial_tables import (
    ReviewedSecFinancialFact,
    ReviewedSecFinancialRequest,
    publish_reviewed_sec_financial_tables,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_source_fact_repository import seed_foundation

STAMP = datetime(2030, 1, 1, tzinfo=UTC)
END = datetime(2026, 6, 30, tzinfo=UTC)
URL = "https://www.sec.gov/Archives/edgar/data/1858985/000185898526000018/onfinancials.htm"
RAW = b"""<html><body><p>IFRS consolidated financial statements</p><table><tr><th>Six months ended June 30, 2026</th><th>CHF in millions</th></tr><tr><td>Net revenue</td><td>10.0</td></tr><tr><th>June 30, 2026</th><th>CHF in millions</th></tr><tr><td>Cash</td><td>2.5</td></tr></table></body></html>"""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seal(request: ReviewedSecFinancialRequest) -> ReviewedSecFinancialRequest:
    return request.model_copy(
        update={"review_sha256": _sha(request.canonical_review_json.encode())}
    )


def test_qualified_share_population_apply_and_later_review_reuse(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.test_reviewed_sec_financial_tables as fixture

    shares = b"""<table><tr><th>June 30, 2026</th><th>shares</th><th>Class A Shares</th><th>Class B Shares</th></tr><tr><td>Shares issued and outstanding</td><td>301715535</td><td>324991680</td></tr><tr><td>Awards with dilutive effects</td><td>1644629</td><td>2493692</td></tr></table>"""
    monkeypatch.setattr(fixture, "RAW", RAW.replace(b"</body>", shares + b"</body>"))
    original = synthetic_sec_request(database, tmp_path)
    rows = database.execute(
        "SELECT node_id,text,locator_sha256 FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='table_cell'",
        (original.facts[0].fulltext_run_id,),
    ).fetchall()
    nodes = {str(row["text"]): row for row in rows}
    facts: list[ReviewedSecFinancialFact] = []
    for key, label, number, header in (
        ("class_a_outstanding", "Shares issued and outstanding", "301715535", "Class A Shares"),
        ("class_b_outstanding", "Shares issued and outstanding", "324991680", "Class B Shares"),
        ("class_a_awards", "Awards with dilutive effects", "1644629", "Class A Shares"),
        ("class_b_awards", "Awards with dilutive effects", "2493692", "Class B Shares"),
    ):
        facts.append(
            original.facts[1].model_copy(
                update={
                    "fact_key": key,
                    "concept_name": label,
                    "value_node_id": str(nodes[number]["node_id"]),
                    "value_locator_sha256": str(nodes[number]["locator_sha256"]),
                    "raw_lexical_value": number,
                    "unit_key": "shares",
                    "currency": None,
                    "definition_text": label + "\n" + header,
                    "definition_node_ids": (
                        str(nodes[label]["node_id"]),
                        str(nodes[header]["node_id"]),
                    ),
                    "context_node_ids": (
                        str(nodes["June 30, 2026"]["node_id"]),
                        str(nodes["shares"]["node_id"]),
                    ),
                }
            )
        )
    review = _seal(
        original.model_copy(
            update={
                "facts": tuple(facts),
                "expected_fact_keys": tuple(fact.fact_key for fact in facts),
            }
        )
    )
    dry = publish_reviewed_sec_financial_tables(database, review)
    apply = review.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256})
    result = publish_reviewed_sec_financial_tables(database, apply)
    assert result.captured_count == 4
    reader = FactReadModel(database)
    by_key = {fact.fact_key: fact for fact in facts}
    for key, observation_id in result.observation_ids:
        evidence = reader.provenance_bundle(observation_id, cutoff=review.reviewed_at).evidence
        assert evidence is not None
        definition = evidence.source_locator.root["definition"]
        assert isinstance(definition, dict)
        assert definition["label"] == by_key[key].concept_name
        assert definition["wording"] == by_key[key].definition_text
    cells = database.execute(
        "SELECT fact_cell_id,concept_name,taxonomy_version,recorded_at FROM fact_cells_v2 ORDER BY concept_name"
    ).fetchall()
    assert {row["concept_name"] for row in cells} == {fact.definition_text for fact in facts}
    assert len({row["fact_cell_id"] for row in cells}) == 4
    assert publish_reviewed_sec_financial_tables(database, apply).exact_replay
    later = _seal(
        review.model_copy(
            update={
                "reviewed_at": review.reviewed_at + timedelta(seconds=1),
                "recorded_at": review.recorded_at + timedelta(seconds=1),
                "review_evidence": "Second independent synthetic source review",
            }
        )
    )
    later_dry = publish_reviewed_sec_financial_tables(database, later)
    later_result = publish_reviewed_sec_financial_tables(
        database,
        later.model_copy(
            update={
                "apply": True,
                "expected_plan_sha256": later_dry.plan_sha256,
            }
        ),
    )
    assert not later_result.exact_replay
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 8
    assert [tuple(row) for row in cells] == [
        tuple(row)
        for row in database.execute(
            "SELECT fact_cell_id,concept_name,taxonomy_version,recorded_at FROM fact_cells_v2 ORDER BY concept_name"
        ).fetchall()
    ]


@pytest.fixture
def database(migrated_db: Callable[..., Path], tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(migrated_db(tmp_path / "sec-reviewed.db"))
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def synthetic_sec_request(conn: sqlite3.Connection, root: Path) -> ReviewedSecFinancialRequest:
    seed_foundation(conn)
    path = root / "onfinancials.htm"
    path.write_bytes(RAW)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=_sha(RAW),
            byte_size=len(RAW),
            media_type="text/html",
            storage_uri=path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="sec-source",
            idempotency_key="sec-source",
            source_kind="sec_filing",
            source_url=URL,
            blob_sha256=_sha(RAW),
            source_published_at=None,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-test.v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="sec-doc",
            document_key="sec-doc",
            version_sequence=1,
            observation_id="sec-source",
            blob_sha256=_sha(RAW),
            issuer_id="issuer-1",
            ticker="ONON",
            document_type="filing",
            form_type="6-K",
            accession_number="0001858985-26-000018",
            period_end=END,
            language="en",
            recorded_at=STAMP,
        )
    )
    registry = IssuerRegistry(conn)
    assertion = IdentifierAssertion(
        assertion_id="on-cik",
        idempotency_key="on-cik",
        issuer_id="issuer-1",
        identifier_type="sec_cik",
        identifier_value="1858985",
        normalized_value="0001858985",
        authority="sec_registry",
        source_observation_id="sec-source",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="on-cik-resolution",
            idempotency_key="on-cik-resolution",
            resolution_key=assertion.resolution_key,
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="synthetic-test",
            policy_version="1",
            policy_config_sha256="a" * 64,
            reason_code="test",
            reason_details=(("fixture", "synthetic"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    backfill_fulltext_evidence(
        conn,
        FullTextBackfillRequest(
            repo_root=root, source_lane="evidence_native", document_version_id="sec-doc", apply=True
        ),
    )
    run = str(
        conn.execute(
            "SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id='sec-doc'"
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT node_id,text,locator_sha256 FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='table_cell'",
        (run,),
    ).fetchall()
    nodes = {str(row["text"]): row for row in rows}
    facts: list[ReviewedSecFinancialFact] = []
    for key, label, number, start in (
        ("revenue_ytd", "Net revenue", "10.0", datetime(2026, 1, 1, tzinfo=UTC)),
        ("cash", "Cash", "2.5", None),
    ):
        facts.append(
            ReviewedSecFinancialFact(
                fact_key=key,
                concept_name=label,
                document_version_id="sec-doc",
                fulltext_run_id=run,
                source_kind="sec_6k",
                source_doc_sha256=_sha(RAW),
                accession_number="0001858985-26-000018",
                sec_cik="0001858985",
                value_node_id=str(nodes[number]["node_id"]),
                value_locator_sha256=str(nodes[number]["locator_sha256"]),
                raw_lexical_value=number,
                unit_key="CHF_millions",
                currency="CHF",
                period_start=start,
                period_end=END,
                fiscal_period="H1" if start else "Q2",
                accounting_basis="ifrs",
                definition_text=label,
                definition_node_ids=(str(nodes[label]["node_id"]),),
                context_node_ids=(
                    str(
                        nodes["Six months ended June 30, 2026" if start else "June 30, 2026"][
                            "node_id"
                        ]
                    ),
                    str(nodes["CHF in millions"]["node_id"]),
                ),
            )
        )
    draft = ReviewedSecFinancialRequest.model_construct(
        ticker="ONON",
        reviewed_by="owner",
        reviewed_at=STAMP + timedelta(seconds=1),
        review_evidence="Synthetic test source review",
        expected_fact_keys=("revenue_ytd", "cash"),
        facts=tuple(facts),
        rejections=(),
        content_roots=(root,),
        recorded_at=STAMP + timedelta(seconds=1),
        review_sha256="0" * 64,
        apply=False,
    )
    return ReviewedSecFinancialRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "review_sha256": _sha(draft.canonical_review_json.encode()),
        }
    )


def test_native_review_dry_apply_and_replay(database: sqlite3.Connection, tmp_path: Path) -> None:
    request = synthetic_sec_request(database, tmp_path)
    before = database.total_changes
    dry = publish_reviewed_sec_financial_tables(database, request)
    assert database.total_changes == before
    assert dry.source_population_complete and dry.captured_count == 2
    applied = publish_reviewed_sec_financial_tables(
        database,
        request.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256}),
    )
    assert applied.publication_id is not None
    replay = publish_reviewed_sec_financial_tables(
        database,
        request.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256}),
    )
    assert replay.exact_replay
    cells = database.execute(
        "SELECT period_kind,period_start,unit_key,currency FROM fact_cells_v2 WHERE concept_namespace LIKE 'reviewed-sec:%' ORDER BY concept_name"
    ).fetchall()
    assert [tuple(row) for row in cells] == [
        ("instant", None, "CHF", "CHF"),
        ("duration", "2026-01-01 00:00:00+00:00", "CHF", "CHF"),
    ]
    assert (
        database.execute(
            "SELECT numeric_value FROM fact_observations_v2 WHERE method_name='reviewed-sec-financial-tables' ORDER BY numeric_value"
        ).fetchall()[0][0]
        == "10000000"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"raw_lexical_value": "11.0"},
        {"value_locator_sha256": "f" * 64},
        {"source_kind": "sec_20f"},
        {"accession_number": "0001858985-26-000019"},
    ],
)
def test_source_review_cannot_change_exact_evidence(
    database: sqlite3.Connection, tmp_path: Path, change: dict[str, str]
) -> None:
    request = synthetic_sec_request(database, tmp_path)
    fact = request.facts[0].model_copy(update=change)
    draft = request.model_copy(update={"facts": (fact, request.facts[1])})
    updated = draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())})
    with pytest.raises(ValueError):
        publish_reviewed_sec_financial_tables(database, updated)
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_population_gap_and_apply_without_dry_receipt_fail_closed(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = synthetic_sec_request(database, tmp_path)
    with pytest.raises(ValueError, match="population"):
        ReviewedSecFinancialRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "expected_fact_keys": ["cash", "revenue_ytd", "missing"],
            }
        )
    with pytest.raises(ValueError, match="plan"):
        publish_reviewed_sec_financial_tables(database, request.model_copy(update={"apply": True}))


def test_signed_fragments_and_narrative_spans(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.test_reviewed_sec_financial_tables as fixture
    from provenance.evidence_ledger import EvidenceLocator
    from provenance.reviewed_sec_financial_tables import ReviewedSecNumericFragment

    raw = (
        RAW.replace(b"<td>10.0</td>", b"<td><span>(</span><span>10.0</span><span>)</span></td>")
        + b"<p>As of June 30, 2026, restricted cash was CHF 0.9 million.</p>"
    )
    # Build the reviewed fixture with a positive scalar only to select source
    # identities, then re-seal the explicit negative source fragments below.
    monkeypatch.setattr(fixture, "RAW", raw)
    request = synthetic_sec_request(database, tmp_path)
    fact = request.facts[0]
    rows = database.execute(
        "SELECT node_id,text,locator_json FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='table_cell'",
        (fact.fulltext_run_id,),
    ).fetchall()
    value = next(row for row in rows if row["node_id"] == fact.value_node_id)
    anchor = EvidenceLocator.model_validate_json(str(value["locator_json"]))
    cell = sorted(
        (
            row
            for row in rows
            if (
                lambda loc: (
                    (loc.table_name, loc.table_row_index, loc.table_column_index)
                    == (anchor.table_name, anchor.table_row_index, anchor.table_column_index)
                )
            )(EvidenceLocator.model_validate_json(str(row["locator_json"])))
        ),
        key=lambda row: (
            EvidenceLocator.model_validate_json(str(row["locator_json"])).char_start or 0
        ),
    )
    fragment_values: list[ReviewedSecNumericFragment] = []
    for row in cell:
        fragment_locator = EvidenceLocator.model_validate_json(str(row["locator_json"]))
        assert fragment_locator.char_start is not None and fragment_locator.char_end is not None
        fragment_values.append(
            ReviewedSecNumericFragment(
                node_id=str(row["node_id"]),
                char_start=fragment_locator.char_start,
                char_end=fragment_locator.char_end,
                raw_text=str(row["text"]),
            )
        )
    fragments = tuple(fragment_values)
    signed = fact.model_copy(
        update={
            "raw_lexical_value": "(10.0)",
            "value_capture_kind": "table_cell_fragments",
            "value_fragments": fragments,
        }
    )
    draft = request.model_copy(update={"facts": (signed, request.facts[1])})
    request = draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())})
    assert publish_reviewed_sec_financial_tables(database, request).captured_count == 2
    # Omitting the source parentheses cannot turn the negative into a positive.
    forged = signed.model_copy(
        update={
            "raw_lexical_value": "10.0",
            "value_capture_kind": "table_cell",
            "value_fragments": (),
        }
    )
    draft = request.model_copy(update={"facts": (forged, request.facts[1])})
    with pytest.raises(ValueError, match="complete exact numeric fragment"):
        publish_reviewed_sec_financial_tables(
            database,
            draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())}),
        )
    passage = database.execute(
        "SELECT node_id,text,locator_json,locator_sha256 FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='passage' AND text LIKE '%restricted cash%'",
        (fact.fulltext_run_id,),
    ).fetchone()
    assert passage is not None
    loc = EvidenceLocator.model_validate_json(str(passage["locator_json"]))
    assert loc.char_start is not None
    offset = str(passage["text"]).index("0.9")
    narrative = request.facts[1].model_copy(
        update={
            "fact_key": "restricted_cash",
            "concept_name": "Restricted cash",
            "value_node_id": str(passage["node_id"]),
            "value_locator_sha256": str(passage["locator_sha256"]),
            "raw_lexical_value": "0.9",
            "value_capture_kind": "passage_numeric_span",
            "value_fragments": (
                ReviewedSecNumericFragment(
                    node_id=str(passage["node_id"]),
                    char_start=loc.char_start + offset,
                    char_end=loc.char_start + offset + 3,
                    raw_text="0.9",
                ),
            ),
            "definition_text": str(passage["text"]),
            "definition_node_ids": (str(passage["node_id"]),),
            "context_node_ids": (str(passage["node_id"]),),
        }
    )
    draft = request.model_copy(
        update={"facts": (narrative,), "expected_fact_keys": ("restricted_cash",)}
    )
    assert (
        publish_reviewed_sec_financial_tables(
            database,
            draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())}),
        ).captured_count
        == 1
    )


def test_rejected_population_members_remain_in_immutable_review(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    from provenance.reviewed_sec_financial_tables import ReviewedSecFinancialRejection

    request = synthetic_sec_request(database, tmp_path)
    draft = request.model_copy(
        update={
            "expected_fact_keys": (*request.expected_fact_keys, "unavailable_debt"),
            "rejections": (
                ReviewedSecFinancialRejection(
                    fact_key="unavailable_debt", reason_code="aggregate_not_reported"
                ),
            ),
        }
    )
    request = draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())})
    dry = publish_reviewed_sec_financial_tables(database, request)
    assert not dry.source_population_complete and dry.rejected_count == 1
    publish_reviewed_sec_financial_tables(
        database,
        request.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256}),
    )
    review = database.execute(
        "SELECT text FROM evidence_nodes WHERE node_id LIKE 'reviewed-sec-review:%'"
    ).fetchone()
    assert review is not None and "aggregate_not_reported" in str(review[0])


@pytest.mark.parametrize(
    "change",
    [
        {"period_end": datetime(2026, 9, 30, tzinfo=UTC)},
        {"currency": "USD", "unit_key": "USD_millions"},
        {"period_start": datetime(2026, 4, 1, tzinfo=UTC)},
    ],
)
def test_review_context_cannot_invent_period_or_unit(
    database: sqlite3.Connection, tmp_path: Path, change: dict[str, object]
) -> None:
    request = synthetic_sec_request(database, tmp_path)
    draft = request.model_copy(
        update={"facts": (request.facts[0].model_copy(update=change), request.facts[1])}
    )
    request = draft.model_copy(update={"review_sha256": _sha(draft.canonical_review_json.encode())})
    with pytest.raises(ValueError):
        publish_reviewed_sec_financial_tables(database, request)
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_unbound_selector_binds_exact_local_keys_and_rejects_changed_text(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    from provenance.fact_plane_v2 import CanonicalJSONObject
    from provenance.reviewed_sec_financial_tables import (
        ReviewedSecFinancialSelector,
        ReviewedSecNodeCommitment,
        bind_reviewed_sec_financial_selector,
        load_reviewed_sec_html_evidence,
    )

    request = synthetic_sec_request(database, tmp_path)
    fact = request.facts[0]
    evidence = load_reviewed_sec_html_evidence(
        database,
        document_version_id=fact.document_version_id,
        fulltext_run_id=fact.fulltext_run_id,
        source_kind=fact.source_kind,
        accession_number=fact.accession_number,
        sec_cik=fact.sec_cik,
        source_doc_sha256=fact.source_doc_sha256,
        content_roots=request.content_roots,
        knowledge_cutoff=request.reviewed_at,
    )
    touched = evidence.require_nodes(
        (fact.value_node_id, *fact.definition_node_ids, *fact.context_node_ids)
    )
    locals_by_id = {
        node.node_id: f"html:{node.locator.filing_ordinal}:{node.kind}" for node in touched
    }
    coordinates = fact.model_dump(
        mode="json",
        exclude={
            "document_version_id",
            "fulltext_run_id",
            "source_kind",
            "source_doc_sha256",
            "accession_number",
            "sec_cik",
        },
    )
    coordinates["value_node_id"] = locals_by_id[fact.value_node_id]
    coordinates["definition_node_ids"] = [
        locals_by_id[identity] for identity in fact.definition_node_ids
    ]
    coordinates["context_node_ids"] = [locals_by_id[identity] for identity in fact.context_node_ids]
    commitments = tuple(
        ReviewedSecNodeCommitment(
            local_key=locals_by_id[node.node_id],
            locator_sha256=node.locator.canonical_sha256,
            text_sha256=_sha(node.text.encode()),
        )
        for node in touched
    )
    selector = ReviewedSecFinancialSelector(
        source_url=evidence.source_url,
        source_doc_sha256=fact.source_doc_sha256,
        source_kind=fact.source_kind,
        accession_number=fact.accession_number,
        sec_cik=fact.sec_cik,
        coordinates=CanonicalJSONObject(root=coordinates),
        node_commitments=commitments,
    )
    assert bind_reviewed_sec_financial_selector(evidence, selector) == fact
    tainted = selector.model_copy(
        update={
            "node_commitments": (
                commitments[0].model_copy(update={"text_sha256": "f" * 64}),
                *commitments[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="commitment"):
        bind_reviewed_sec_financial_selector(evidence, tainted)
