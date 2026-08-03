"""Closed offline Arelle bridge for one captured SEC Inline-XBRL package.

This module is copied into the separately hash-locked processor runtime.  The
application does not import Arelle; only this isolated subprocess does.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import socket
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias, cast
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

_PROTOCOL = "filing-xbrl-bridge.v1"
_SANDBOX_CONTRACT = "earnings-xbrl-os-sandbox.v1"
_COORDINATES = {"arelle": "2.39.8", "edgar": "26.1", "xule": "30052"}
_SHA256_LENGTH = 64
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class _Session(Protocol):
    def __enter__(self) -> _Session: ...

    def __exit__(self, *args: object) -> None: ...

    def run(self, options: object) -> bool: ...

    def get_models(self) -> list[object]: ...


class _SessionFactory(Protocol):
    def __call__(self) -> _Session: ...


class _OptionsFactory(Protocol):
    def __call__(self, **kwargs: object) -> object: ...


class _ElementTree(Protocol):
    def getpath(self, value: object) -> str: ...


class _Fact(Protocol):
    def getroottree(self) -> _ElementTree: ...


@dataclass(frozen=True)
class ExtractedFact:
    member_ordinal: int
    fact_id: str
    element_path: str
    concept_namespace: str
    concept_name: str
    context_id: str
    observed_cik: str
    evidence_text: str
    canonical_raw_fact: dict[str, JsonValue]
    footnotes: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True)
class ArelleExtraction:
    facts: tuple[ExtractedFact, ...]
    loaded_member_ordinals: tuple[int, ...]


def process_request(
    payload: dict[str, object],
    *,
    runtime_artifact_sha256: str,
) -> dict[str, object]:
    """Validate one request and return its closed canonical processor output."""

    _require_sha(runtime_artifact_sha256, "runtime artifact")
    request = _validated_request(payload)
    members = cast(list[dict[str, object]], request["members"])
    _verify_members(members)
    extraction = _extract_arelle_facts(request, members)
    facts = extraction.facts
    expected_cik = cast(str, request["expected_cik"])
    output_facts = tuple(
        _fact_payload(
            fact,
            input_ordinal=ordinal,
            members=members,
            accession_number=cast(str, request["accession_number"]),
            expected_cik=expected_cik,
        )
        for ordinal, fact in enumerate(facts)
    )
    loaded_ordinals = set(extraction.loaded_member_ordinals)
    required_network_ordinals = {
        ordinal
        for ordinal, member in enumerate(members)
        if member["member_role"] in {"issuer_taxonomy", "standard_taxonomy", "network_artifact"}
    }
    if not required_network_ordinals.issubset(loaded_ordinals):
        raise ValueError("Arelle did not load every declared taxonomy/network artifact")
    network_artifacts = [
        {
            "blob_sha256": member["blob_sha256"],
            "source_url": member["source_url"],
        }
        for ordinal, member in enumerate(members)
        if ordinal in loaded_ordinals
        and member["member_role"] in {"issuer_taxonomy", "standard_taxonomy", "network_artifact"}
    ]
    footnotes = [
        {
            "canonical_footnote": footnote["canonical_footnote"],
            "footnote_ordinal": footnote["footnote_ordinal"],
            "footnote_sha256": footnote["footnote_sha256"],
            "input_ordinal": fact["input_ordinal"],
        }
        for fact in output_facts
        for footnote in cast(list[dict[str, object]], fact["footnotes"])
    ]
    raw_set = [
        {
            "input_ordinal": fact["input_ordinal"],
            "raw_fact_sha256": fact["raw_fact_sha256"],
            "source_entry_sha256": fact["source_entry_sha256"],
            "source_locator_sha256": fact["source_locator_sha256"],
        }
        for fact in output_facts
    ]
    package_sha = cast(str, request["package_member_set_sha256"])
    return {
        "bridge_protocol_version": _PROTOCOL,
        "coordinates": _COORDINATES,
        "execution_evidence": {
            "accession_number": request["accession_number"],
            "expected_cik": expected_cik,
            "internet_connectivity": "os_denied",
            "network_requests_observed": 0,
            "package_member_set_sha256": package_sha,
            "runtime_artifact_sha256": runtime_artifact_sha256,
            "sandbox_contract_version": _SANDBOX_CONTRACT,
        },
        "facts": list(output_facts),
        "footnote_count": len(footnotes),
        "footnote_set_sha256": _digest(footnotes),
        "network_artifact_count": len(network_artifacts),
        "network_artifact_set_sha256": _digest(network_artifacts),
        "network_artifacts": network_artifacts,
        "package_member_set_sha256": package_sha,
        "raw_fact_set_sha256": _digest(raw_set),
        "runtime_artifact_sha256": runtime_artifact_sha256,
        "zero_fact_disposition": None if output_facts else "verified_no_inline_xbrl",
    }


def _validated_request(payload: dict[str, object]) -> dict[str, object]:
    required = {
        "accession_number",
        "entrypoint_ordinal",
        "expected_cik",
        "members",
        "package_member_set_sha256",
    }
    if set(payload) != required:
        raise ValueError("bridge request fields are not closed")
    accession = payload["accession_number"]
    cik = payload["expected_cik"]
    entrypoint = payload["entrypoint_ordinal"]
    members = payload["members"]
    if not isinstance(accession, str) or re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) is None:
        raise ValueError("bridge accession is malformed")
    if not isinstance(cik, str) or len(cik) != 10 or not cik.isdigit():
        raise ValueError("bridge CIK is malformed")
    if not isinstance(entrypoint, int) or isinstance(entrypoint, bool):
        raise ValueError("bridge entrypoint is malformed")
    if not isinstance(members, list) or not members:
        raise ValueError("bridge package is empty")
    raw_members = cast(list[object], members)
    if entrypoint < 0 or entrypoint >= len(raw_members):
        raise ValueError("bridge entrypoint is outside the package")
    if not all(isinstance(member, dict) for member in raw_members):
        raise ValueError("bridge member is malformed")
    _require_sha(payload["package_member_set_sha256"], "package member set")
    canonical_members = cast(list[dict[str, object]], members)
    expected_set = _digest(
        [
            {
                "blob_sha256": member.get("blob_sha256"),
                "byte_size": member.get("byte_size"),
                "document_version_id": member.get("document_version_id"),
                "media_type": member.get("media_type"),
                "member_ordinal": member.get("member_ordinal"),
                "member_role": member.get("member_role"),
                "source_url": member.get("source_url"),
            }
            for member in canonical_members
        ]
    )
    if expected_set != payload["package_member_set_sha256"]:
        raise ValueError("bridge package member-set digest does not match")
    return payload


def _verify_members(members: list[dict[str, object]]) -> None:
    required = {
        "blob_sha256",
        "byte_size",
        "document_version_id",
        "local_path",
        "media_type",
        "member_ordinal",
        "member_role",
        "source_url",
    }
    for ordinal, member in enumerate(members):
        if set(member) != required or member["member_ordinal"] != ordinal:
            raise ValueError("bridge member fields or order are invalid")
        path_value = member["local_path"]
        if not isinstance(path_value, str):
            raise ValueError("bridge member path is malformed")
        path = Path(path_value)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("bridge member path is unavailable")
        body = path.read_bytes()
        if len(body) != member["byte_size"] or _sha(body) != member["blob_sha256"]:
            raise ValueError("bridge member digest or size does not match")


def _fact_payload(
    fact: ExtractedFact,
    *,
    input_ordinal: int,
    members: list[dict[str, object]],
    accession_number: str,
    expected_cik: str,
) -> dict[str, object]:
    if fact.observed_cik != expected_cik:
        raise ValueError("Arelle fact CIK differs from the request")
    if fact.member_ordinal < 0 or fact.member_ordinal >= len(members):
        raise ValueError("Arelle fact member is outside the package")
    member = members[fact.member_ordinal]
    locator: dict[str, JsonValue] = {
        "filing_ordinal": input_ordinal,
        "source_ref": cast(str, member["source_url"]),
        "xbrl_concept_name": fact.concept_name,
        "xbrl_concept_namespace": fact.concept_namespace,
        "xbrl_context_id": fact.context_id,
        "xbrl_element_path": fact.element_path,
        "xbrl_fact_id": fact.fact_id,
        "xbrl_package_member": cast(str, member["source_url"]),
    }
    locator_sha = _digest(locator)
    raw_sha = _digest(fact.canonical_raw_fact)
    source_entry = {
        "accession_number": accession_number,
        "observed_cik": fact.observed_cik,
        "package_member_blob_sha256": member["blob_sha256"],
        "package_member_ordinal": fact.member_ordinal,
        "raw_fact_sha256": raw_sha,
        "source_locator_sha256": locator_sha,
    }
    footnotes = [
        {
            "canonical_footnote": item,
            "footnote_ordinal": ordinal,
            "footnote_sha256": _digest(item),
        }
        for ordinal, item in enumerate(fact.footnotes)
    ]
    return {
        "accession_number": accession_number,
        "canonical_raw_fact": fact.canonical_raw_fact,
        "evidence_text": fact.evidence_text,
        "footnotes": footnotes,
        "input_ordinal": input_ordinal,
        "normalization_outcome": "rejected",
        "normalized_fact": None,
        "observed_cik": fact.observed_cik,
        "package_member_blob_sha256": member["blob_sha256"],
        "package_member_ordinal": fact.member_ordinal,
        "raw_fact_sha256": raw_sha,
        "rejection_detail": (
            "Raw Arelle fact was preserved; filing-native semantic normalization "
            "has not been separately qualified for this processor bundle."
        ),
        "rejection_reason_code": "normalization_not_qualified",
        "source_entry_sha256": _digest(source_entry),
        "source_locator": locator,
        "source_locator_sha256": locator_sha,
    }


def _extract_arelle_facts(
    request: dict[str, object],
    members: list[dict[str, object]],
) -> ArelleExtraction:
    session_factory = cast(
        _SessionFactory,
        _attribute(importlib.import_module("arelle.api.Session"), "Session"),
    )
    options_factory = cast(
        _OptionsFactory,
        _attribute(importlib.import_module("arelle.RuntimeOptions"), "RuntimeOptions"),
    )
    primary = members[cast(int, request["entrypoint_ordinal"])]
    locked_cache = Path(sys.prefix) / "Lib" / "site-packages" / "arelle" / "resources" / "cache"
    if not locked_cache.is_dir():
        raise ValueError("Arelle locked built-in cache is unavailable")
    with _deny_python_network() as attempts:
        options = options_factory(
            cacheDirectory=str(locked_cache),
            disablePersistentConfig=True,
            entrypointFile=cast(str, primary["local_path"]),
            internetConnectivity="offline",
            keepOpen=True,
            logFile="logToBuffer",
            plugins="xule|EDGAR",
        )
        with session_factory() as session:
            if not session.run(options):
                raise ValueError("Arelle rejected the captured filing package")
            models = session.get_models()
            if len(models) != 1:
                raise ValueError("Arelle did not produce one filing model")
            model = models[0]
            loaded_ordinals = _loaded_document_ordinals(model, members)
            extracted = _facts_from_model(model, members)
        if attempts[0] != 0:
            raise ValueError("Arelle attempted network access in offline mode")
    return ArelleExtraction(
        facts=extracted,
        loaded_member_ordinals=loaded_ordinals,
    )


def _loaded_document_ordinals(
    model: object,
    members: list[dict[str, object]],
) -> tuple[int, ...]:
    declared = {
        Path(os.path.abspath(cast(str, member["local_path"]))): ordinal
        for ordinal, member in enumerate(members)
    }
    runtime_root = Path(os.path.abspath(sys.prefix))
    raw_url_docs = getattr(model, "urlDocs", {})
    documents = (
        list(cast("dict[object, object]", raw_url_docs).values())
        if isinstance(raw_url_docs, dict)
        else []
    )
    model_document = getattr(model, "modelDocument", None)
    if model_document is not None and model_document not in documents:
        documents.append(model_document)
    if not documents:
        raise ValueError("Arelle filing model exposes no loaded document closure")
    loaded: set[int] = set()
    for document in documents:
        paths = _document_local_paths(document)
        ordinal = next((declared[path] for path in paths if path in declared), None)
        if ordinal is not None:
            loaded.add(ordinal)
            continue
        if any(_path_is_within(path, runtime_root) for path in paths):
            continue
        raise ValueError("Arelle loaded an undeclared document outside the locked runtime")
    primary_ordinals = {
        ordinal
        for ordinal, member in enumerate(members)
        if member["member_role"] == "primary_document"
    }
    if not primary_ordinals.issubset(loaded):
        raise ValueError("Arelle did not load the declared primary document")
    return tuple(sorted(loaded))


def _document_local_paths(document: object) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for attribute in ("filepath", "uri"):
        raw = str(getattr(document, attribute, "") or "")
        if not raw:
            continue
        parsed = urlsplit(raw)
        if re.match(r"^[A-Za-z]:[\\/]", raw):
            candidate = Path(raw)
        elif parsed.scheme.casefold() == "file":
            rendered = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", rendered):
                rendered = rendered[1:]
            candidate = Path(rendered)
        elif parsed.scheme:
            continue
        else:
            candidate = Path(raw)
        if candidate.is_absolute():
            paths.add(Path(os.path.abspath(candidate)))
    if not paths:
        raise ValueError("Arelle loaded document has no attributable local path")
    return tuple(sorted(paths, key=lambda path: str(path).casefold()))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _facts_from_model(
    model: object,
    members: list[dict[str, object]],
) -> tuple[ExtractedFact, ...]:
    raw_facts = cast("list[object] | set[object]", getattr(model, "factsInInstance", ()))
    ordered = sorted(raw_facts, key=_fact_sort_key)
    return tuple(
        _extracted_fact(model, fact, members, ordinal) for ordinal, fact in enumerate(ordered)
    )


def _extracted_fact(
    model: object,
    fact: object,
    members: list[dict[str, object]],
    ordinal: int,
) -> ExtractedFact:
    qname = getattr(fact, "qname", None)
    namespace = str(getattr(qname, "namespaceURI", ""))
    concept_name = str(getattr(qname, "localName", ""))
    context = getattr(fact, "context", None)
    context_id = str(getattr(fact, "contextID", ""))
    entity_identifier = cast(
        "tuple[object, object] | None", getattr(context, "entityIdentifier", None)
    )
    observed_cik = _canonical_cik("" if entity_identifier is None else str(entity_identifier[1]))
    value = str(getattr(fact, "value", getattr(fact, "textValue", "")))
    fact_id = str(getattr(fact, "id", "") or f"fact-{ordinal}")
    member_ordinal = _member_ordinal(fact, members)
    canonical_raw: dict[str, JsonValue] = {
        "concept_name": concept_name,
        "concept_namespace": namespace,
        "context_id": context_id,
        "decimals": _optional_text(getattr(fact, "decimals", None)),
        "fact_id": fact_id,
        "is_nil": bool(getattr(fact, "isNil", False)),
        "precision": _optional_text(getattr(fact, "precision", None)),
        "unit_id": _optional_text(getattr(fact, "unitID", None)),
        "value": value,
    }
    return ExtractedFact(
        member_ordinal=member_ordinal,
        fact_id=fact_id,
        element_path=_element_path(fact, ordinal),
        concept_namespace=namespace,
        concept_name=concept_name,
        context_id=context_id,
        observed_cik=observed_cik,
        evidence_text=f"{concept_name} {value}".strip() or fact_id,
        canonical_raw_fact=canonical_raw,
        footnotes=_footnotes(model, fact, fact_id),
    )


def _footnotes(
    model: object,
    fact: object,
    fact_id: str,
) -> tuple[dict[str, JsonValue], ...]:
    relationship_factory = cast(Callable[[str], object], _attribute(model, "relationshipSet"))
    footnote_arcrole = cast(
        str, _attribute(importlib.import_module("arelle.XbrlConst"), "factFootnote")
    )
    relationship_set = relationship_factory(footnote_arcrole)
    from_model_object = cast(
        Callable[[object], list[object]],
        _attribute(relationship_set, "fromModelObject"),
    )
    relationships = from_model_object(fact)
    values: list[dict[str, JsonValue]] = []
    for relation in relationships:
        target = getattr(relation, "toModelObject", None)
        if target is None:
            raise ValueError("Arelle footnote relationship has no target")
        values.append(
            {
                "fact_id": fact_id,
                "language": _optional_text(getattr(target, "xmlLang", None)),
                "role": _optional_text(getattr(target, "role", None)),
                "text": str(getattr(target, "textValue", getattr(target, "value", ""))),
            }
        )
    return tuple(sorted(values, key=_canonical))


def _member_ordinal(fact: object, members: list[dict[str, object]]) -> int:
    document = getattr(fact, "modelDocument", None)
    candidates = {
        str(getattr(document, "filepath", "")).casefold(),
        str(getattr(document, "uri", "")).casefold(),
    }
    for ordinal, member in enumerate(members):
        local = str(Path(cast(str, member["local_path"])).resolve()).casefold()
        if local in candidates:
            return ordinal
    raise ValueError("Arelle fact cannot be bound to one declared instance member")


def _element_path(fact: object, ordinal: int) -> str:
    try:
        tree = cast(_Fact, fact).getroottree()
        path = str(tree.getpath(fact))
    except (AttributeError, TypeError, ValueError):
        path = f"/xbrl/fact[{ordinal + 1}]"
    return path


def _fact_sort_key(fact: object) -> tuple[str, int, str, str, str, str]:
    document = getattr(fact, "modelDocument", None)
    qname = getattr(fact, "qname", None)
    return (
        str(getattr(document, "uri", "")),
        int(getattr(fact, "sourceline", 0) or 0),
        str(getattr(qname, "namespaceURI", "")),
        str(getattr(qname, "localName", "")),
        str(getattr(fact, "contextID", "")),
        str(getattr(fact, "value", "")),
    )


@contextmanager
def _deny_python_network() -> Generator[list[int]]:
    attempts = [0]

    def denied_connect(*_args: object, **_kwargs: object) -> None:
        attempts[0] += 1
        raise OSError("network denied by filing-XBRL bridge")

    def denied_connect_ex(*_args: object, **_kwargs: object) -> int:
        attempts[0] += 1
        return 10013

    with (
        patch.object(socket.socket, "connect", denied_connect),
        patch.object(socket.socket, "connect_ex", denied_connect_ex),
    ):
        yield attempts


def _canonical_cik(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits or len(digits) > 10:
        raise ValueError("Arelle fact entity identifier is not a SEC CIK")
    return digits.zfill(10)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _attribute(value: object, name: str) -> object:
    return getattr(value, name)


def _require_sha(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} SHA-256 is malformed")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return _sha(_canonical(value).encode())


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=(_PROTOCOL,), required=True)
    parser.add_argument("--runtime-artifact-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise ValueError("bridge request must be a JSON object")
        result = process_request(
            cast(dict[str, object], raw),
            runtime_artifact_sha256=str(args.runtime_artifact_sha256),
        )
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {"error_type": type(exc).__name__, "event": "filing_xbrl_bridge_refused"},
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    sys.stdout.write(_canonical(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
