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
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias, cast
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

_PROTOCOL = "filing-xbrl-bridge.v1"
_SANDBOX_CONTRACT = "earnings-xbrl-os-sandbox.v1"
_COORDINATES = {"arelle": "2.39.8", "edgar": "26.1", "xule": "30052"}
_SHA256_LENGTH = 64
_INLINE_NAMESPACES = frozenset(
    {
        "http://www.xbrl.org/2008/inlinexbrl",
        "http://www.xbrl.org/2013/inlinexbrl",
        "http://www.xbrl.org/2021/inlinexbrl",
        "https://www.xbrl.org/2021/inlinexbrl",
    }
)
_INLINE_FACT_NAMES = frozenset({"fraction", "nonFraction", "nonNumeric"})
JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None


class _Session(Protocol):
    def __enter__(self) -> _Session: ...

    def __exit__(self, *args: object) -> None: ...

    def run(self, options: object) -> bool: ...

    def get_models(self) -> list[object]: ...

    def get_logs(self, log_format: str, clear_logs: bool = False) -> str: ...


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
    unit_id: str | None = None
    normalized_fact: dict[str, JsonValue] | None = None
    rejection_reason_code: str | None = "normalization_not_qualified"
    rejection_detail: str | None = (
        "Raw Arelle fact was preserved; filing-native semantic normalization "
        "has not been separately qualified for this processor bundle."
    )


@dataclass(frozen=True)
class ArelleExtraction:
    facts: tuple[ExtractedFact, ...]
    loaded_member_ordinals: tuple[int, ...]
    source_inline_fact_count: int | None = None


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
    if (
        extraction.source_inline_fact_count is not None
        and len(facts) != extraction.source_inline_fact_count
    ):
        raise ValueError(
            "Arelle fact census does not match the independently parsed Inline XBRL source"
        )
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
    if fact.unit_id is not None:
        locator["xbrl_unit_id"] = fact.unit_id
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
    normalized = fact.normalized_fact is not None
    if normalized and (fact.rejection_reason_code is not None or fact.rejection_detail is not None):
        raise ValueError("normalized Arelle fact carries rejection evidence")
    if not normalized and (fact.rejection_reason_code is None or fact.rejection_detail is None):
        raise ValueError("rejected Arelle fact lacks rejection evidence")
    return {
        "accession_number": accession_number,
        "canonical_raw_fact": fact.canonical_raw_fact,
        "evidence_text": fact.evidence_text,
        "footnotes": footnotes,
        "input_ordinal": input_ordinal,
        "normalization_outcome": "normalized" if normalized else "rejected",
        "normalized_fact": fact.normalized_fact,
        "observed_cik": fact.observed_cik,
        "package_member_blob_sha256": member["blob_sha256"],
        "package_member_ordinal": fact.member_ordinal,
        "raw_fact_sha256": raw_sha,
        "rejection_detail": fact.rejection_detail,
        "rejection_reason_code": fact.rejection_reason_code,
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
    entrypoint_ordinal = cast(int, request["entrypoint_ordinal"])
    primary = members[entrypoint_ordinal]
    source_inline_fact_count = _inline_package_fact_count(members, entrypoint_ordinal)
    locked_cache = Path(sys.prefix) / "offline-cache"
    if not locked_cache.is_dir():
        raise ValueError("Arelle sealed offline taxonomy cache is unavailable")
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
            _require_clean_arelle_logs(session.get_logs("json"))
        if attempts[0] != 0:
            raise ValueError("Arelle attempted network access in offline mode")
    return ArelleExtraction(
        facts=extracted,
        loaded_member_ordinals=loaded_ordinals,
        source_inline_fact_count=source_inline_fact_count,
    )


def _inline_package_fact_count(
    members: list[dict[str, object]],
    entrypoint_ordinal: int,
) -> int:
    counts: dict[int, int] = {}
    namespace_markers = tuple(value.encode().lower() for value in _INLINE_NAMESPACES)
    for ordinal, member in enumerate(members):
        if member["member_role"] not in {"primary_document", "filing_attachment"}:
            continue
        path = Path(cast(str, member["local_path"]))
        body = path.read_bytes().lower()
        if not any(marker in body for marker in namespace_markers):
            continue
        count = _inline_fact_count(path)
        if count:
            counts[ordinal] = count
    extra_inline_members = tuple(sorted(set(counts) - {entrypoint_ordinal}))
    if extra_inline_members:
        raise ValueError(
            "multi-document Inline XBRL packages require separately qualified IXDS handling"
        )
    return counts.get(entrypoint_ordinal, 0)


def _inline_fact_count(path: Path) -> int:
    etree = importlib.import_module("lxml.etree")
    parser_factory = cast(Callable[..., object], _attribute(etree, "XMLParser"))
    parse = cast(Callable[[str, object], object], _attribute(etree, "parse"))
    parser = parser_factory(
        load_dtd=False,
        no_network=True,
        recover=False,
        resolve_entities=False,
    )
    tree = parse(str(path), parser)
    getroot = cast(Callable[[], object], _attribute(tree, "getroot"))
    root = getroot()
    iterator = cast(Callable[[], Iterable[object]], _attribute(root, "iter"))
    count = 0
    for element in iterator():
        tag = getattr(element, "tag", None)
        if not isinstance(tag, str) or not tag.startswith("{") or "}" not in tag:
            continue
        namespace, local_name = tag[1:].split("}", 1)
        if namespace.casefold() in _INLINE_NAMESPACES and local_name in _INLINE_FACT_NAMES:
            count += 1
    return count


def _require_clean_arelle_logs(raw_logs: str) -> None:
    try:
        payload = cast(object, json.loads(raw_logs))
    except json.JSONDecodeError as exc:
        raise ValueError("Arelle log buffer is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("Arelle log buffer is not closed")
    typed_payload = cast(dict[object, object], payload)
    if set(typed_payload) != {"log"}:
        raise ValueError("Arelle log buffer is not closed")
    logs = typed_payload["log"]
    if not isinstance(logs, list):
        raise ValueError("Arelle log entries are malformed")
    for raw_entry in cast(list[object], logs):
        if not isinstance(raw_entry, dict):
            raise ValueError("Arelle log entry is malformed")
        entry = cast(dict[object, object], raw_entry)
        level = str(entry.get("level", "")).casefold()
        if level in {"error", "critical"}:
            code = str(entry.get("code", "unknown"))
            raise ValueError(f"Arelle reported a fatal {code} error")


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
    fiscal_year, fiscal_period, filing_period_end = _filing_focus(ordered)
    accounting_basis = _accounting_basis(ordered)
    return tuple(
        _extracted_fact(
            model,
            fact,
            members,
            ordinal,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_period_end=filing_period_end,
            accounting_basis=accounting_basis,
        )
        for ordinal, fact in enumerate(ordered)
    )


def _extracted_fact(
    model: object,
    fact: object,
    members: list[dict[str, object]],
    ordinal: int,
    *,
    fiscal_year: int | None,
    fiscal_period: str | None,
    filing_period_end: datetime | None,
    accounting_basis: str,
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
    unit_id = _optional_text(getattr(fact, "unitID", None))
    canonical_raw: dict[str, JsonValue] = {
        "concept_name": concept_name,
        "concept_namespace": namespace,
        "context_id": context_id,
        "decimals": _optional_text(getattr(fact, "decimals", None)),
        "fact_id": fact_id,
        "is_nil": bool(getattr(fact, "isNil", False)),
        "precision": _optional_text(getattr(fact, "precision", None)),
        "raw_lexical_value": str(getattr(fact, "textValue", "")),
        "unit_id": unit_id,
        "value": value,
    }
    normalized, rejection_code, rejection_detail = _normalize_fact(
        fact,
        concept_namespace=namespace,
        concept_name=concept_name,
        context=context,
        context_id=context_id,
        unit_id=unit_id,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        filing_period_end=filing_period_end,
        accounting_basis=accounting_basis,
    )
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
        unit_id=unit_id,
        normalized_fact=normalized,
        rejection_reason_code=rejection_code,
        rejection_detail=rejection_detail,
    )


def _qname_parts(qname: object) -> tuple[str, str]:
    namespace = str(getattr(qname, "namespaceURI", ""))
    local_name = str(getattr(qname, "localName", ""))
    if not namespace or not local_name:
        raise ValueError("XBRL QName is incomplete")
    return namespace, local_name


def _taxonomy_identity(namespace: str) -> tuple[str, str]:
    version = namespace.rstrip("/").rsplit("/", 1)[-1]
    if not version:
        raise ValueError("XBRL taxonomy namespace has no version identity")
    lowered = namespace.casefold()
    if "/us-gaap/" in lowered:
        return "US GAAP", version
    if "/ifrs-full/" in lowered or "ifrs.org" in lowered:
        return "IFRS", version
    if "/dei/" in lowered:
        return "DEI", version
    if "/srt/" in lowered:
        return "SRT", version
    if "/country/" in lowered:
        return "SEC Country", version
    return "Issuer Extension", version


def _accounting_basis(facts: Sequence[object]) -> str:
    namespaces = {_qname_parts(getattr(fact, "qname", None))[0].casefold() for fact in facts}
    has_us_gaap = any("/us-gaap/" in namespace for namespace in namespaces)
    has_ifrs = any(
        "/ifrs-full/" in namespace or "ifrs.org" in namespace for namespace in namespaces
    )
    if has_us_gaap and has_ifrs:
        raise ValueError("filing mixes US GAAP and IFRS taxonomy identities")
    if has_us_gaap:
        return "us_gaap"
    if has_ifrs:
        return "ifrs"
    return "other"


def _filing_focus(
    facts: Sequence[object],
) -> tuple[int | None, str | None, datetime | None]:
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    period_end: datetime | None = None
    allowed_periods = {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "YTD", "TTM", "OTHER"}
    for fact in facts:
        namespace, concept_name = _qname_parts(getattr(fact, "qname", None))
        if re.fullmatch(r"https?://xbrl\.sec\.gov/dei/\d{4}", namespace) is None:
            continue
        value = str(getattr(fact, "value", "")).strip()
        if concept_name == "DocumentFiscalYearFocus" and value.isdigit():
            fiscal_year = int(value)
        elif concept_name == "DocumentFiscalPeriodFocus" and value in allowed_periods:
            fiscal_period = value
        elif concept_name == "DocumentPeriodEndDate":
            try:
                period_end = datetime.fromisoformat(value)
            except ValueError:
                period_end = None
    return fiscal_year, fiscal_period, period_end


def _context_period(context: object) -> tuple[str, datetime | None, datetime]:
    if bool(getattr(context, "isInstantPeriod", False)):
        instant = getattr(context, "instantDatetime", None)
        if not isinstance(instant, datetime):
            raise ValueError("instant XBRL context has no instant")
        return "instant", None, instant - timedelta(days=1)
    if bool(getattr(context, "isStartEndPeriod", False)):
        start = getattr(context, "startDatetime", None)
        end = getattr(context, "endDatetime", None)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise ValueError("duration XBRL context has incomplete dates")
        return "duration", start, end - timedelta(days=1)
    raise ValueError("forever XBRL contexts are not admitted")


def _context_dimensions(context: object) -> list[JsonValue]:
    raw_dimensions = getattr(context, "qnameDims", {})
    if not isinstance(raw_dimensions, dict):
        raise ValueError("XBRL context dimensions are malformed")
    dimensions: list[JsonValue] = []
    for raw_axis, raw_dimension in sorted(
        cast(dict[object, object], raw_dimensions).items(), key=lambda item: str(item[0])
    ):
        axis_namespace, axis_name = _qname_parts(raw_axis)
        if bool(getattr(raw_dimension, "isExplicit", False)):
            member_namespace, member_name = _qname_parts(
                getattr(raw_dimension, "memberQname", None)
            )
            dimensions.append(
                {
                    "application": "explicit",
                    "axis_name": axis_name,
                    "axis_namespace": axis_namespace,
                    "explicit_member_name": member_name,
                    "explicit_member_namespace": member_namespace,
                    "member_kind": "explicit",
                    "typed_member_value": None,
                }
            )
            continue
        typed_member = getattr(raw_dimension, "typedMember", None)
        typed_text = (
            "" if typed_member is None else str(getattr(typed_member, "textValue", typed_member))
        )
        if not typed_text:
            raise ValueError("typed XBRL dimension has no canonical value")
        dimensions.append(
            {
                "application": "explicit",
                "axis_name": axis_name,
                "axis_namespace": axis_namespace,
                "explicit_member_name": None,
                "explicit_member_namespace": None,
                "member_kind": "typed",
                "typed_member_value": {"text": typed_text},
            }
        )
    return dimensions


def _fact_unit(fact: object, unit_id: str | None) -> tuple[str, str | None]:
    if unit_id is None:
        return "pure", None
    unit = getattr(fact, "unit", None)
    measures = cast(object, getattr(unit, "measures", ((), ()))) if unit is not None else ((), ())
    raw_numerator: object = ()
    if isinstance(measures, (tuple, list)) and measures:
        raw_numerator = cast(Sequence[object], measures)[0]
    numerator = (
        tuple(cast(Sequence[object], raw_numerator))
        if isinstance(raw_numerator, (tuple, list))
        else ()
    )
    currency: str | None = None
    if len(numerator) == 1:
        namespace, local_name = _qname_parts(numerator[0])
        if "iso4217" in namespace.casefold() and len(local_name) == 3:
            currency = local_name.upper()
    return unit_id, currency


def _normalize_fact(
    fact: object,
    *,
    concept_namespace: str,
    concept_name: str,
    context: object,
    context_id: str,
    unit_id: str | None,
    fiscal_year: int | None,
    fiscal_period: str | None,
    filing_period_end: datetime | None,
    accounting_basis: str,
) -> tuple[dict[str, JsonValue] | None, str | None, str | None]:
    if context is None or not context_id:
        return None, "context_unavailable", "Arelle fact has no complete source context"
    if int(getattr(fact, "xValid", 0)) < 4:
        return None, "arelle_value_invalid", "Arelle did not validate the fact value"
    try:
        period_kind, period_start, period_end = _context_period(context)
        taxonomy_name, taxonomy_version = _taxonomy_identity(concept_namespace)
        dimensions = _context_dimensions(context)
        unit_key, currency = _fact_unit(fact, unit_id)
        is_nil = bool(getattr(fact, "isNil", False))
        numeric_value: str | None = None
        text_value: str | None = None
        if is_nil:
            value_kind = "nil"
        elif bool(getattr(fact, "isNumeric", False)):
            numeric_value = str(getattr(fact, "xValue", ""))
            if not numeric_value:
                raise ValueError("numeric XBRL fact has no validated value")
            if unit_id is None:
                raise ValueError("numeric XBRL fact has no source unit")
            value_kind = "numeric"
        else:
            text_value = str(getattr(fact, "xValue", ""))
            if not text_value:
                raise ValueError("text XBRL fact has no validated value")
            value_kind = "text"
        focused = filing_period_end is not None and period_end.date() == filing_period_end.date()
        normalized: dict[str, JsonValue] = {
            "accounting_basis": accounting_basis,
            "concept_name": concept_name,
            "concept_namespace": concept_namespace,
            "consolidation_scope": "consolidated" if not dimensions else "other",
            "currency": currency,
            "decimals": _optional_text(getattr(fact, "decimals", None)),
            "dimensions": dimensions,
            "effective_at": period_end.isoformat(),
            "fiscal_period": fiscal_period if focused else None,
            "fiscal_year": fiscal_year if focused else None,
            "is_nil": is_nil,
            "numeric_value": numeric_value,
            "period_end": period_end.isoformat(),
            "period_kind": period_kind,
            "period_start": None if period_start is None else period_start.isoformat(),
            "precision": _optional_text(getattr(fact, "precision", None)),
            "raw_lexical_value": _optional_text(getattr(fact, "textValue", None)),
            "revision_kind": "initial",
            "source_context_id": context_id,
            "source_taxonomy_version": taxonomy_version,
            "source_unit_id": unit_id,
            "supersedes_observation_id": None,
            "taxonomy_name": taxonomy_name,
            "text_value": text_value,
            "unit_key": unit_key,
            "value_kind": value_kind,
        }
        return normalized, None, None
    except (AttributeError, TypeError, ValueError) as exc:
        detail = str(exc)[:4096] or type(exc).__name__
        return None, "deterministic_normalization_rejected", detail


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


def _configure_standard_streams() -> None:
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            raise ValueError("bridge standard stream encoding cannot be pinned")
        cast(Callable[..., None], reconfigure)(encoding="utf-8", errors="strict")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _configure_standard_streams()
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
