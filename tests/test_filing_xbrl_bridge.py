from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from io import StringIO
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from execution import filing_xbrl_bridge as bridge

_matplotlib_configuration: Callable[[], AbstractContextManager[Path]] = getattr(
    bridge, "_matplotlib_configuration"
)


@pytest.mark.parametrize("fail", [False, True])
def test_matplotlib_configuration_is_private_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MPLCONFIGDIR", "parent-configuration")
    allocated: list[Path] = []

    def use_configuration() -> None:
        with _matplotlib_configuration() as directory:
            allocated.append(directory)
            assert directory.parent == tmp_path
            assert bridge.os.environ["MPLCONFIGDIR"] == str(directory)
            (directory / "cache.json").write_text("{}")
            if fail:
                raise RuntimeError("body failed")

    if fail:
        with pytest.raises(RuntimeError, match="body failed"):
            use_configuration()
    else:
        use_configuration()
    assert len(allocated) == 1
    assert not allocated[0].exists()
    assert bridge.os.environ["MPLCONFIGDIR"] == "parent-configuration"


def test_matplotlib_configuration_bounds_abandoned_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    for number in range(4):
        directory = tmp_path / f"earnings-summary-xbrl-mpl-{number}"
        directory.mkdir()
        (directory / "preserved.txt").write_text("prior incomplete request")
    with pytest.raises(ValueError, match="requires recovery"), _matplotlib_configuration():
        pytest.fail("configuration must refuse allocation")
    assert len(tuple(tmp_path.iterdir())) == 4
    assert all((directory / "preserved.txt").is_file() for directory in tmp_path.iterdir())


def test_matplotlib_configuration_preserves_a_replaced_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "preserved.txt").write_text("replacement data")
    allocated: list[Path] = []
    with (
        pytest.raises(ValueError, match="identity changed"),
        _matplotlib_configuration() as directory,
    ):
        allocated.append(directory)
        directory.rename(tmp_path / "displaced")
        replacement.rename(directory)
    assert (allocated[0] / "preserved.txt").read_text() == "replacement data"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_bridge_emits_closed_rejected_fact_commitments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><ix:nonFraction/></html>"
    document = tmp_path / "filing.htm"
    document.write_bytes(body)
    request = _request(document, body)

    def extracted(*_args: object, **_kwargs: object) -> bridge.ArelleExtraction:
        return bridge.ArelleExtraction(
            facts=(
                bridge.ExtractedFact(
                    member_ordinal=0,
                    fact_id="fact-1",
                    element_path="/html/body/ix:nonFraction[1]",
                    concept_namespace="https://example.test/taxonomy",
                    concept_name="Revenue",
                    context_id="ctx-1",
                    observed_cik="0000000001",
                    evidence_text="Revenue 10",
                    canonical_raw_fact={"concept": "Revenue", "value": "10"},
                    footnotes=(),
                ),
            ),
            loaded_member_ordinals=(0,),
        )

    monkeypatch.setattr(
        bridge,
        "_extract_arelle_facts",
        extracted,
    )

    result = bridge.process_request(request, runtime_artifact_sha256="b" * 64)

    assert result["coordinates"] == {
        "arelle": "2.39.8",
        "edgar": "26.1",
        "xule": "30052",
    }
    execution = cast(dict[str, object], result["execution_evidence"])
    assert execution["network_requests_observed"] == 0
    assert execution["runtime_artifact_sha256"] == "b" * 64
    facts = cast(list[dict[str, object]], result["facts"])
    fact = facts[0]
    assert fact["normalization_outcome"] == "rejected"
    assert fact["rejection_reason_code"] == "normalization_not_qualified"
    locator = cast(dict[str, object], fact["source_locator"])
    assert locator["filing_ordinal"] == 0
    assert fact["package_member_blob_sha256"] == _sha(body)
    assert fact["raw_fact_sha256"] == _sha(
        json.dumps(
            {"concept": "Revenue", "value": "10"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def test_bridge_emits_closed_normalized_fact_commitments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><ix:nonFraction/></html>"
    document = tmp_path / "filing.htm"
    document.write_bytes(body)
    request = _request(document, body)
    normalized = cast(
        dict[str, bridge.JsonValue],
        {
            "accounting_basis": "us_gaap",
            "concept_name": "Revenue",
            "concept_namespace": "https://example.test/taxonomy/2026",
            "consolidation_scope": "consolidated",
            "currency": "USD",
            "decimals": "-6",
            "dimensions": [],
            "effective_at": "2026-03-31T00:00:00+00:00",
            "fiscal_period": "Q1",
            "fiscal_year": 2026,
            "is_nil": False,
            "numeric_value": "10",
            "period_end": "2026-03-31T00:00:00+00:00",
            "period_kind": "duration",
            "period_start": "2026-01-01T00:00:00+00:00",
            "precision": None,
            "raw_lexical_value": "10",
            "revision_kind": "initial",
            "source_context_id": "ctx-1",
            "source_taxonomy_version": "2026",
            "source_unit_id": "usd",
            "supersedes_observation_id": None,
            "taxonomy_name": "Issuer Extension",
            "text_value": None,
            "unit_key": "usd",
            "value_kind": "numeric",
        },
    )

    def extracted(*_args: object, **_kwargs: object) -> bridge.ArelleExtraction:
        return bridge.ArelleExtraction(
            facts=(
                bridge.ExtractedFact(
                    member_ordinal=0,
                    fact_id="fact-1",
                    element_path="/html/body/ix:nonFraction[1]",
                    concept_namespace="https://example.test/taxonomy/2026",
                    concept_name="Revenue",
                    context_id="ctx-1",
                    observed_cik="0000000001",
                    evidence_text="Revenue 10",
                    canonical_raw_fact={"concept": "Revenue", "value": "10"},
                    footnotes=(),
                    normalized_fact=normalized,
                    rejection_reason_code=None,
                    rejection_detail=None,
                ),
            ),
            loaded_member_ordinals=(0,),
            source_inline_fact_count=1,
        )

    monkeypatch.setattr(bridge, "_extract_arelle_facts", extracted)

    result = bridge.process_request(request, runtime_artifact_sha256="f" * 64)

    fact = cast(list[dict[str, object]], result["facts"])[0]
    assert fact["normalization_outcome"] == "normalized"
    assert fact["normalized_fact"] == normalized
    assert fact["rejection_reason_code"] is None
    assert fact["rejection_detail"] is None


def test_bridge_refuses_incomplete_inline_fact_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'>"
        b"<ix:nonFraction/><ix:nonNumeric/></html>"
    )
    document = tmp_path / "filing.htm"
    document.write_bytes(body)
    request = _request(document, body)

    def extracted(*_args: object, **_kwargs: object) -> bridge.ArelleExtraction:
        return bridge.ArelleExtraction(
            facts=(),
            loaded_member_ordinals=(0,),
            source_inline_fact_count=2,
        )

    monkeypatch.setattr(bridge, "_extract_arelle_facts", extracted)

    with pytest.raises(ValueError, match="fact census"):
        bridge.process_request(request, runtime_artifact_sha256="a" * 64)


def test_bridge_counts_inline_facts_independently(tmp_path: Path) -> None:
    document = tmp_path / "filing.htm"
    document.write_bytes(
        b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'>"
        b"<ix:nonFraction/><ix:nonNumeric/><ix:continuation/></html>"
    )

    counter = cast(Callable[[Path], int], getattr(bridge, "_inline_fact_count"))

    assert counter(document) == 2


def test_bridge_refuses_arelle_missing_reference_errors() -> None:
    logs = json.dumps(
        {
            "log": [
                {
                    "code": "ix11.12.1.2:missingReferences",
                    "level": "error",
                    "message": {"text": "Instance fact missing schema definition"},
                }
            ]
        }
    )
    verifier = cast(Callable[[str], None], getattr(bridge, "_require_clean_arelle_logs"))

    with pytest.raises(ValueError, match="missingReferences"):
        verifier(logs)


def test_bridge_normalizes_valid_numeric_fact_deterministically() -> None:
    concept_namespace = "https://xbrl.fasb.org/us-gaap/2024"
    concept_name = "RevenueFromContractWithCustomerExcludingAssessedTax"
    currency_qname = type(
        "QName",
        (),
        {"namespaceURI": "http://www.xbrl.org/2003/iso4217", "localName": "USD"},
    )()
    context = type(
        "Context",
        (),
        {
            "isInstantPeriod": False,
            "isStartEndPeriod": True,
            "startDatetime": datetime.fromisoformat("2024-02-01T00:00:00"),
            "endDatetime": datetime.fromisoformat("2024-05-01T00:00:00"),
            "qnameDims": {},
        },
    )()
    unit = type("Unit", (), {"measures": ([currency_qname], [])})()
    fact = type(
        "Fact",
        (),
        {
            "decimals": "-6",
            "isNil": False,
            "isNumeric": True,
            "precision": None,
            "textValue": "784000000",
            "unit": unit,
            "xValid": 4,
            "xValue": 784000000,
        },
    )()
    normalizer = cast(
        Callable[..., tuple[dict[str, object] | None, str | None, str | None]],
        getattr(bridge, "_normalize_fact"),
    )

    normalized, rejection_code, rejection_detail = normalizer(
        fact,
        concept_namespace=concept_namespace,
        concept_name=concept_name,
        context=context,
        context_id="D2024Q1",
        unit_id="u17",
        fiscal_year=2025,
        fiscal_period="Q1",
        filing_period_end=datetime.fromisoformat("2024-04-30T00:00:00"),
        accounting_basis="us_gaap",
    )

    assert rejection_code is None
    assert rejection_detail is None
    assert normalized is not None
    assert normalized["taxonomy_name"] == "US GAAP"
    assert normalized["source_taxonomy_version"] == "2024"
    assert normalized["period_start"] == "2024-02-01T00:00:00"
    assert normalized["period_end"] == "2024-04-30T00:00:00"
    assert normalized["fiscal_year"] == 2025
    assert normalized["fiscal_period"] == "Q1"
    assert normalized["currency"] == "USD"
    assert normalized["unit_key"] == "USD"
    assert normalized["source_unit_id"] == "u17"
    assert normalized["consolidation_scope"] == "other"
    assert normalized["numeric_value"] == "784000000"


def test_bridge_refuses_unqualified_multi_document_ixds(tmp_path: Path) -> None:
    first = tmp_path / "first.htm"
    second = tmp_path / "second.htm"
    body = b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><ix:nonNumeric/></html>"
    first.write_bytes(body)
    second.write_bytes(body)
    members: list[dict[str, object]] = [
        {"local_path": str(first), "member_role": "primary_document"},
        {"local_path": str(second), "member_role": "filing_attachment"},
    ]
    counter = cast(
        Callable[[list[dict[str, object]], int], int], getattr(bridge, "_inline_package_fact_count")
    )

    with pytest.raises(ValueError, match="IXDS"):
        counter(members, 0)


def test_bridge_pins_standard_streams_to_strict_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Stream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            calls.append((encoding, errors))

    monkeypatch.setattr(bridge.sys, "stdin", Stream())
    monkeypatch.setattr(bridge.sys, "stdout", Stream())
    configure = cast(Callable[[], None], getattr(bridge, "_configure_standard_streams"))

    configure()

    assert calls == [("utf-8", "strict"), ("utf-8", "strict")]


def test_bridge_rejects_changed_member_bytes(tmp_path: Path) -> None:
    body = b"original"
    document = tmp_path / "filing.htm"
    document.write_bytes(b"changed!")

    with pytest.raises(ValueError, match="member digest"):
        bridge.process_request(_request(document, body), runtime_artifact_sha256="c" * 64)


def test_bridge_rejects_malformed_accession(tmp_path: Path) -> None:
    body = b"original"
    document = tmp_path / "filing.htm"
    document.write_bytes(body)
    request = _request(document, body)
    request["accession_number"] = "0000000001xx26xx000001"

    with pytest.raises(ValueError, match="accession"):
        bridge.process_request(request, runtime_artifact_sha256="d" * 64)


def test_loaded_document_closure_rejects_undeclared_document(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_bytes(b"filing")
    undeclared = tmp_path / "other.xsd"
    undeclared.write_bytes(b"schema")
    primary = type("Document", (), {"filepath": str(filing), "uri": str(filing)})()
    other = type("Document", (), {"filepath": str(undeclared), "uri": str(undeclared)})()
    model = type(
        "Model",
        (),
        {"modelDocument": primary, "urlDocs": {"primary": primary, "other": other}},
    )()

    closure = cast(
        Callable[[object, list[dict[str, object]]], tuple[int, ...]],
        getattr(bridge, "_loaded_document_ordinals"),
    )
    members = cast(list[dict[str, object]], _request(filing, b"filing")["members"])
    with pytest.raises(ValueError, match="undeclared document"):
        closure(model, members)


def test_loaded_document_closure_requires_declared_taxonomy(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_bytes(b"filing")
    taxonomy = tmp_path / "taxonomy.xsd"
    taxonomy.write_bytes(b"schema")
    request = _request(filing, b"filing")
    members = cast(list[dict[str, object]], request["members"])
    members.append(
        {
            "blob_sha256": _sha(b"schema"),
            "byte_size": 6,
            "document_version_id": "document-2",
            "local_path": str(taxonomy),
            "media_type": "application/xml",
            "member_ordinal": 1,
            "member_role": "issuer_taxonomy",
            "source_url": "https://www.sec.gov/Archives/taxonomy.xsd",
        }
    )
    primary = type("Document", (), {"filepath": str(filing), "uri": str(filing)})()
    model = type("Model", (), {"modelDocument": primary, "urlDocs": {"primary": primary}})()

    closure = cast(
        Callable[[object, list[dict[str, object]]], tuple[int, ...]],
        getattr(bridge, "_loaded_document_ordinals"),
    )
    assert closure(model, members) == (0,)
    extraction = bridge.ArelleExtraction(facts=(), loaded_member_ordinals=(0,))

    def extract(
        _request: dict[str, object],
        _members: list[dict[str, object]],
    ) -> bridge.ArelleExtraction:
        return extraction

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(bridge, "_extract_arelle_facts", extract)
        request["package_member_set_sha256"] = _sha(
            json.dumps(
                [
                    {key: value for key, value in member.items() if key != "local_path"}
                    for member in members
                ],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        with pytest.raises(ValueError, match="did not load every declared"):
            bridge.process_request(request, runtime_artifact_sha256="e" * 64)


def test_fact_member_identity_has_no_primary_fallback(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_bytes(b"filing")
    unknown = tmp_path / "unknown.htm"
    unknown.write_bytes(b"unknown")
    document = type("Document", (), {"filepath": str(unknown), "uri": str(unknown)})()
    fact = type("Fact", (), {"modelDocument": document})()

    member_ordinal = cast(
        Callable[[object, list[dict[str, object]]], int],
        getattr(bridge, "_member_ordinal"),
    )
    members = cast(list[dict[str, object]], _request(filing, b"filing")["members"])
    with pytest.raises(ValueError, match="cannot be bound"):
        member_ordinal(fact, members)


def _request(document: Path, body: bytes) -> dict[str, object]:
    member = {
        "blob_sha256": _sha(body),
        "byte_size": len(body),
        "document_version_id": "document-1",
        "local_path": str(document),
        "media_type": "text/html",
        "member_ordinal": 0,
        "member_role": "primary_document",
        "source_url": "https://www.sec.gov/Archives/filing.htm",
    }
    member_commitment = {key: value for key, value in member.items() if key != "local_path"}
    package_sha = _sha(
        json.dumps(
            [member_commitment],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return {
        "accession_number": "0000000001-26-000001",
        "entrypoint_ordinal": 0,
        "expected_cik": "0000000001",
        "members": [member],
        "package_member_set_sha256": package_sha,
    }


def test_tied_fact_input_order_preserves_result_and_locator_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Tree:
        def getpath(self, fact: object) -> str:
            assert isinstance(fact, Fact)
            return fact.path

    class Fact:
        def __init__(self, fact_id: str, path: str) -> None:
            self.id = fact_id
            self.path = path
            self.modelDocument = SimpleNamespace(uri="filing.htm")
            self.sourceline = 8
            self.qname = SimpleNamespace(
                namespaceURI="https://example.test/taxonomy", localName="Revenue"
            )
            self.contextID = "same-context"
            self.value = "10"

        def getroottree(self) -> Tree:
            return Tree()

    def extract_fact(
        _model: object,
        fact: object,
        _members: list[dict[str, object]],
        _ordinal: int,
        **_focus: object,
    ) -> bridge.ExtractedFact:
        assert isinstance(fact, Fact)
        return bridge.ExtractedFact(
            member_ordinal=0,
            fact_id=fact.id,
            element_path=fact.path,
            concept_namespace="https://example.test/taxonomy",
            concept_name="Revenue",
            context_id=fact.contextID,
            observed_cik="0000000001",
            evidence_text="Revenue 10",
            canonical_raw_fact={"fact_id": fact.id, "value": fact.value},
            footnotes=(),
        )

    extract_model = cast(
        Callable[[object, list[dict[str, object]]], tuple[bridge.ExtractedFact, ...]],
        getattr(bridge, "_facts_from_model"),
    )
    facts = (
        Fact("z-source-id", "/html/body/fact[1]"),
        Fact("a-source-id", "/html/body/fact[2]"),
        Fact("m-source-id", "/html/body/fact[3]"),
    )
    document = tmp_path / "filing.htm"
    body = b"<html>three distinct captured source elements</html>"
    document.write_bytes(body)
    request = _request(document, body)
    monkeypatch.setattr(bridge, "_extracted_fact", extract_fact)
    model = SimpleNamespace(factsInInstance=facts)

    def extract(
        _request: dict[str, object], members: list[dict[str, object]]
    ) -> bridge.ArelleExtraction:
        return bridge.ArelleExtraction(
            facts=extract_model(model, members),
            loaded_member_ordinals=(0,),
            source_inline_fact_count=3,
        )

    monkeypatch.setattr(bridge, "_extract_arelle_facts", extract)
    results: list[dict[str, object]] = []
    for ordering in permutations(facts):
        model.factsInInstance = ordering
        results.append(bridge.process_request(request, runtime_artifact_sha256="b" * 64))
    # Complete output equality includes ordinal-derived locators, source entries and set digests.
    assert all(result == results[0] for result in results)
    captured = cast(list[dict[str, object]], results[0]["facts"])
    assert [
        cast(dict[str, object], fact["source_locator"])["xbrl_fact_id"] for fact in captured
    ] == ["z-source-id", "a-source-id", "m-source-id"]
    assert [fact["input_ordinal"] for fact in captured] == [0, 1, 2]


@pytest.mark.parametrize("has_locator", [False, True])
def test_ambiguous_fact_order_refuses_before_assigning_ordinals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_locator: bool,
) -> None:
    facts = [
        SimpleNamespace(
            modelDocument=SimpleNamespace(uri="filing.htm"),
            sourceline=8,
            qname=SimpleNamespace(namespaceURI="example", localName="Revenue"),
            contextID="context",
            value="10",
            decimals=decimals,
            id="duplicate-id" if has_locator else None,
        )
        for decimals in ("0", "-3")
    ]
    if has_locator:

        def element_path(_fact: object) -> str:
            return "/xbrl/fact[1]"

        for fact in facts:
            fact.getroottree = lambda: SimpleNamespace(getpath=element_path)
    extract_model = cast(
        Callable[[object, list[dict[str, object]]], tuple[bridge.ExtractedFact, ...]],
        getattr(bridge, "_facts_from_model"),
    )

    def extract(
        _request: dict[str, object], members: list[dict[str, object]]
    ) -> bridge.ArelleExtraction:
        return bridge.ArelleExtraction(
            facts=extract_model(SimpleNamespace(factsInInstance=facts), members),
            loaded_member_ordinals=(0,),
        )

    def unexpected_extraction(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ambiguous source identities must fail before ordinal assignment")

    body = b"<html>ambiguous source facts</html>"
    document = tmp_path / "filing.htm"
    document.write_bytes(body)
    monkeypatch.setattr(bridge, "_extract_arelle_facts", extract)
    monkeypatch.setattr(bridge, "_extracted_fact", unexpected_extraction)
    monkeypatch.setattr(bridge, "_configure_standard_streams", lambda: None)
    monkeypatch.setattr(bridge.sys, "stdin", StringIO(json.dumps(_request(document, body))))
    assert (
        bridge.main(["--protocol", "filing-xbrl-bridge.v2", "--runtime-artifact-sha256", "b" * 64])
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_type": "ValueError",
        "event": "filing_xbrl_bridge_refused",
    }
