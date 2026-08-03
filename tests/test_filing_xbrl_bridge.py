from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from execution import filing_xbrl_bridge as bridge


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
