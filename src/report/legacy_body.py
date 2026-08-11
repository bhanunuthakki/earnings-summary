"""Fail-closed extraction of inert reader bodies from persisted workspace reports."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict

PARSER_VERSION = "legacy_workspace_reader.v1"

_DISALLOWED_TAGS = ("script", "style", "iframe", "object", "embed", "link", "meta")
_TOKEN_REFERENCE_ATTRS = (
    "aria-controls",
    "aria-describedby",
    "aria-labelledby",
    "aria-owns",
    "for",
    "headers",
    "list",
)
_URL_ATTRS = ("href", "src", "action", "formaction", "poster", "xlink:href")
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


class LegacyReaderBody(BaseModel):
    """Deterministic, inert content extracted from one immutable standalone report."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["legacy_reader_body.v1"] = "legacy_reader_body.v1"
    parser_version: Literal["legacy_workspace_reader.v1"] = PARSER_VERSION
    body_html: str
    body_sha256: str
    text_sha256: str
    section_ids: tuple[str, ...]
    id_map: dict[str, str]
    heading_count: int
    table_count: int
    link_count: int
    source_link_count: int
    warnings: tuple[str, ...]


class ReaderExtractionReceipt(BaseModel):
    """Auditable proof that a derived body came from one immutable source artifact."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["reader_extraction_receipt.v1"] = "reader_extraction_receipt.v1"
    body_schema_version: Literal["legacy_reader_body.v1"] = "legacy_reader_body.v1"
    parser_version: Literal["legacy_workspace_reader.v1"] = PARSER_VERSION
    artifact_id: str
    source_path: str
    source_sha256: str
    body_path: str
    body_sha256: str
    text_sha256: str
    section_ids: tuple[str, ...]
    id_map: dict[str, str]
    heading_count: int
    table_count: int
    link_count: int
    source_link_count: int
    warnings: tuple[str, ...]


def _normalized_text(root: Tag) -> str:
    return " ".join(root.stripped_strings)


def _safe_fragment(value: str) -> str:
    normalized = _SAFE_ID.sub("-", value).strip("-")
    return normalized or "node"


def _is_executable_attr(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("on")
        or lowered.startswith("x-")
        or lowered.startswith("@")
        or lowered.startswith(":")
        or lowered == "srcdoc"
    )


def _url_is_safe(name: str, value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return True
    if candidate.startswith("#") or (candidate.startswith("/") and not candidate.startswith("//")):
        return True
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if name in ("src", "poster"):
        return scheme == "data" and candidate.lower().startswith("data:image/")
    return scheme in ("http", "https", "mailto")


def _classes(tag: Tag) -> list[str]:
    raw = tag.get("class")
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()
    return [str(value) for value in raw]


def extract_legacy_reader_body(source_html: str, *, artifact_id: str) -> LegacyReaderBody:
    """Extract one complete workspace content subtree and strip executable ownership.

    Only the recognized ``.l1-root`` report structure is accepted. The original
    standalone document remains authoritative and is never modified here.
    """

    soup = BeautifulSoup(source_html, "html.parser")
    roots = soup.select(".l1-root")
    if not roots:
        roots = soup.select('[data-report-body="v1"]')
    if len(roots) != 1:
        raise ValueError("workspace must contain exactly one .l1-root or data-report-body=v1 root")
    root = roots[0]
    warnings: Counter[str] = Counter()

    for tag in list(root.find_all(_DISALLOWED_TAGS)):
        warnings[f"{tag.name}_removed"] += 1
        tag.decompose()

    for form in list(root.find_all("form")):
        warnings["form_unwrapped"] += 1
        form.unwrap()

    all_tags = [root, *root.find_all(True)]
    for tag in all_tags:
        for raw_name in list(tag.attrs):
            name = str(raw_name)
            if _is_executable_attr(name):
                warnings["executable_attribute_removed"] += 1
                del tag.attrs[raw_name]
        for name in _URL_ATTRS:
            value = tag.get(name)
            if value is None:
                continue
            rendered = (
                " ".join(str(part) for part in value) if isinstance(value, list) else str(value)
            )
            if not _url_is_safe(name, rendered):
                warnings[f"unsafe_{name}_removed"] += 1
                del tag.attrs[name]
        if tag.name == "a" and str(tag.get("target", "")).lower() == "_blank":
            tag["rel"] = "noopener noreferrer"

    for group in root.select(".tab-group-pane[data-tab-group]"):
        label = str(group.get("data-tab-group", "Research")).replace("_", " ").strip()
        heading = soup.new_tag("h2")
        heading["class"] = "reader-group-title"
        heading.string = label.title() or "Research"
        group.insert(0, heading)

    for pane in root.select(".tab-group-pane, .tab-pane, .subtab-pane"):
        classes = _classes(pane)
        if "active" not in classes:
            classes.append("active")
        pane["class"] = " ".join(classes)

    used_ids = {str(tag.get("id")) for tag in [root, *root.find_all(id=True)] if tag.get("id")}
    generated_counts: Counter[str] = Counter()
    for pane in root.select("[data-tab]"):
        if pane.get("id") is None:
            base = f"section-{_safe_fragment(str(pane.get('data-tab', 'section')))}"
            generated_counts[base] += 1
            candidate = base
            if candidate in used_ids:
                candidate = f"{base}-{generated_counts[base]}"
            while candidate in used_ids:
                generated_counts[base] += 1
                candidate = f"{base}-{generated_counts[base]}"
            pane["id"] = candidate
            used_ids.add(candidate)

    id_tags = [tag for tag in all_tags if tag.get("id") is not None]
    ids = [str(tag.get("id")) for tag in id_tags]
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"legacy workspace contains duplicate ids: {', '.join(duplicates)}")
    prefix = f"reader-{hashlib.sha256(artifact_id.encode('utf-8')).hexdigest()[:12]}-"
    id_map = {value: prefix + _safe_fragment(value) for value in ids}
    for tag in id_tags:
        old_id = str(tag["id"])
        tag["id"] = id_map[old_id]

    for tag in all_tags:
        for name in _TOKEN_REFERENCE_ATTRS:
            raw = tag.get(name)
            if raw is None:
                continue
            values = raw if isinstance(raw, list) else str(raw).split()
            rewritten = [id_map.get(str(value), str(value)) for value in values]
            tag[name] = " ".join(rewritten)
        href = tag.get("href")
        if isinstance(href, str) and href.startswith("#"):
            target = href[1:]
            if target in id_map:
                tag["href"] = f"#{id_map[target]}"

    section_ids = tuple(
        dict.fromkeys(
            str(tag.get("data-tab")) for tag in root.select("[data-tab]") if tag.get("data-tab")
        )
    )
    normalized_text = _normalized_text(root)
    if not normalized_text:
        raise ValueError("legacy workspace reader body is empty")
    body_html = str(root)
    reparsed = BeautifulSoup(body_html, "html.parser")
    reparsed_root = reparsed.select_one(".l1-root")
    if reparsed_root is None:
        reparsed_root = reparsed.select_one('[data-report-body="v1"]')
    if reparsed_root is None or _normalized_text(reparsed_root) != normalized_text:
        raise ValueError("legacy workspace text changed during serialization")
    body_sha256 = hashlib.sha256(body_html.encode("utf-8")).hexdigest()
    text_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    warning_labels = tuple(
        f"{label}:{count}" if count > 1 else label for label, count in sorted(warnings.items())
    )
    return LegacyReaderBody(
        body_html=body_html,
        body_sha256=body_sha256,
        text_sha256=text_sha256,
        section_ids=section_ids,
        id_map=id_map,
        heading_count=len(root.find_all(re.compile(r"^h[1-6]$"))),
        table_count=len(root.find_all("table")),
        link_count=len(root.find_all("a")),
        source_link_count=len(
            [
                link
                for link in root.find_all("a", href=True)
                if str(link.get("href", "")).startswith("/source/")
            ]
        ),
        warnings=warning_labels,
    )


__all__ = [
    "PARSER_VERSION",
    "LegacyReaderBody",
    "ReaderExtractionReceipt",
    "extract_legacy_reader_body",
]
