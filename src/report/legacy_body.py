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

CANONICAL_READER_GROUP_LABELS: dict[str, str] = {
    "overview": "Overview & Moat",
    "quarter": "Quarter & Guidance",
    "financials": "Financials & DCF",
    "thesis-risk": "Thesis & Risk",
    "valuation-comps": "Valuation & Comps",
    "sources": "Sources & Citations",
}

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
_FETCH_ATTRS = (
    "action",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "manifest",
    "ping",
    "poster",
    "src",
    "srcset",
    "xlink:href",
)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_SVG_STATIC_TAGS = {
    "a",
    "circle",
    "clippath",
    "defs",
    "desc",
    "ellipse",
    "g",
    "lineargradient",
    "line",
    "mask",
    "path",
    "polygon",
    "polyline",
    "radialgradient",
    "rect",
    "stop",
    "svg",
    "text",
    "title",
    "tspan",
    "use",
}
_SVG_STATIC_ATTRS = {
    "aria-hidden",
    "aria-label",
    "class",
    "clip-path",
    "color",
    "cx",
    "cy",
    "d",
    "dx",
    "dy",
    "fill",
    "fill-opacity",
    "font-size",
    "font-weight",
    "height",
    "href",
    "id",
    "mask",
    "offset",
    "opacity",
    "pathlength",
    "points",
    "preserveaspectratio",
    "r",
    "rel",
    "role",
    "rx",
    "ry",
    "stop-color",
    "stop-opacity",
    "stroke",
    "stroke-dasharray",
    "stroke-dashoffset",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-opacity",
    "stroke-width",
    "target",
    "text-anchor",
    "transform",
    "viewbox",
    "width",
    "x",
    "x1",
    "x2",
    "xlink:href",
    "xmlns",
    "y",
    "y1",
    "y2",
}
_SVG_URL_REFERENCE_ATTRS = {"clip-path", "fill", "mask", "stroke"}
_SVG_URL_REFERENCE = re.compile(r"url\(\s*(['\"]?)(#[A-Za-z0-9_.:-]+)\1\s*\)", re.I)


class ReaderContentMetrics(BaseModel):
    """Stable proof that governed content survived sanitization."""

    model_config = ConfigDict(frozen=True)

    normalized_text_sha256: str
    heading_count: int
    table_count: int
    table_cell_count: int
    link_count: int
    source_link_count: int
    image_count: int
    image_alt_sha256: str


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
    source_metrics: ReaderContentMetrics
    preserved_metrics: ReaderContentMetrics
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
    legacy_manifest_path: str
    body_path: str
    body_sha256: str
    text_sha256: str
    section_ids: tuple[str, ...]
    id_map: dict[str, str]
    source_metrics: ReaderContentMetrics
    preserved_metrics: ReaderContentMetrics
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


def _anchor_href_is_safe(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return True
    if candidate.startswith("#") or (candidate.startswith("/") and not candidate.startswith("//")):
        return True
    parsed = urlsplit(candidate)
    return parsed.scheme.lower() in ("http", "https", "mailto")


def _image_src_is_safe(value: str) -> bool:
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered.startswith(
        (
            "data:image/png;",
            "data:image/jpeg;",
            "data:image/gif;",
            "data:image/webp;",
        )
    ):
        return True
    return candidate.startswith("/source/") and not candidate.startswith("//")


def _classes(tag: Tag) -> list[str]:
    raw = tag.get("class")
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()
    return [str(value) for value in raw]


def _content_metrics(root: Tag) -> ReaderContentMetrics:
    normalized_text = _normalized_text(root)
    image_alt_text = "\n".join(str(image.get("alt", "")) for image in root.find_all("img"))
    return ReaderContentMetrics(
        normalized_text_sha256=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
        heading_count=len(root.find_all(re.compile(r"^h[1-6]$"))),
        table_count=len(root.find_all("table")),
        table_cell_count=len(root.find_all(("th", "td"))),
        link_count=len(root.find_all("a")),
        source_link_count=len(
            [
                link
                for link in root.find_all("a", href=True)
                if str(link.get("href", "")).startswith("/source/")
            ]
        ),
        image_count=len(root.find_all("img")),
        image_alt_sha256=hashlib.sha256(image_alt_text.encode("utf-8")).hexdigest(),
    )


def _accepted_source_metrics(root: Tag) -> ReaderContentMetrics:
    parsed = BeautifulSoup(str(root), "html.parser")
    accepted = parsed.select_one(".l1-root")
    if accepted is None:
        accepted = parsed.select_one('[data-report-body="v1"]')
    if accepted is None:
        raise ValueError("accepted reader body disappeared during source normalization")
    for tag in list(accepted.find_all(_DISALLOWED_TAGS)):
        tag.decompose()
    for form in list(accepted.find_all("form")):
        form.unwrap()
    return _content_metrics(accepted)


def _sanitize_static_svg(root: Tag, warnings: Counter[str]) -> None:
    for svg in list(root.find_all("svg")):
        for node in [svg, *svg.find_all(True)]:
            if node.parent is None:
                continue
            if str(node.name).lower() not in _SVG_STATIC_TAGS:
                warnings["svg_element_removed"] += 1
                node.decompose()
                continue
            for raw_name in list(node.attrs):
                name = str(raw_name).lower()
                if name not in _SVG_STATIC_ATTRS:
                    warnings["svg_attribute_removed"] += 1
                    del node.attrs[raw_name]
                    continue
                if name in _SVG_URL_REFERENCE_ATTRS:
                    value = str(node.get(raw_name, ""))
                    matches = tuple(_SVG_URL_REFERENCE.finditer(value))
                    if "url(" in value.lower() and (
                        not matches
                        or "".join(match.group(0) for match in matches).strip() != value.strip()
                    ):
                        warnings["svg_external_url_removed"] += 1
                        del node.attrs[raw_name]


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
    source_metrics = _accepted_source_metrics(root)
    warnings: Counter[str] = Counter()

    for tag in list(root.find_all(_DISALLOWED_TAGS)):
        warnings[f"{tag.name}_removed"] += 1
        tag.decompose()

    for form in list(root.find_all("form")):
        warnings["form_unwrapped"] += 1
        form.unwrap()

    _sanitize_static_svg(root, warnings)
    all_tags = [root, *root.find_all(True)]
    for tag in all_tags:
        for raw_name in list(tag.attrs):
            name = str(raw_name)
            if name.lower() == "style":
                warnings["inline_style_removed"] += 1
                del tag.attrs[raw_name]
            elif _is_executable_attr(name):
                warnings["executable_attribute_removed"] += 1
                del tag.attrs[raw_name]
        for name in _FETCH_ATTRS:
            value = tag.get(name)
            if value is None:
                continue
            rendered = (
                " ".join(str(part) for part in value) if isinstance(value, list) else str(value)
            )
            keep = False
            if tag.name == "a" and name == "href":
                keep = _anchor_href_is_safe(rendered)
            elif tag.name == "img" and name == "src":
                keep = _image_src_is_safe(rendered)
            elif tag.name == "use" and name in ("href", "xlink:href"):
                keep = rendered.startswith("#")
            if not keep:
                warnings[f"fetch_{name}_removed"] += 1
                del tag.attrs[name]
        if tag.name == "a" and str(tag.get("target", "")).lower() == "_blank":
            tag["rel"] = "noopener noreferrer"

    preserved_metrics = _content_metrics(root)
    if preserved_metrics != source_metrics:
        raise ValueError("governed content metrics changed during sanitization")

    for group in root.select(".tab-group-pane[data-tab-group]"):
        group_id = str(group.get("data-tab-group", "Research")).strip()
        label = CANONICAL_READER_GROUP_LABELS.get(
            group_id,
            group_id.replace("_", " ").replace("-", " ").title() or "Research",
        )
        heading = soup.new_tag("h2")
        heading["class"] = "reader-group-title"
        heading.string = label
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
        for link_name in ("href", "xlink:href"):
            href = tag.get(link_name)
            if isinstance(href, str) and href.startswith("#"):
                target = href[1:]
                if target in id_map:
                    tag[link_name] = f"#{id_map[target]}"
        for name in _SVG_URL_REFERENCE_ATTRS:
            value = tag.get(name)
            if not isinstance(value, str):
                continue
            tag[name] = _SVG_URL_REFERENCE.sub(
                lambda match: f"url(#{id_map.get(match.group(2)[1:], match.group(2)[1:])})",
                value,
            )

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
    preserved_reparsed = BeautifulSoup(str(reparsed_root), "html.parser")
    preserved_root = preserved_reparsed.select_one(".l1-root")
    if preserved_root is None:
        preserved_root = preserved_reparsed.select_one('[data-report-body="v1"]')
    if preserved_root is None:
        raise ValueError("reader body disappeared during fidelity verification")
    for heading in list(preserved_root.select(".reader-group-title")):
        heading.decompose()
    if _content_metrics(preserved_root) != source_metrics:
        raise ValueError("governed content metrics changed during serialization")
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
        source_metrics=source_metrics,
        preserved_metrics=preserved_metrics,
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
    "ReaderContentMetrics",
    "ReaderExtractionReceipt",
    "extract_legacy_reader_body",
]
