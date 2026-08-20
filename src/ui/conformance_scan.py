"""Shared, registry-backed design-conformance scanner.

The scanner owns detection mechanics only. Sanctioned values, selectors,
surface exemptions, and compatibility approvals all come from
``ui.design_registry`` so pytest and command-line enforcement cannot drift.
"""

from __future__ import annotations

import ast
import hashlib
import re
import token as _token
import tokenize
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from string import Formatter
from typing import cast

from ui.design_registry import (
    CHROME_TOKENS,
    DYNAMIC_VISUAL_CONTRACTS,
    EXEMPT,
    FONT_FAMILY_KEYWORDS,
    FONT_SIZE_EXEMPT,
    GLOBAL_MASTER_SOURCES,
    GRID_ARCHETYPES,
    INDENT_TOKEN_NAMES,
    LOCAL_PROPERTY_CONTRACTS,
    MASTER_GEOMETRY_CONTRACTS,
    MASTER_SOURCES,
    PALETTE_DARK,
    PALETTE_LIGHT,
    RADIUS_PX,
    RADIUS_SANCTIONED,
    RAIL_TOKEN_NAMES,
    RUNTIME_VISUAL_CONTRACTS,
    SHAPE_ARCHETYPES,
    SHAPES_BY_SELECTOR,
    TITLES_BY_SELECTOR,
    TYPE_SCALE_PX,
)
from ui.tokens import (
    FONT_TOKENS,
    INDENT_TOKENS,
    PALETTE_WHITE_OVERRIDES,
    RAIL_TOKENS,
    SPACING_SCALE,
    TYPE_SCALE,
)

# href-safe raw hex: a literal # + 3-8 hex digits, NOT preceded by a word char
# (so entities and CSS variables do not trip it), a quote (anchor fragments),
# or % (encoded data-URI colors).
_RAW_HEX = re.compile(r"""(?<![\w&%"'])#[0-9a-fA-F]{3,8}(?![0-9a-fA-F])""")
_FONT_SIZE = re.compile(
    r"font-size:\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem|em|pt|pc|ch|ex|vw|vh|vmin|vmax|%))",
    re.IGNORECASE,
)
_FONT_SIZE_DECL = re.compile(r"font-size\s*:\s*([^;}\"'<>]+)", re.IGNORECASE)
_RELATIVE_FONT_SIZE = re.compile(
    r"font-size:\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:rem|em|pt|pc|ch|ex|vw|vh|vmin|vmax|%))",
    re.IGNORECASE,
)
_FONT_SHORTHAND = re.compile(r"(?<![-\w])font\s*:\s*([^;}]+)", re.IGNORECASE)
_RADIUS_DECL = re.compile(r"border-radius:\s*([^;}]+)")
_PX = re.compile(r"([0-9.]+px)")
_FONT_FAMILY = re.compile(r"font-family:\s*([^;}]+)")
_FONT_TOKEN = re.compile(r"^var\(\s*--(?:sans|serif|mono)\b")
_ALIAS = re.compile(
    r"--(?:panel-alt|panel|bg-card|bg-elev|row-hover|ink-muted|ink|fg-muted|link"
    r"|font-serif|font-mono|font-body)(?![\w-])"
)
_FUNC_COLOR = re.compile(r"(?<![a-z])(?:rgba?|hsla?)\([^)]*\)", re.IGNORECASE)
_FONT_WEIGHT = re.compile(r"font-weight:\s*(bold|[789]00)\b")
_TRANSITION_ALL = re.compile(r"transition:\s*all\b")

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_NAMED_BADGE = re.compile(r"[.#][\w-]*(?:pill|badge|chip|tag)\b", re.IGNORECASE)
_KIT_BADGE = re.compile(r"\b(?:k-pill|k-chip|k-well)\b")
_STATUS_FILL = re.compile(r"background(?:-color)?:\s*color-mix\(in srgb, var\(--(?:ok|warn|bad)\)")

_INDENT_SCOPE_SEGMENT = re.compile(r"(?:^|-)(?:tree|indent|bucket)(?:-|$)", re.IGNORECASE)
_SHAPE_SCOPE_SEGMENT = re.compile(
    r"(?:^|-)(?:card|panel|box|tile|well|drawer)(?:-|$)", re.IGNORECASE
)
_CUSTOM_PROPERTY = re.compile(r"var\(\s*--([A-Za-z_][\w-]*)\s*\)", re.IGNORECASE)
_CUSTOM_PROPERTY_REFERENCE = re.compile(
    r"var\(\s*--([A-Za-z_][\w-]*)(?=\s*[,)]|\s*$)", re.IGNORECASE
)
_CUSTOM_PROPERTY_DECL = re.compile(r"(?<![-\w])--([A-Za-z_][\w-]*)\s*:", re.IGNORECASE)
_CUSTOM_PROPERTY_VALUE_DECL = re.compile(
    r"(?<![-\w])--([A-Za-z_][\w-]*)\s*:\s*([^;}]+)", re.IGNORECASE
)
_HTML_EMITTER = re.compile(
    r"<(?:!doctype|html|head|body|style|script|main|nav|aside|article|section|div|span|table|thead|tbody|tr|th|td|button|a|p|h[1-6]|ul|ol|li|input|select|option|textarea|label|img|canvas|video|audio|picture|source|track|iframe|object|embed|figure|figcaption|header|footer|pre|code|dl|dt|dd|form|details|summary|dialog)\b",
    re.IGNORECASE,
)
_SVG_EMITTER = re.compile(r"<(?:svg|path|circle|rect|line|polyline|polygon|text)\b", re.IGNORECASE)
_CSS_EMITTER = re.compile(r"(?:^|[}\s])[^{}<>]+\{\s*[-\w]+\s*:", re.MULTILINE)
_CSS_SELECTOR_START = re.compile(
    r"^(?:[.#:\[*]|(?:html|body|main|nav|aside|article|section|div|span|table|thead|tbody|tr|th|td|button|a|p|h[1-6]|ul|ol|li|input|select|option|textarea|label|img|canvas|video|audio|picture|iframe|figure|header|footer|pre|code|form|details|summary|dialog)\b)",
    re.IGNORECASE,
)
_RUNTIME_DOM_EMITTER = re.compile(
    r"(?:\.innerHTML\b|\.outerHTML\s*(?:\+?=)|\[['\"]outerHTML['\"]\]\s*(?:\+?=)|insertAdjacentHTML\s*\(|\.style(?:\.|\[)|\[['\"]style['\"]\]|\.attributeStyleMap\.set\s*\(|Object\.(?:assign|defineProperty)\s*\(\s*[^,;\r\n]*(?:\.style|\[['\"]style['\"]\])\s*,|Reflect\.set\s*\(\s*[^,;\r\n]*(?:\.style|\[['\"]style['\"]\])\s*,|\.classList\.|\.className\s*=|setAttribute\s*\(\s*['\"](?:class|style|fill|stroke)|createElement(?:NS)?\s*\(|\[\s*['\"]createElement(?:NS)?['\"]\s*\]\s*\(|\b\w*sheet\w*(?:\?\.|\.)(?:replace|replaceSync|insertRule)\s*\(|\b\w*sheet\w*\[['\"](?:replace|replaceSync|insertRule)['\"]\]\s*\(|CSSStyleSheet\.prototype(?:\.(?:replace|replaceSync|insertRule)|\[['\"](?:replace|replaceSync|insertRule)['\"]\])\.call\s*\(|(?:\.adoptedStyleSheets|\[['\"]adoptedStyleSheets['\"]\])\s*(?:=|(?:\.|\[['\"])(?:push|unshift|splice)(?:['\"]\])?\s*\()|\b\w*style\w*(?:\.(?:textContent|innerHTML)|\[['\"](?:textContent|innerHTML)['\"]\])\s*=|\b\w*style\w*(?:\.append|\[['\"]append['\"]\]|\?\.\[['\"]append['\"]\]\?\.)\s*\()",
    re.IGNORECASE,
)
_RUNTIME_VISUAL_MUTATION = re.compile(
    r"(?:\.style\.(?P<style_property>[A-Za-z_]\w*)\s*=\s*(?P<style_value>[^;\r\n]+)|(?P<outer_html_property>\.outerHTML)\s*=\s*(?P<outer_html_value>[^;\r\n]+)|\.style\.setProperty\s*\(|\[['\"]style['\"]\]\s*(?:=|\.|\[)|Object\.(?:assign|defineProperty)\s*\(\s*[^,;\r\n]*(?:\.style|\[['\"]style['\"]\])\s*,|Reflect\.set\s*\(\s*[^,;\r\n]*(?:\.style|\[['\"]style['\"]\])\s*,|\.attributeStyleMap\.set\s*\(|\.style\[|\.style\s*=|\.outerHTML\s*\+=|\[['\"]outerHTML['\"]\]\s*(?:\+?=)|setAttribute\s*\(\s*['\"](?:style|fill|stroke)['\"]|setAttributeNS\s*\(\s*[^,]+,\s*['\"](?:style|fill|stroke)['\"]|createElement\s*\(\s*['\"]style['\"]|\b\w*sheet\w*(?:\?\.|\.)(?:replace|replaceSync|insertRule)\s*\(|\b\w*sheet\w*\[['\"](?:replace|replaceSync|insertRule)['\"]\]\s*\(|CSSStyleSheet\.prototype(?:\.(?:replace|replaceSync|insertRule)|\[['\"](?:replace|replaceSync|insertRule)['\"]\])\.call\s*\(|(?:\.adoptedStyleSheets|\[['\"]adoptedStyleSheets['\"]\])\s*(?:=|(?:\.|\[['\"])(?:push|unshift|splice)(?:['\"]\])?\s*\()|\b\w*style\w*(?:\.(?:textContent|innerHTML)|\[['\"](?:textContent|innerHTML)['\"]\])\s*=|\b\w*style\w*(?:\.append|\[['\"]append['\"]\]|\?\.\[['\"]append['\"]\]\?\.)\s*\()",
    re.IGNORECASE,
)
_DYNAMIC_CREATE_ELEMENT = re.compile(
    r"(?:\b[A-Za-z_$][\w$]*\.(?:createElement|createElementNS)|\[['\"](?:createElement|createElementNS)['\"]\])\s*\(\s*(?!['\"])",
    re.IGNORECASE,
)
_INDIRECT_VISUAL_INVOCATION = re.compile(
    r"(?:Reflect\.apply\s*\(\s*[^,;\r\n]*(?:\.style\b|\.replace(?:Sync)?\b|\.insertRule\b|\.adoptedStyleSheets\.(?:push|unshift|splice)\b)"
    r"|(?:\.replace(?:Sync)?|\.insertRule|\.adoptedStyleSheets\.(?:push|unshift|splice))\.bind\s*\()",
    re.IGNORECASE,
)
_VISUAL_MUTATOR_ALIAS = re.compile(
    r"(?:\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*[^;\r\n]*"
    r"(?:\.style\.(?:setProperty|[A-Za-z_$][\w$]*)|\b\w*sheet\w*\.(?:replace(?:Sync)?|insertRule)\b|\.adoptedStyleSheets\.(?:push|unshift|splice)\b|\bstyleSheets\b)"
    r"|\b(?:const|let|var)\s*\{[^}\r\n]*(?:push|unshift|splice|replace|replaceSync|insertRule)[^}\r\n]*\}\s*=\s*[^;\r\n]*(?:adoptedStyleSheets|sheet))",
    re.IGNORECASE,
)
_RUNTIME_HTML_SINK = re.compile(
    r"(?:\bdocument\.(?:write|writeln)\s*\("
    r"|\bnew\s+DOMParser\s*\(\s*\)\.parseFromString\s*\("
    r"|\.createContextualFragment\s*\("
    r"|\.srcdoc\s*="
    r"|\.(?:setHTML|setHTMLUnsafe)\s*\("
    r"|\.createHTMLDocument\s*\("
    r"|\.execCommand\s*\(\s*['\"]insertHTML['\"]"
    r"|\.html\s*\("
    r"|\bdangerouslySetInnerHTML\s*=)",
    re.IGNORECASE,
)
_RUNTIME_STYLE_ELEMENT_SINK = re.compile(
    r"\b\w*style\w*(?:\.innerText\s*=|\.(?:appendChild|replaceChildren|insertBefore|prepend|replaceWith)\s*\()",
    re.IGNORECASE,
)
_RUNTIME_STYLESHEET_COLLECTION_SINK = re.compile(
    r"(?:\bstyleSheets\b[^;\r\n]{0,500}\.(?:insertRule|deleteRule|addRule|removeRule|replace|replaceSync)\s*\("
    r"|\.setAttribute\s*\(\s*['\"]srcdoc['\"])",
    re.IGNORECASE,
)
_DYNAMIC_CLASS_LIST = re.compile(
    r"\.classList\.(?:add|remove|toggle|replace)\s*\((?P<args>[^)\r\n]*)\)",
    re.IGNORECASE,
)
_DYNAMIC_CLASS_ATTRIBUTE = re.compile(
    r"(?:\.className\s*=|setAttribute\s*\(\s*['\"]class['\"]\s*,)\s*(?P<value>[^;\r\n)]+)",
    re.IGNORECASE,
)
_JS_STRING_LITERAL_ONLY = re.compile(r"^\s*(['\"])(?:\\.|(?!\1).)*\1\s*$", re.DOTALL)
_INLINE_STYLE_ATTRIBUTE = re.compile(
    r"\bstyle\s*=\s*(['\"])(?P<value>.*?)\1", re.IGNORECASE | re.DOTALL
)
_DYNAMIC_VALUE_PLACEHOLDER = re.compile(r"(?<!\{)\{[A-Za-z_][^{}]*\}(?!\})")
_JSX_INLINE_STYLE_API = re.compile(
    r"\bstyle\s*=\s*\{|\bstyle\??\s*:\s*(?:(?:React\.)?CSSProperties|Record\s*<)",
    re.IGNORECASE,
)
_REACT_NATIVE_PROP_DECL = re.compile(
    r"(?:export\s+)?(?:interface|type)\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?:extends|=)\s*(?P<body>[^;{]{0,1200}\b(?:"
    r"(?:React\.)?[A-Za-z_]*HTMLAttributes\s*<|"
    r"(?:React\.)?SVGAttributes\s*<|"
    r"(?:React\.)?(?:HTML|SVG)Props\s*<|"
    r"(?:React\.)?ComponentProps(?:WithRef|WithoutRef)?\s*<|"
    r"(?:React\.)?JSX\.IntrinsicElements\s*\["
    r")[^;{]{0,1200})(?:[;{])",
    re.IGNORECASE | re.DOTALL,
)
_SVG_PRESENTATION = re.compile(
    r"\b(fill|stroke|stroke-width|font-size|font-family)\s*=\s*(['\"])(.*?)\2",
    re.IGNORECASE | re.DOTALL,
)
_SIGNATURE_WORD = re.compile(r"(?<![-\w])([A-Za-z_][\w-]*)(?![\w-])")
_HEADING_TAG = re.compile(r"h[1-6]", re.IGNORECASE)
_CONDITIONAL_AT_RULE = re.compile(
    r"@(?:media|supports|container|layer|scope|document|(?:-\w+-)?keyframes)\b", re.IGNORECASE
)
_PYTHON_STRING_WRAPPER = re.compile(r"^(?:[rRuUbBfF]{0,3})?(?:'''|\"\"\"|'|\")")
_PYTHON_STRING_LITERAL = re.compile(r"^(?:[rRuUbBfF]{0,3})?(?P<quote>'''|\"\"\"|'|\")")
_PSEUDO_ELEMENT = re.compile(
    r"::[\w-]+|:(?:before|after|first-line|first-letter|marker|placeholder|selection|backdrop|file-selector-button)\b",
    re.IGNORECASE,
)
_STRING_TOKEN_BOUNDARY = "\n\f\n"
_MAX_STATIC_COMPOSITION = 1_000_000
_UNSUPPORTED_LITERAL = object()
_COMPOSITION_LIMIT_HIT: ContextVar[bool] = ContextVar(
    "design_conformance_composition_limit", default=False
)

DIMENSIONS = (
    "color",
    "canonical-token-redefinition",
    "consumer-visual-css",
    "dynamic-visual-contract",
    "font-size",
    "font-shorthand",
    "radius",
    "runtime-visual-mutation",
    "inline-style-attribute",
    "inline-style-api",
    "master-geometry-contract",
    "font-family",
    "alias",
    "kit-badge",
    "local-property-value",
    "font-weight",
    "transition",
    "floating-card-title",
    "off-scale-indent",
    "unsanctioned-shape-geometry",
    "off-scale-grid-column",
    "svg-presentation",
    "unknown-custom-property",
)

_CANONICAL_CUSTOM_PROPERTIES = frozenset(
    {
        *PALETTE_DARK,
        *PALETTE_LIGHT,
        *FONT_TOKENS,
        *TYPE_SCALE,
        *SPACING_SCALE,
        *INDENT_TOKEN_NAMES,
        *RAIL_TOKEN_NAMES,
        *CHROME_TOKENS,
    }
)
_CANONICAL_CUSTOM_PROPERTY_VALUES: Mapping[str, frozenset[str]] = {
    name: frozenset(
        value
        for mapping in (
            PALETTE_DARK,
            PALETTE_LIGHT,
            PALETTE_WHITE_OVERRIDES,
            FONT_TOKENS,
            TYPE_SCALE,
            SPACING_SCALE,
            INDENT_TOKENS,
            RAIL_TOKENS,
            CHROME_TOKENS,
        )
        if (value := mapping.get(name)) is not None
    )
    for name in _CANONICAL_CUSTOM_PROPERTIES
}
_CSS_NAMED_COLORS = frozenset(
    {
        "black",
        "blue",
        "brown",
        "cyan",
        "gold",
        "gray",
        "green",
        "grey",
        "lime",
        "magenta",
        "maroon",
        "navy",
        "olive",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "teal",
        "white",
        "yellow",
    }
)
_COLOR_PROPERTIES = frozenset(
    {
        "background",
        "background-color",
        "border",
        "border-bottom",
        "border-color",
        "border-left",
        "border-right",
        "border-top",
        "box-shadow",
        "color",
        "fill",
        "outline",
        "stroke",
        "text-shadow",
    }
)

_GEOMETRY_PROPERTIES = frozenset(
    {
        "animation",
        "animation-delay",
        "animation-duration",
        "backdrop-filter",
        "background-position",
        "background-size",
        "block-size",
        "border",
        "border-bottom",
        "border-left",
        "border-right",
        "border-top",
        "border-width",
        "bottom",
        "box-shadow",
        "column-gap",
        "filter",
        "flex-basis",
        "gap",
        "grid-auto-columns",
        "grid-auto-rows",
        "grid-template-columns",
        "grid-template-rows",
        "height",
        "inline-size",
        "inset",
        "inset-block",
        "inset-inline",
        "left",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-height",
        "max-width",
        "min-height",
        "min-width",
        "outline-width",
        "outline-offset",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "right",
        "row-gap",
        "text-shadow",
        "top",
        "transform",
        "transform-origin",
        "transition",
        "transition-delay",
        "transition-duration",
        "width",
    }
)
_RAW_GEOMETRY_VALUE = re.compile(
    r"(?<![-\w])(?:[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem|em|ch|ex|vw|vh|vmin|vmax|fr|deg|turn|ms|s|%)?)(?![-\w])",
    re.IGNORECASE,
)

_SKIP_TOKEN_TYPES = (
    _token.NL,
    _token.INDENT,
    _token.DEDENT,
    _token.COMMENT,
    _token.ENCODING,
    _token.ENDMARKER,
)


@dataclass(frozen=True)
class _CssRule:
    selectors: tuple[str, ...]
    declarations: tuple[tuple[str, str], ...]
    context: tuple[str, ...] = ()
    base_selectors: tuple[str | None, ...] = ()


@dataclass
class _HtmlFrame:
    tag: str
    classes: tuple[str, ...]
    dynamic: bool
    within_container: bool
    within_ambiguous_container: bool
    heading_allowed: bool


@dataclass(frozen=True)
class SurfaceScanEvidence:
    """One surface's violations plus structural evidence the scanner cannot prove."""

    findings: tuple[tuple[str, tuple[str, ...]], ...]
    unverifiable_markup: tuple[str, ...]

    def violations(self) -> dict[str, list[str]]:
        return {dimension: list(values) for dimension, values in self.findings}


@dataclass(frozen=True)
class DiscoveredEmitter:
    """One independently discovered visual-emitter candidate."""

    path: str
    adapter_kinds: frozenset[str]
    evidence_modes: frozenset[str]
    evidence: tuple[str, ...]


class _ExtractedSurfaceText(str):
    """Semantic text plus the legacy token view used by the original guards."""

    legacy_text: str
    source_text: str
    composition_limited: bool

    def __new__(
        cls,
        value: str,
        legacy_text: str,
        *,
        source_text: str = "",
        composition_limited: bool = False,
    ) -> _ExtractedSurfaceText:
        instance = super().__new__(cls, value)
        instance.legacy_text = legacy_text
        instance.source_text = source_text
        instance.composition_limited = composition_limited
        return instance


def css_text(path: Path) -> str:
    """Return CSS/HTML-bearing string tokens from a Python surface.

    Comments and bare-statement docstrings are excluded, preserving the exact
    behavior of the original pytest-local extractor. Malformed in-progress
    Python yields the complete token prefix available before ``TokenError``.
    """
    with tokenize.open(path) as source:
        text = source.read()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return _ExtractedSurfaceText(
            _css_text_from_token_prefix(path), _legacy_css_text(path), source_text=text
        )

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    candidates = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call))
        ),
        key=lambda node: (
            node.lineno,
            node.col_offset,
            -(node.end_lineno or node.lineno),
            -(node.end_col_offset or node.col_offset),
        ),
    )
    semantic_parts: list[str] = []
    semantic_size = 0
    composition_limited = False
    suppressed: set[ast.AST] = set()
    limit_token = _COMPOSITION_LIMIT_HIT.set(False)
    try:
        for node in candidates:
            if node in suppressed:
                continue
            value = _string_expression_value(node)
            if _COMPOSITION_LIMIT_HIT.get():
                composition_limited = True
                break
            if value is None:
                continue
            suppressed.update(ast.walk(node))
            suppressed.discard(node)
            if isinstance(parents.get(node), ast.Expr):
                continue
            added_size = len(value) + (len(_STRING_TOKEN_BOUNDARY) if semantic_parts else 0)
            if semantic_size + added_size > _MAX_STATIC_COMPOSITION:
                composition_limited = True
                break
            semantic_parts.append(value)
            semantic_size += added_size
    finally:
        _COMPOSITION_LIMIT_HIT.reset(limit_token)
    semantic_text = _STRING_TOKEN_BOUNDARY.join(semantic_parts)
    return _ExtractedSurfaceText(
        semantic_text,
        _legacy_css_text(path),
        source_text=text,
        composition_limited=composition_limited,
    )


def _mark_composition_limit() -> None:
    _COMPOSITION_LIMIT_HIT.set(True)


def _bounded_join(parts: tuple[str, ...], separator: str = "") -> str | None:
    size = sum(len(part) for part in parts)
    if parts:
        size += len(separator) * (len(parts) - 1)
    if size > _MAX_STATIC_COMPOSITION:
        _mark_composition_limit()
        return None
    return separator.join(parts)


def _literal_value(node: ast.AST) -> object:
    string_value = _string_expression_value(node)
    if string_value is not None:
        return string_value
    if isinstance(node, (ast.Tuple, ast.List)):
        values: list[object] = []
        size = 0
        for item in node.elts:
            value = _literal_value(item)
            if value is _UNSUPPORTED_LITERAL:
                return _UNSUPPORTED_LITERAL
            size += _literal_expansion_cost(value)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return _UNSUPPORTED_LITERAL
            values.append(value)
        return tuple(values) if isinstance(node, ast.Tuple) else values
    if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
        pairs: list[tuple[object, object]] = []
        size = 0
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                return _UNSUPPORTED_LITERAL
            key = _literal_value(key_node)
            value = _literal_value(value_node)
            if key is _UNSUPPORTED_LITERAL or value is _UNSUPPORTED_LITERAL:
                return _UNSUPPORTED_LITERAL
            size += _literal_expansion_cost(key) + _literal_expansion_cost(value)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return _UNSUPPORTED_LITERAL
            pairs.append((key, value))
        try:
            return dict(pairs)
        except TypeError:
            return _UNSUPPORTED_LITERAL
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return _UNSUPPORTED_LITERAL


_PERCENT_DIRECTIVE = re.compile(
    r"%(?:\((?P<key>[^)]+)\))?[-+#0 ]*(?P<width>\d+|\*)?"
    r"(?:\.(?P<precision>\d+|\*))?[hlL]?(?P<conversion>[diouxXeEfFgGcrsa%])"
)


def _percent_widths_are_bounded(template: str) -> bool:
    bounded = (
        sum(
            int(value)
            for directive in _PERCENT_DIRECTIVE.finditer(template)
            for value in (directive.group("width"), directive.group("precision"))
            if value is not None and value != "*"
        )
        <= _MAX_STATIC_COMPOSITION
    )
    if not bounded:
        _mark_composition_limit()
    return bounded


def _resolved_format_spec_is_bounded(format_spec: str) -> bool:
    bounded = (
        sum(int(digits) for digits in re.findall(r"\d+", format_spec)) <= _MAX_STATIC_COMPOSITION
    )
    if not bounded:
        _mark_composition_limit()
    return bounded


def _literal_expansion_cost(value: object) -> int:
    if isinstance(value, bool | int | float | complex | type(None)):
        return len(str(value))
    if isinstance(value, str | bytes):
        return len(value)
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return sum(
            _literal_expansion_cost(key) + _literal_expansion_cost(item)
            for key, item in mapping.items()
        )
    if isinstance(value, tuple | list | set | frozenset):
        sequence = cast(tuple[object, ...] | list[object] | set[object] | frozenset[object], value)
        return sum(_literal_expansion_cost(item) for item in sequence)
    return 0


def _literal_is_bounded(value: object) -> bool:
    bounded = _literal_expansion_cost(value) <= _MAX_STATIC_COMPOSITION
    if not bounded:
        _mark_composition_limit()
    return bounded


def _dynamic_format_template(template: str) -> str:
    try:
        return "".join(
            literal + ("{dynamic}" if field_name is not None else "")
            for literal, field_name, _format_spec, _conversion in Formatter().parse(template)
        )
    except ValueError:
        return template


def _render_brace_format_bounded(
    template: str,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    *,
    depth: int = 0,
    auto_state: list[int] | None = None,
) -> str | None:
    if depth > 20:
        _mark_composition_limit()
        return None
    formatter = Formatter()
    parts: list[str] = []
    size = 0
    if auto_state is None:
        auto_state = [0]
    try:
        parsed = formatter.parse(template)
        for literal, field_name, format_spec, conversion in parsed:
            size += len(literal)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return None
            parts.append(literal)
            if field_name is None:
                continue
            if field_name == "":
                field_name = str(auto_state[0])
                auto_state[0] += 1
            field_value, _used_key = formatter.get_field(field_name, args, kwargs)
            converted = (
                formatter.convert_field(field_value, conversion) if conversion else field_value
            )
            resolved_spec = _render_brace_format_bounded(
                format_spec or "",
                args,
                kwargs,
                depth=depth + 1,
                auto_state=auto_state,
            )
            if resolved_spec is None or not _resolved_format_spec_is_bounded(resolved_spec):
                return None
            rendered = formatter.format_field(converted, resolved_spec)
            size += len(rendered)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return None
            parts.append(rendered)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    return "".join(parts)


def _render_percent_bounded(template: str, argument: object) -> str | None:
    parts: list[str] = []
    size = 0
    cursor = 0
    positional = cast(tuple[object, ...], argument) if isinstance(argument, tuple) else ()
    positional_index = 0
    single_used = False
    mapping = cast(Mapping[object, object], argument) if isinstance(argument, Mapping) else None
    try:
        for directive in _PERCENT_DIRECTIVE.finditer(template):
            literal = template[cursor : directive.start()]
            size += len(literal)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return None
            parts.append(literal)
            cursor = directive.end()
            if directive.group("conversion") == "%":
                parts.append("%")
                size += 1
                continue
            if not _percent_widths_are_bounded(directive.group()):
                return None
            key = directive.group("key")
            if key is not None:
                if mapping is None or key not in mapping:
                    return None
                operand: object = {key: mapping[key]}
            else:
                consumed: list[object] = []
                for group_name in ("width", "precision"):
                    if directive.group(group_name) == "*":
                        if positional_index >= len(positional):
                            return None
                        width = positional[positional_index]
                        positional_index += 1
                        if not isinstance(width, int) or abs(width) > _MAX_STATIC_COMPOSITION:
                            if isinstance(width, int):
                                _mark_composition_limit()
                            return None
                        consumed.append(width)
                if positional:
                    if positional_index >= len(positional):
                        return None
                    consumed.append(positional[positional_index])
                    positional_index += 1
                elif single_used:
                    return None
                else:
                    consumed.append(cast(object, argument))
                    single_used = True
                operand = consumed[0] if len(consumed) == 1 else tuple(consumed)
            rendered = directive.group() % operand
            size += len(rendered)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return None
            parts.append(rendered)
    except (KeyError, TypeError, ValueError):
        return None
    trailing = template[cursor:]
    if positional and positional_index != len(positional):
        return None
    size += len(trailing)
    if size > _MAX_STATIC_COMPOSITION:
        _mark_composition_limit()
        return None
    parts.append(trailing)
    return "".join(parts)


def _string_expression_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_expression_value(node.left)
        right = _string_expression_value(node.right)
        if left is None and right is None:
            return None
        return _bounded_join((left or "{dynamic}", right or "{dynamic}"))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _string_expression_value(node.left)
        if template is None:
            return None
        argument = _literal_value(node.right)
        if argument is not _UNSUPPORTED_LITERAL and _literal_is_bounded(argument):
            rendered = _render_percent_bounded(template, argument)
            if rendered is not None:
                return rendered
        return _bounded_join(
            (
                _PERCENT_DIRECTIVE.sub(
                    lambda match: "%" if match.group("conversion") == "%" else "{dynamic}",
                    template,
                ),
            )
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _string_expression_value(node.left)
        right = _string_expression_value(node.right)
        count_node = node.right if left is not None else node.left
        count = _literal_value(count_node)
        value = left if left is not None else right
        if value is not None and isinstance(count, int):
            integer_count = int(count)
            if integer_count <= 0:
                return ""
            if len(value) <= _MAX_STATIC_COMPOSITION // integer_count:
                return value * integer_count
            _mark_composition_limit()
        return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        size = 0
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                part = value.value
            elif isinstance(value, ast.FormattedValue):
                literal = _literal_value(value.value)
                if literal is _UNSUPPORTED_LITERAL:
                    part = "{" + ast.unparse(value.value) + "}"
                else:
                    if value.conversion == ord("r"):
                        converted: object = repr(literal)
                    elif value.conversion == ord("a"):
                        converted = ascii(literal)
                    elif value.conversion == ord("s"):
                        converted = str(literal)
                    else:
                        converted = literal
                    format_spec = (
                        _string_expression_value(value.format_spec)
                        if value.format_spec is not None
                        else ""
                    )
                    if format_spec is None or not _resolved_format_spec_is_bounded(format_spec):
                        part = "{" + ast.unparse(value.value) + "}"
                    else:
                        try:
                            rendered = format(converted, format_spec)
                        except (TypeError, ValueError):
                            part = "{" + ast.unparse(value.value) + "}"
                        else:
                            if len(rendered) > _MAX_STATIC_COMPOSITION:
                                _mark_composition_limit()
                                return None
                            part = rendered
            else:
                return None
            size += len(part)
            if size > _MAX_STATIC_COMPOSITION:
                _mark_composition_limit()
                return None
            parts.append(part)
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = _string_expression_value(node.func.value)
        if receiver is None:
            return None
        if node.func.attr == "format":
            args_list: list[object] = []
            unsupported_expansion = False
            for argument in node.args:
                if isinstance(argument, ast.Starred):
                    expanded = _literal_value(argument.value)
                    if not isinstance(expanded, tuple | list):
                        unsupported_expansion = True
                    else:
                        args_list.extend(cast(tuple[object, ...] | list[object], expanded))
                else:
                    args_list.append(_literal_value(argument))
            args = tuple(args_list)
            kwargs: dict[str, object] = {}
            for keyword in node.keywords:
                value = _literal_value(keyword.value)
                if keyword.arg is None:
                    if isinstance(value, dict):
                        expanded_mapping = cast(dict[object, object], value)
                        if all(isinstance(key, str) for key in expanded_mapping):
                            kwargs.update(cast(dict[str, object], value))
                            continue
                    unsupported_expansion = True
                else:
                    kwargs[keyword.arg] = value
            format_values = (
                *args,
                *kwargs.values(),
                *([_UNSUPPORTED_LITERAL] if unsupported_expansion else []),
            )
            if all(
                value is not _UNSUPPORTED_LITERAL for value in format_values
            ) and _literal_is_bounded(format_values):
                rendered = _render_brace_format_bounded(receiver, args, kwargs)
                if rendered is not None:
                    return rendered
            return _bounded_join((_dynamic_format_template(receiver),))
        if node.func.attr == "format_map" and len(node.args) == 1:
            mapping = _literal_value(node.args[0])
            if isinstance(mapping, dict):
                object_mapping = cast(dict[object, object], mapping)
                typed_mapping = cast(dict[str, object], mapping)
            else:
                object_mapping = {}
                typed_mapping = {}
            if _literal_is_bounded(object_mapping):
                rendered = _render_brace_format_bounded(receiver, (), typed_mapping)
                if rendered is not None:
                    return rendered
            return _bounded_join((_dynamic_format_template(receiver),))
        if node.func.attr == "join" and len(node.args) == 1:
            values_node = node.args[0]
            if isinstance(values_node, (ast.List, ast.Tuple)):
                join_values: list[str] = []
                size = 0
                for item in values_node.elts:
                    value = _string_expression_value(item)
                    if value is None:
                        return None
                    size += len(value) + (len(receiver) if join_values else 0)
                    if size > _MAX_STATIC_COMPOSITION:
                        _mark_composition_limit()
                        return None
                    join_values.append(value)
                return receiver.join(join_values)
    return None


def _legacy_css_text(path: Path) -> str:
    """Return the exact raw-token projection owned by the original eight guards."""
    out: list[str] = []
    depth = 0
    line_start = True
    with path.open("rb") as source:
        try:
            for tok in tokenize.tokenize(source.readline):
                token_type, token_text = tok.type, tok.string
                if token_type == _token.OP:
                    if token_text in "([{":
                        depth += 1
                    elif token_text in ")]}":
                        depth = max(0, depth - 1)
                if token_type == _token.NEWLINE:
                    line_start = True
                    continue
                if token_type in _SKIP_TOKEN_TYPES:
                    continue
                is_fstring = tokenize.tok_name.get(token_type, "").startswith("FSTRING")
                if token_type == _token.STRING and line_start and depth == 0:
                    line_start = False
                    continue
                if token_type == _token.STRING or is_fstring:
                    out.append(token_text)
                line_start = False
        except tokenize.TokenError:
            pass
    return "\n".join(out)


def _css_text_from_token_prefix(path: Path) -> str:
    """Best-effort extraction for a malformed, in-progress Python module."""
    out: list[str] = []
    depth = 0
    line_start = True
    adjacent_string = False
    fstring_parts: list[str] | None = None
    with path.open("rb") as source:
        try:
            for tok in tokenize.tokenize(source.readline):
                token_type, token_text = tok.type, tok.string
                token_name = tokenize.tok_name.get(token_type, "")
                if token_name == "FSTRING_START":
                    fstring_parts = []
                    continue
                if fstring_parts is not None:
                    if token_name == "FSTRING_END":
                        piece = "".join(fstring_parts)
                        if adjacent_string and out:
                            out[-1] += piece
                        else:
                            out.append(piece)
                        adjacent_string = True
                        fstring_parts = None
                    elif token_name == "FSTRING_MIDDLE" or token_type not in (
                        _token.NL,
                        _token.ENCODING,
                    ):
                        fstring_parts.append(token_text)
                    continue
                if token_type == _token.OP:
                    if token_text in "([{":
                        depth += 1
                    elif token_text in ")]}":
                        depth = max(0, depth - 1)
                if token_type == _token.NEWLINE:
                    line_start = True
                    adjacent_string = False
                    continue
                if token_type in _SKIP_TOKEN_TYPES:
                    continue
                if token_type == _token.STRING and line_start and depth == 0:
                    line_start = False
                    adjacent_string = False
                    continue
                if token_type == _token.STRING:
                    token_text = _unwrap_extracted_string_token(token_text)
                    if adjacent_string and out:
                        out[-1] += token_text
                    else:
                        out.append(token_text)
                    adjacent_string = True
                elif token_type == _token.OP and token_text == "+" and adjacent_string:
                    pass
                else:
                    adjacent_string = False
                line_start = False
        except tokenize.TokenError:
            if fstring_parts:
                out.append("".join(fstring_parts))
    return _STRING_TOKEN_BOUNDARY.join(out)


def discover_surfaces(source_root: Path) -> frozenset[str]:
    """Discover every Python module under ``source_root`` that emits CSS."""
    return frozenset(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if "var(--" in path.read_text(encoding="utf-8")
    )


def _emitter_relative_path(project_root: Path, path: Path) -> str:
    src_root = project_root / "src"
    if path.is_relative_to(src_root):
        return path.relative_to(src_root).as_posix()
    return path.relative_to(project_root).as_posix()


def _normalize_runtime_js_syntax(text: str) -> str:
    """Canonicalize equivalent JS property and optional-call spellings."""

    normalized = re.sub(r"\?\.\s*\[['\"]([A-Za-z_$][\w$]*)['\"]\]", r".\1", text)
    normalized = re.sub(r"\?\.\s*(\[[^\]\r\n]+\])", r"\1", normalized)
    normalized = re.sub(r"\[['\"]([A-Za-z_$][\w$]*)['\"]\]", r".\1", normalized)
    normalized = re.sub(r"\?\.\s*\(", "(", normalized)
    return re.sub(r"\?\.", ".", normalized)


def _emitter_adapters(text: str, *, suffix: str) -> tuple[frozenset[str], tuple[str, ...]]:
    adapters: set[str] = set()
    evidence: list[str] = []
    if _HTML_EMITTER.search(text):
        adapters.add("html")
        evidence.append("html-markup")
    if _SVG_EMITTER.search(text) or suffix == ".svg":
        adapters.add("svg")
        evidence.append("svg-markup")
    runtime_text = _normalize_runtime_js_syntax(text)
    if (
        _RUNTIME_DOM_EMITTER.search(runtime_text)
        or _INDIRECT_VISUAL_INVOCATION.search(runtime_text)
        or _VISUAL_MUTATOR_ALIAS.search(runtime_text)
        or _RUNTIME_HTML_SINK.search(runtime_text)
        or _RUNTIME_STYLE_ELEMENT_SINK.search(runtime_text)
        or _RUNTIME_STYLESHEET_COLLECTION_SINK.search(runtime_text)
    ):
        adapters.add("runtime-js")
        evidence.append("runtime-visual-mutation")
    if _CSS_EMITTER.search(strip_css_comments(text)) or suffix == ".css":
        adapters.add("python-css")
        evidence.append("css-declaration")
    return frozenset(adapters), tuple(sorted(set(evidence)))


def discover_emitters(project_root: Path) -> tuple[DiscoveredEmitter, ...]:
    """Independently census shipped source files that emit visual evidence.

    Discovery is extension-aware and reads executable Python string payloads;
    it deliberately does not depend on the registry or on a ``var(--...)``
    marker.  Reconciliation with the manifest belongs to the verifier.
    """

    candidates: set[Path] = set()
    for root in (project_root / "src", project_root / "execution"):
        if root.exists():
            candidates.update(root.rglob("*.py"))
    design_root = project_root / "design-system" / "src"
    if design_root.exists():
        for suffix in ("*.css", "*.html", "*.js", "*.mjs", "*.ts", "*.tsx", "*.svg"):
            candidates.update(design_root.rglob(suffix))

    found: list[DiscoveredEmitter] = []
    for path in sorted(candidates):
        suffix = path.suffix.lower()
        try:
            raw_source = path.read_text("utf-8")
            payload = str(css_text(path)) if suffix == ".py" else raw_source
        except (OSError, UnicodeError, SyntaxError):
            continue
        adapters, evidence = _emitter_adapters(payload, suffix=suffix)
        adapter_set = set(adapters)
        evidence_set = set(evidence)
        if suffix == ".py" and (
            _RUNTIME_DOM_EMITTER.search(_normalize_runtime_js_syntax(raw_source))
            or _INDIRECT_VISUAL_INVOCATION.search(_normalize_runtime_js_syntax(raw_source))
            or _VISUAL_MUTATOR_ALIAS.search(_normalize_runtime_js_syntax(raw_source))
            or _RUNTIME_HTML_SINK.search(_normalize_runtime_js_syntax(raw_source))
            or _RUNTIME_STYLE_ELEMENT_SINK.search(_normalize_runtime_js_syntax(raw_source))
            or _RUNTIME_STYLESHEET_COLLECTION_SINK.search(_normalize_runtime_js_syntax(raw_source))
        ):
            adapter_set.add("runtime-js")
            evidence_set.add("runtime-visual-mutation")
        if suffix == ".py" and (
            _contains_dynamic_tag(raw_source) or _contains_dynamic_html_emitter(raw_source)
        ):
            adapter_set.add("html")
            evidence_set.add("dynamic-html-markup")
        if suffix == ".py" and _contains_dynamic_css(raw_source):
            adapter_set.add("python-css")
            evidence_set.add("dynamic-css-markup")
        if suffix == ".py":
            opaque_kinds = _opaque_visual_composition_kinds(raw_source)
            if "dynamic-html-markup" in opaque_kinds:
                adapter_set.add("html")
                evidence_set.add("dynamic-html-markup")
            if "dynamic-visual-value" in opaque_kinds:
                adapter_set.add("python-css")
                evidence_set.add("dynamic-css-markup")
        adapters = frozenset(adapter_set)
        evidence = tuple(sorted(evidence_set))
        if not adapters:
            continue
        found.append(
            DiscoveredEmitter(
                path=_emitter_relative_path(project_root, path),
                adapter_kinds=adapters,
                evidence_modes=frozenset({"static"}),
                evidence=evidence,
            )
        )
    return tuple(sorted(found, key=lambda entry: entry.path))


def _normalize_css_fragment(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip())
    normalized = re.sub(r"\s*([(:,>+~])\s*", r"\1", normalized)
    normalized = re.sub(r"\s*\)", ")", normalized)
    return re.sub(r"\s+([+-])\s+", r"\1", normalized)


def _geometry_identities(rel: str, text: str, *, raw_numeric_only: bool = True) -> tuple[str, ...]:
    """Return normalized, multiplicity-preserving geometry recipe identities."""

    identities: list[str] = []
    for rule in _surface_css_rules(text):
        context = "/".join(_normalize_css_fragment(item) for item in rule.context) or "root"
        for selector in rule.selectors:
            normalized_selector = _normalize_css_fragment(selector)
            for property_name, value in rule.declarations:
                lowered = property_name.strip().lower()
                normalized_value = _normalize_css_fragment(value)
                if lowered not in _GEOMETRY_PROPERTIES:
                    continue
                if _DYNAMIC_VALUE_PLACEHOLDER.search(normalized_value):
                    continue
                if raw_numeric_only and not _RAW_GEOMETRY_VALUE.search(normalized_value):
                    continue
                identities.append(
                    "|".join((rel, context, normalized_selector, lowered, normalized_value))
                )
    counts = Counter(identities)
    return tuple(
        f"{identity}|#{ordinal}"
        for identity in sorted(counts)
        for ordinal in range(1, counts[identity] + 1)
    )


def master_geometry_digest(rel: str, text: str) -> str:
    """Return the compact contract digest for one master geometry recipe."""

    payload = "\n".join(_geometry_identities(rel, text, raw_numeric_only=False)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _master_geometry_contract_findings(rel: str, text: str) -> list[str]:
    if rel not in MASTER_SOURCES:
        return []
    contracts = [item for item in MASTER_GEOMETRY_CONTRACTS if item.surface == rel]
    if len(contracts) != 1:
        return [f"contract-count={len(contracts)}"]
    observed = master_geometry_digest(rel, text)
    expected = contracts[0].digest
    return [] if observed == expected else [f"digest={observed};expected={expected}"]


def geometry_debt_fingerprints(rel: str, text: str) -> tuple[str, ...]:
    """Return formatting-stable, multiplicity-preserving raw-geometry debt."""

    if rel in EXEMPT or rel in MASTER_SOURCES:
        return ()
    return _geometry_identities(rel, text)


def geometry_debt_failures(observed: Iterable[str], baseline: Iterable[str]) -> list[str]:
    """Require an exact checked-in debt ledger, including deliberate shrink."""

    observed_set = set(observed)
    baseline_set = set(baseline)
    failures = [f"new geometry debt: {item}" for item in sorted(observed_set - baseline_set)]
    failures.extend(
        f"stale geometry debt baseline: {item}" for item in sorted(baseline_set - observed_set)
    )
    return failures


def finding_debt_id(surface: str, dimension: str, value: str) -> str:
    """Return the stable ledger identity for one deterministic finding value."""

    return f"{surface}|{dimension}|{value}"


def unverifiable_debt_id(surface: str, value: str) -> str:
    """Return the stable ledger identity for one bounded static uncertainty."""

    return f"{surface}|{value}"


def split_top_commas(value: str) -> list[str]:
    """Split a CSS value on top-level commas, retaining commas in functions."""
    return _split_top_level(value, ",")


def strip_css_comments(text: str) -> str:
    """Remove CSS block comments using the scanner's canonical rule."""
    return _CSS_COMMENT.sub("", text)


def _split_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    escaped = False
    current: list[str] = []
    for character in value:
        if quote:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            current.append(character)
        elif character == "(":
            depth += 1
            current.append(character)
        elif character == ")":
            depth = max(0, depth - 1)
            current.append(character)
        elif character == delimiter and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _matching_brace(source: str, opening: int) -> int | None:
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _html_tag_end(text: str, start: int) -> int | None:
    quote = ""
    escaped = False
    for cursor in range(start, len(text)):
        character = text[cursor]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == ">":
            return cursor
        elif character == "<":
            return None
    return None


def _contains_dynamic_tag(text: str) -> bool:
    cursor = 0
    raw_tag = ""
    while cursor < len(text):
        if raw_tag:
            closing = re.search(rf"</\s*{re.escape(raw_tag)}\b", text[cursor:], re.IGNORECASE)
            if closing is None:
                return False
            opening = cursor + closing.start()
            end = _html_tag_end(text, opening + 2)
            if end is None:
                return False
            cursor = end + 1
            raw_tag = ""
            continue

        opening = text.find("<", cursor)
        if opening < 0:
            return False
        if text.startswith("<!--", opening):
            comment_end = text.find("-->", opening + 4)
            if comment_end < 0:
                return False
            cursor = comment_end + 3
            continue
        name_start = opening + 1
        is_closing = name_start < len(text) and text[name_start] == "/"
        if is_closing:
            name_start += 1
        while name_start < len(text) and text[name_start].isspace():
            name_start += 1
        dynamic_prefix = text[opening:name_start]
        previous = text[opening - 1] if opening else ""
        if (
            name_start < len(text)
            and text[name_start] == "{"
            and dynamic_prefix in {"<", "</"}
            and (not previous or previous in "\"'\n\r=+(,[{:")
        ):
            expression_end = _matching_brace(text, name_start)
            if expression_end is not None:
                following = text[expression_end + 1 : expression_end + 2]
                if (
                    following in {"", ">", "/", " ", "\t", "\r", "\n"}
                    and _html_tag_end(text, expression_end + 1) is not None
                ):
                    return True
        name = re.match(r"[A-Za-z][\w:-]*", text[name_start:])
        end = _html_tag_end(text, name_start)
        if end is None:
            cursor = opening + 1
            continue
        if name is not None and not is_closing:
            tag_name = name.group().lower()
            if tag_name in {
                "iframe",
                "noembed",
                "noframes",
                "plaintext",
                "script",
                "style",
                "textarea",
                "title",
                "xmp",
            } and not text[opening:end].rstrip().endswith("/"):
                raw_tag = tag_name
        cursor = end + 1
    return False


def _contains_dynamic_css(text: str) -> bool:
    """Return whether a Python expression can construct visual CSS dynamically."""

    return any(not item.startswith("opaque-html:") for item in _dynamic_visual_skeletons(text))


def _dynamic_string_skeleton(node: ast.AST) -> tuple[str, bool] | None:
    """Reduce bounded Python string composition to a conservative recipe."""

    marker = "{dynamic}"
    if isinstance(node, ast.Constant):
        return (node.value, False) if isinstance(node.value, str) else None
    if isinstance(node, ast.FormattedValue):
        return marker, True
    if isinstance(node, ast.JoinedStr):
        parts = [_dynamic_string_skeleton(part) for part in node.values]
        if any(part is None for part in parts):
            return None
        concrete = [part for part in parts if part is not None]
        return "".join(part[0] for part in concrete), any(part[1] for part in concrete)
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return marker, True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _dynamic_string_skeleton(node.left)
        right = _dynamic_string_skeleton(node.right)
        if left is None or right is None:
            return None
        return left[0] + right[0], left[1] or right[1]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _dynamic_string_skeleton(node.left)
        if template is None:
            return None
        protected = template[0].replace("%%", "\x00percent\x00")
        rendered, count = re.subn(r"%[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[A-Za-z]", marker, protected)
        return rendered.replace("\x00percent\x00", "%"), template[1] or count > 0
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            template = _dynamic_string_skeleton(node.func.value)
            if template is None:
                return None
            protected = template[0].replace("{{", "\x00open\x00").replace("}}", "\x00close\x00")
            rendered, count = re.subn(r"\{[^{}]*\}", marker, protected)
            return (
                rendered.replace("\x00open\x00", "{").replace("\x00close\x00", "}"),
                template[1] or count > 0,
            )
        if node.func.attr == "join" and len(node.args) == 1:
            separator = _dynamic_string_skeleton(node.func.value)
            values = node.args[0]
            if separator is None or not isinstance(values, (ast.List, ast.Tuple)):
                return None
            parts = [_dynamic_string_skeleton(item) for item in values.elts]
            if any(part is None for part in parts):
                return None
            concrete = [part for part in parts if part is not None]
            return (
                separator[0].join(part[0] for part in concrete),
                separator[1] or any(part[1] for part in concrete),
            )
    if isinstance(node, ast.Call):
        return marker, True
    return None


def _dynamic_string_recipes(tree: ast.AST) -> tuple[tuple[str, bool], ...]:
    """Return only maximal bounded string expressions, never nested fragments."""

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    recipes: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Call)):
            continue
        parent = parents.get(node)
        nested = isinstance(parent, (ast.JoinedStr, ast.BinOp)) or (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr in {"format", "join"}
        )
        if nested:
            continue
        recipe = _dynamic_string_skeleton(node)
        if recipe is not None and recipe[1]:
            recipes.append(recipe)
    return tuple(recipes)


def _dynamic_html_skeletons(text: str) -> tuple[str, ...]:
    """Return bounded recipes whose tag identity is decided at runtime."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    skeletons: set[str] = set()
    for rendered, _dynamic in _dynamic_string_recipes(tree):
        if re.search(r"</?\{dynamic\}(?:\s|/?>)", rendered, re.IGNORECASE):
            skeletons.add(_normalize_css_fragment(rendered))
    return tuple(sorted(skeletons))


def _contains_dynamic_html_emitter(text: str) -> bool:
    """Limit census evidence to explicit HTML variables and renderer returns."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    def is_dynamic_tag(node: ast.AST | None) -> bool:
        if node is None:
            return False
        recipe = _dynamic_string_skeleton(node)
        return bool(
            recipe
            and recipe[1]
            and re.search(r"</?\{dynamic\}(?:\s|/?>)", recipe[0], re.IGNORECASE)
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id.lower() for target in targets if isinstance(target, ast.Name)}
            if any(
                re.search(r"(?:html|markup|template)", name) for name in names
            ) and is_dynamic_tag(node.value):
                return True
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and re.search(r"(?:render|html|markup|emit)", node.name, re.IGNORECASE)
            and any(
                isinstance(child, ast.Return) and is_dynamic_tag(child.value)
                for child in ast.walk(node)
            )
        ):
            return True
    return False


def _opaque_visual_composition_skeletons(text: str) -> tuple[str, ...]:
    """Pin explicit visual variables that delegate to otherwise opaque code."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    skeletons: set[str] = set()
    if tree is not None:
        known_callables = (
            {
                alias.asname or alias.name.split(".")[-1]
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            | {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            | {
                argument.arg
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            }
        )
        for node in ast.walk(tree):
            targets: list[ast.expr]
            value: ast.expr | None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets, value = [node.target], node.value
            else:
                continue
            raw_names = {target.id for target in targets if isinstance(target, ast.Name)}
            names = {name.lower() for name in raw_names}
            if not isinstance(value, ast.Call):
                continue
            recipe = _dynamic_string_skeleton(value)
            supported = recipe is not None and recipe[0] != "{dynamic}"
            if supported:
                continue
            callable_name = (
                value.func.id
                if isinstance(value.func, ast.Name)
                else value.func.attr
                if isinstance(value.func, ast.Attribute)
                else ""
            )
            if callable_name in known_callables:
                continue
            opaque_join = isinstance(value.func, ast.Attribute) and value.func.attr == "join"
            visual_call = bool(
                re.search(
                    r"(?:render|html|markup|template|css|style)", callable_name, re.IGNORECASE
                )
            )
            explicit_contract = bool(raw_names & {"HTML", "CSS", "MARKUP", "STYLESHEET"})
            if not explicit_contract and not opaque_join and not visual_call:
                continue
            normalized_call = _normalize_css_fragment(ast.unparse(value))
            for name in sorted(names):
                if re.search(r"(?:html|markup|template)", name):
                    skeletons.add(f"opaque-html:{name}={normalized_call}")
                if re.search(r"(?:css|style|stylesheet)", name):
                    skeletons.add(f"opaque-css:{name}={normalized_call}")

    for match in re.finditer(
        r"\b(?:const|let|var)\s+(?P<name>html|markup|template|css|style|stylesheet)\s*=\s*(?P<call>[^;\r\n]+(?:\([^;\r\n]*\)|\.join\([^;\r\n]*\)))\s*;?",
        text,
        re.IGNORECASE,
    ):
        name = match.group("name").lower()
        call = _normalize_css_fragment(match.group("call"))
        prefix = "opaque-html" if name in {"html", "markup", "template"} else "opaque-css"
        skeletons.add(f"{prefix}:{name}={call}")
    return tuple(sorted(skeletons))


def _opaque_visual_composition_kinds(text: str) -> frozenset[str]:
    skeletons = _opaque_visual_composition_skeletons(text)
    kinds: set[str] = set()
    if any(item.startswith("opaque-html:") for item in skeletons):
        kinds.add("dynamic-html-markup")
    if any(item.startswith("opaque-css:") for item in skeletons):
        kinds.add("dynamic-visual-value")
    return frozenset(kinds)


_OBJECT_VISUAL_PROPERTIES = (
    _GEOMETRY_PROPERTIES
    | _COLOR_PROPERTIES
    | frozenset({"background", "border-radius", "font-family", "font-size", "font-weight"})
)


def _object_style_findings(rel: str, text: str) -> list[str]:
    """Reject Python/TypeScript style objects outside explicit masters."""

    findings: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                property_name = ""
                root_name = ""
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    root_name = target.value.id.lower()
                    property_name = target.attr
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    root_name = target.value.id.lower()
                    property_name = target.slice.value
                normalized_property = re.sub(
                    r"(?<!^)(?=[A-Z])", "-", property_name.replace("_", "-")
                ).lower()
                if re.search(r"(?:style|styles|sx|css|theme|layout)", root_name) and (
                    normalized_property in _OBJECT_VISUAL_PROPERTIES
                ):
                    findings.add(f"object-style:{normalized_property}")
            if node.value is None:
                continue
            for child in ast.walk(node.value):
                if not isinstance(child, ast.Dict):
                    continue
                for key in child.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        property_name = re.sub(
                            r"(?<!^)(?=[A-Z])", "-", key.value.replace("_", "-")
                        ).lower()
                        if property_name in _OBJECT_VISUAL_PROPERTIES:
                            findings.add(f"object-style:{property_name}")
    ts_object_bodies: list[str] = []
    if Path(rel).suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
        ts_object_bodies.extend(
            match.group("body")
            for match in re.finditer(
                r"\b(?:const|let|var)\s+(?:styles?|sx|css|theme|layout|props|[A-Za-z_]\w*(?:style|styles|css|theme)[A-Za-z_]\w*)\s*=\s*(?P<body>.{0,5000}?)\s*;",
                text,
                re.IGNORECASE | re.DOTALL,
            )
        )
        for match in re.finditer(
            r"\b(?:styles?|sx|css|theme|layout)\s*(?:\.\s*(?P<dot>[A-Za-z][\w-]*)|\[\s*['\"](?P<bracket>[A-Za-z][\w-]*)['\"]\s*\])\s*=",
            text,
            re.IGNORECASE,
        ):
            raw_property = match.group("dot") or match.group("bracket") or ""
            property_name = re.sub(r"(?<!^)(?=[A-Z])", "-", raw_property).lower()
            if property_name in _OBJECT_VISUAL_PROPERTIES:
                findings.add(f"object-style:{property_name}")
    for body in ts_object_bodies:
        for property_match in re.finditer(
            r"(?:^|[,{])\s*['\"]?(?P<property>[A-Za-z][\w-]*)['\"]?\s*:",
            body,
        ):
            property_name = re.sub(
                r"(?<!^)(?=[A-Z])", "-", property_match.group("property")
            ).lower()
            if property_name in _OBJECT_VISUAL_PROPERTIES:
                findings.add(f"object-style:{property_name}")
    return sorted(findings)


def _declarations(body: str) -> tuple[tuple[str, str], ...]:
    declarations: list[tuple[str, str]] = []
    for declaration in _split_top_level(body, ";"):
        property_name, separator, value = declaration.partition(":")
        property_name = property_name.strip().lower()
        if separator and re.fullmatch(r"--?[a-z][\w-]*|[a-z][\w-]*", property_name):
            declarations.append((property_name, value.strip()))
    return tuple(declarations)


def _next_unquoted_opening(source: str, start: int) -> int | None:
    quote = ""
    escaped = False
    for cursor in range(start, len(source)):
        character = source[cursor]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == "{":
            return cursor
    return None


def _last_style_wrapper_end(source: str) -> int | None:
    last_end: int | None = None
    cursor = 0
    quote = ""
    escaped = False
    brackets = 0
    while cursor < len(source):
        character = source[cursor]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            cursor += 1
            continue
        if character in "\"'":
            quote = character
            cursor += 1
            continue
        if character == "[":
            brackets += 1
            cursor += 1
            continue
        if character == "]":
            brackets = max(0, brackets - 1)
            cursor += 1
            continue
        if not brackets and source[cursor : cursor + 6].lower() == "<style":
            boundary = cursor + 6
            if boundary < len(source) and not (
                source[boundary].isspace() or source[boundary] == ">"
            ):
                cursor += 1
                continue
            closing: int | None = None
            tag_quote = ""
            tag_escaped = False
            for tag_cursor in range(boundary, len(source)):
                tag_character = source[tag_cursor]
                if tag_quote:
                    if tag_escaped:
                        tag_escaped = False
                    elif tag_character == "\\":
                        tag_escaped = True
                    elif tag_character == tag_quote:
                        tag_quote = ""
                elif tag_character in "\"'":
                    tag_quote = tag_character
                elif tag_character == ">":
                    closing = tag_cursor
                    break
            if closing is None:
                return last_end
            last_end = closing + 1
            cursor = closing + 1
            continue
        cursor += 1
    return last_end


def _walk_rule_body(
    body: str,
    context: tuple[str, ...],
    parent_selectors: tuple[str, ...],
    base_selectors: tuple[str | None, ...] = (),
) -> Iterator[_CssRule]:
    """Yield direct declaration segments and nested blocks in source order."""
    if base_selectors and any(base is not None for base in base_selectors):
        yield _CssRule(parent_selectors, (), context, base_selectors)
    cursor = 0
    while True:
        opening = _next_unquoted_opening(body, cursor)
        if opening is None:
            direct = _declarations(body[cursor:])
            if direct:
                yield _CssRule(parent_selectors, direct, context, base_selectors)
            return
        prefix = body[cursor:opening]
        last_terminator = prefix.rfind(";")
        nested_head = prefix.strip()
        if last_terminator >= 0:
            direct = _declarations(prefix[: last_terminator + 1])
            if direct:
                yield _CssRule(parent_selectors, direct, context, base_selectors)
            nested_head = prefix[last_terminator + 1 :].strip()
        closing = _matching_brace(body, opening)
        if closing is None:
            return
        nested_body = body[opening + 1 : closing]
        if nested_head.startswith("@"):
            nested_context = (*context, _normalize_value(nested_head))
            yield from _walk_rule_body(
                nested_body,
                nested_context,
                parent_selectors,
                base_selectors,
            )
        else:
            selectors = tuple(split_top_commas(nested_head))
            if any("&" in selector or _selector_atoms(selector) for selector in selectors):
                nested_selectors: list[str] = []
                nested_bases: list[str | None] = []
                for parent in parent_selectors:
                    parent_atoms = set(_subject_selector_atoms(parent))
                    for child in selectors:
                        expanded = (
                            child.replace("&", parent) if "&" in child else f"{parent} {child}"
                        )
                        expanded_atoms = set(_subject_selector_atoms(expanded))
                        preserves_subject = bool(
                            "&" in child
                            and parent_atoms
                            and parent_atoms & expanded_atoms
                            and re.sub(r"\s+", " ", expanded.strip())
                            != re.sub(r"\s+", " ", parent.strip())
                        )
                        nested_selectors.append(expanded)
                        nested_bases.append(parent if preserves_subject else None)
                yield from _walk_rule_body(
                    nested_body,
                    context,
                    tuple(nested_selectors),
                    tuple(nested_bases),
                )
        cursor = closing + 1


def _expand_nested_selectors(
    selectors: tuple[str, ...], parent_selectors: tuple[str, ...]
) -> tuple[str, ...]:
    if not parent_selectors:
        return selectors
    return tuple(
        child.replace("&", parent) if "&" in child else f"{parent} {child}"
        for parent in parent_selectors
        for child in selectors
    )


def _walk_css_blocks(
    source: str,
    context: tuple[str, ...] = (),
    parent_selectors: tuple[str, ...] = (),
) -> Iterator[_CssRule]:
    cursor = 0
    while cursor < len(source):
        opening = source.find("{", cursor)
        if opening < 0:
            return
        closing = _matching_brace(source, opening)
        if closing is None:
            return
        head = source[cursor:opening].strip()
        candidate_head = head.rsplit(_STRING_TOKEN_BOUNDARY, maxsplit=1)[-1].lstrip()
        wrapper = _PYTHON_STRING_WRAPPER.match(candidate_head)
        while wrapper is not None:
            candidate_head = candidate_head[wrapper.end() :].lstrip()
            wrapper = _PYTHON_STRING_WRAPPER.match(candidate_head)
        style_end = _last_style_wrapper_end(candidate_head)
        if style_end is not None:
            candidate_head = candidate_head[style_end:].lstrip()
        if not _CSS_SELECTOR_START.match(candidate_head):
            last_line = candidate_head.splitlines()[-1].strip() if candidate_head else ""
            if _CSS_SELECTOR_START.match(last_line):
                candidate_head = last_line
        head = candidate_head
        conditional_rules = tuple(_CONDITIONAL_AT_RULE.finditer(candidate_head))
        if not _CSS_SELECTOR_START.match(candidate_head) and conditional_rules:
            head = candidate_head[conditional_rules[-1].start() :]
        body = source[opening + 1 : closing]
        selectors = tuple(split_top_commas(head))
        has_selector_atom = not head.startswith("@") and (
            any(_selector_atoms(selector) for selector in selectors)
            or bool(_CSS_SELECTOR_START.match(head))
        )
        keyframe_step = any("keyframes" in item.lower() for item in context) and all(
            re.fullmatch(r"(?:from|to|\d+(?:\.\d+)?%)", selector.strip(), re.IGNORECASE)
            for selector in selectors
        )
        if has_selector_atom or keyframe_step:
            selectors = _expand_nested_selectors(selectors, parent_selectors)
            yield from _walk_rule_body(body, context, selectors)
        elif head.startswith("@"):
            nested_context = (*context, _normalize_value(head))
            if parent_selectors:
                yield from _walk_rule_body(body, nested_context, parent_selectors)
            else:
                yield from _walk_css_blocks(body, nested_context)
        cursor = closing + 1


def _unwrap_extracted_string_token(token_text: str) -> str:
    match = _PYTHON_STRING_LITERAL.match(token_text)
    if match is None:
        return token_text
    quote = match.group("quote")
    if not token_text.endswith(quote):
        return token_text
    return token_text[match.end() : -len(quote)]


def _css_rules(text: str) -> tuple[_CssRule, ...]:
    segments = (
        tuple(_unwrap_extracted_string_token(part) for part in text.split(_STRING_TOKEN_BOUNDARY))
        if _STRING_TOKEN_BOUNDARY in text
        else (text,)
    )
    rules: list[_CssRule] = []
    for segment in segments:
        clean = strip_css_comments(segment)
        rules.extend(_walk_css_blocks(clean))
        # Python ``str.format`` templates double both braces. Parse that
        # additional representation only when an escaped opening exists;
        # ordinary nested CSS often ends in adjacent ``}}`` and must never be
        # collapsed.
        if "{{" in clean:
            rendered = clean.replace("{{", "{").replace("}}", "}")
            rules.extend(_walk_css_blocks(rendered))
    return tuple(rules)


def _surface_css_rules(text: str) -> tuple[_CssRule, ...]:
    """Parse stylesheet rules plus literal HTML inline-style declarations."""

    inline = tuple(
        _CssRule(selectors=("[style]",), declarations=_declarations(match.group("value")))
        for match in _INLINE_STYLE_ATTRIBUTE.finditer(text)
    )
    return (*_css_rules(text), *inline)


def _dynamic_visual_skeletons(text: str) -> tuple[str, ...]:
    """Return normalized dynamic string recipes that contain visual values."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    marker = "{dynamic}"
    skeletons: set[str] = set()
    recipes = _dynamic_string_recipes(tree) if tree is not None else ()
    for rendered, _dynamic in recipes:
        dynamic_declaration = any(
            marker in value
            for rule in _surface_css_rules(rendered)
            for _property_name, value in rule.declarations
        )
        static_structure = rendered.replace(marker, "")
        dynamic_rule = (
            "{" in static_structure
            and "}" in static_structure
            and any(
                match.group("property").lower()
                in (_GEOMETRY_PROPERTIES | _COLOR_PROPERTIES | {"background", "border-radius"})
                or match.group("property").startswith("--")
                for match in re.finditer(
                    r"(?:^|[;{])\s*(?P<property>--[\w-]+|[a-z][\w-]*)\s*:"
                    r"[^;\r\n]*\{dynamic\}",
                    rendered,
                    re.IGNORECASE,
                )
            )
        )
        dynamic_style_attribute = any(
            marker in match.group("value") for match in _INLINE_STYLE_ATTRIBUTE.finditer(rendered)
        )
        fully_dynamic_rule = bool(
            re.search(
                r"\{dynamic\}[^{};]*\{\s*\{dynamic\}\s*:\s*\{dynamic\}[^{}]*;\s*}",
                rendered,
                re.DOTALL,
            )
        )
        dynamic_property_or_selector = bool(
            re.search(
                r"(?:[;{])\s*\{dynamic\}\s*:\s*[^;{}]+;"
                r"|^\s*\{dynamic\}[^{};]*\{\s*(?:--[\w-]+|[a-z][\w-]*)\s*:",
                rendered,
                re.IGNORECASE | re.DOTALL,
            )
        )
        selector_probe = rendered.replace(marker, "DYNAMIC_MARKER")
        dynamic_selector_fragment = bool(
            re.search(
                r"(?:^|[{}])\s*[^{}]*DYNAMIC_MARKER[^{}]*\{\s*(?:--[\w-]+|[a-z][\w-]*)\s*:"
                r"|@(?:media|supports|container|scope)\b[^{}]*DYNAMIC_MARKER[^{}]*\{",
                selector_probe,
                re.IGNORECASE | re.DOTALL,
            )
        )
        dynamic_style_payload = bool(
            re.search(r"<style\b[^>]*>\s*\{dynamic\}", rendered, re.IGNORECASE)
        )
        if (
            dynamic_declaration
            or dynamic_rule
            or dynamic_style_attribute
            or fully_dynamic_rule
            or dynamic_property_or_selector
            or dynamic_selector_fragment
            or dynamic_style_payload
        ):
            skeletons.add(_normalize_css_fragment(rendered))
    skeletons.update(_opaque_visual_composition_skeletons(text))
    return tuple(sorted(skeletons))


def dynamic_visual_digest(text: str) -> str:
    """Return the compact contract digest for dynamic visual source recipes."""

    return hashlib.sha256("\n".join(_dynamic_visual_skeletons(text)).encode("utf-8")).hexdigest()


def _dynamic_visual_contract_findings(rel: str, text: str) -> tuple[list[str], bool]:
    skeletons = _dynamic_visual_skeletons(text)
    if not skeletons:
        return [], False
    contracts = [item for item in DYNAMIC_VISUAL_CONTRACTS if item.surface == rel]
    if len(contracts) != 1:
        return [], True
    observed = dynamic_visual_digest(text)
    expected = contracts[0].digest
    if observed == expected:
        return [], False
    return [f"digest={observed};expected={expected}"], False


def _css_identifier(source: str, start: int) -> tuple[str, int]:
    output: list[str] = []
    cursor = start
    while cursor < len(source):
        character = source[cursor]
        if character == "\\" and cursor + 1 < len(source):
            cursor += 1
            hex_digits: list[str] = []
            while (
                cursor < len(source)
                and len(hex_digits) < 6
                and source[cursor] in "0123456789abcdefABCDEF"
            ):
                hex_digits.append(source[cursor])
                cursor += 1
            if hex_digits:
                output.append(chr(int("".join(hex_digits), 16)))
                if cursor < len(source) and source[cursor].isspace():
                    cursor += 1
            else:
                output.append(source[cursor])
                cursor += 1
            continue
        if character.isalnum() or character in "_-" or ord(character) >= 128:
            output.append(character)
            cursor += 1
            continue
        break
    return "".join(output), cursor


def _balanced_end(source: str, opening: int, left: str, right: str) -> int:
    depth = 0
    quote = ""
    escaped = False
    for cursor in range(opening, len(source)):
        character = source[cursor]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == left:
            depth += 1
        elif character == right:
            depth -= 1
            if depth == 0:
                return cursor + 1
    return len(source)


def _selector_atoms(selector: str) -> tuple[tuple[str, str], ...]:
    atoms: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(selector):
        character = selector[cursor]
        if character == "[":
            cursor = _balanced_end(selector, cursor, "[", "]")
            continue
        if character == ":":
            pseudo, after_name = _css_identifier(selector, cursor + 1)
            if after_name < len(selector) and selector[after_name] == "(":
                end = _balanced_end(selector, after_name, "(", ")")
                if pseudo.lower() in {"is", "where"}:
                    for branch in split_top_commas(selector[after_name + 1 : end - 1]):
                        atoms.extend(_subject_selector_atoms(branch))
                cursor = end
                continue
        if character in ".#":
            identifier, end = _css_identifier(selector, cursor + 1)
            if identifier:
                atoms.append((character, identifier))
                cursor = end
                continue
        cursor += 1
    return tuple(atoms)


def _subject_compound(selector: str) -> str:
    """Return the selector compound that owns the rule declarations."""
    start = 0
    parentheses = 0
    brackets = 0
    quote = ""
    escaped = False
    cursor = 0
    while cursor < len(selector):
        character = selector[cursor]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            cursor += 1
            continue
        if character in "\"'":
            quote = character
        elif character == "[":
            brackets += 1
        elif character == "]":
            brackets = max(0, brackets - 1)
        elif not brackets and character == "(":
            parentheses += 1
        elif not brackets and character == ")":
            parentheses = max(0, parentheses - 1)
        elif not brackets and not parentheses:
            if character in ">+~":
                start = cursor + 1
            elif character.isspace():
                lookahead = cursor
                while lookahead < len(selector) and selector[lookahead].isspace():
                    lookahead += 1
                if lookahead < len(selector) and selector[lookahead] not in ">+~":
                    start = lookahead
                cursor = lookahead - 1
        cursor += 1
    return selector[start:].strip()


def _subject_selector_atoms(selector: str) -> tuple[tuple[str, str], ...]:
    subject = _subject_compound(selector)
    if _has_structural_pseudo_element(subject):
        return ()
    return _selector_atoms(subject)


def _has_structural_pseudo_element(selector: str) -> bool:
    structural: list[str] = []
    cursor = 0
    while cursor < len(selector):
        character = selector[cursor]
        if character == "[":
            cursor = _balanced_end(selector, cursor, "[", "]")
            continue
        if character in "\"'":
            quote = character
            cursor += 1
            escaped = False
            while cursor < len(selector):
                current = selector[cursor]
                cursor += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    break
            continue
        structural.append(character)
        cursor += 1
    return bool(_PSEUDO_ELEMENT.search("".join(structural)))


def _selector_specificity(selector: str) -> tuple[int, int, int]:
    ids = classes = elements = 0
    cursor = 0
    while cursor < len(selector):
        character = selector[cursor]
        if character in "\"'":
            quote = character
            cursor += 1
            escaped = False
            while cursor < len(selector):
                current = selector[cursor]
                cursor += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    break
            continue
        if character == "[":
            classes += 1
            cursor = _balanced_end(selector, cursor, "[", "]")
            continue
        if character in ".#":
            _identifier, cursor = _css_identifier(selector, cursor + 1)
            if character == "#":
                ids += 1
            else:
                classes += 1
            continue
        if character == ":":
            if cursor + 1 < len(selector) and selector[cursor + 1] == ":":
                _identifier, cursor = _css_identifier(selector, cursor + 2)
                elements += 1
                continue
            pseudo, after_name = _css_identifier(selector, cursor + 1)
            if after_name < len(selector) and selector[after_name] == "(":
                end = _balanced_end(selector, after_name, "(", ")")
                lowered = pseudo.lower()
                if lowered in {"is", "not", "has"}:
                    branches = tuple(
                        _selector_specificity(branch)
                        for branch in split_top_commas(selector[after_name + 1 : end - 1])
                    )
                    if branches:
                        branch_ids, branch_classes, branch_elements = max(branches)
                        ids += branch_ids
                        classes += branch_classes
                        elements += branch_elements
                elif lowered in {"nth-child", "nth-last-child"}:
                    classes += 1
                    content = selector[after_name + 1 : end - 1]
                    of_match = re.search(r"\bof\b", content, re.IGNORECASE)
                    if of_match is not None:
                        branches = tuple(
                            _selector_specificity(branch)
                            for branch in split_top_commas(content[of_match.end() :])
                        )
                        if branches:
                            branch_ids, branch_classes, branch_elements = max(branches)
                            ids += branch_ids
                            classes += branch_classes
                            elements += branch_elements
                elif lowered != "where":
                    classes += 1
                cursor = end
                continue
            classes += 1
            cursor = after_name
            continue
        if character.isalpha() or character == "_" or character == "\\":
            _identifier, after_name = _css_identifier(selector, cursor)
            before = selector[:cursor].rstrip()
            if not before or before[-1] in ">+~,(|" or selector[cursor - 1].isspace():
                elements += 1
            cursor = after_name
            continue
        cursor += 1
    return ids, classes, elements


def _registry_token_names() -> set[str]:
    return (
        set(CHROME_TOKENS)
        | set(PALETTE_DARK)
        | set(PALETTE_LIGHT)
        | set(INDENT_TOKEN_NAMES)
        | set(RAIL_TOKEN_NAMES)
    )


def _expand_registry_signature(value: str) -> str:
    token_names = _registry_token_names()

    def replace(match: re.Match[str]) -> str:
        word = match.group(1)
        return f"var(--{word})" if word in token_names else word

    return _SIGNATURE_WORD.sub(replace, value)


def _normalize_value(value: str) -> str:
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(1))
        return f"__custom_property_{len(protected) - 1}__"

    normalized = re.sub(r"\s+", "", _CUSTOM_PROPERTY.sub(protect, value)).lower()
    for index, identifier in enumerate(protected):
        normalized = normalized.replace(f"__custom_property_{index}__", f"var(--{identifier})")
    return normalized


def _display_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _allowed_indent_values() -> set[str]:
    return {_normalize_value(f"var(--{name})") for name in INDENT_TOKEN_NAMES}


def _grid_signatures(archetype_name: str) -> set[str]:
    return {
        _normalize_value(_expand_registry_signature(signature.column_signature))
        for archetype in GRID_ARCHETYPES
        if archetype.name == archetype_name
        for signature in archetype.signatures
    }


def _shape_signatures(archetype_name: str) -> set[tuple[str, str, str]]:
    return {
        (
            _normalize_value(_expand_registry_signature(signature.radius_token)),
            _normalize_value(_expand_registry_signature(signature.border_signature)),
            _normalize_value(_expand_registry_signature(signature.elevation_token)),
        )
        for archetype in SHAPE_ARCHETYPES
        if archetype.name == archetype_name
        for signature in archetype.signatures
        if signature.radius_token is not None
        and signature.border_signature is not None
        and signature.elevation_token is not None
    }


def _uses_only_token_geometry(values: tuple[str, str, str]) -> bool:
    if not all(_CUSTOM_PROPERTY.search(value) for value in values):
        return False
    return not any(
        _RAW_HEX.search(value) or _FUNC_COLOR.search(value) or _PX.search(value) for value in values
    )


def _effective_declarations(
    rules: tuple[_CssRule, ...],
) -> tuple[tuple[str, Mapping[str, str]], ...]:
    events: list[tuple[tuple[str, ...], str, tuple[tuple[str, str], ...]]] = []
    display: dict[tuple[tuple[str, ...], str], str] = {}
    base_by_key: dict[tuple[tuple[str, ...], str], str] = {}
    for rule in rules:
        for index, selector in enumerate(rule.selectors):
            normalized_selector = re.sub(r"\s+", " ", selector.strip())
            if not normalized_selector:
                continue
            key = (rule.context, normalized_selector)
            display[key] = selector.strip()
            events.append((rule.context, normalized_selector, rule.declarations))
            base_selector = rule.base_selectors[index] if index < len(rule.base_selectors) else None
            if base_selector is not None:
                base_by_key[key] = re.sub(r"\s+", " ", base_selector.strip())

    states: dict[
        tuple[tuple[str, ...], str],
        dict[str, tuple[str, bool, tuple[int, int, int], int]],
    ] = {}
    for target_context, target_selector in display:
        state: dict[str, tuple[str, bool, tuple[int, int, int], int]] = {}
        for event_index, (event_context, event_selector, declarations) in enumerate(events):
            if event_selector != target_selector or not (
                len(event_context) <= len(target_context)
                and target_context[: len(event_context)] == event_context
            ):
                continue
            for property_name, value in declarations:
                important = bool(re.search(r"\s*!important\s*$", value, re.IGNORECASE))
                bare_value = re.sub(r"\s*!important\s*$", "", value, flags=re.IGNORECASE)
                prior = state.get(property_name)
                if prior is None or important or not prior[1]:
                    state[property_name] = (
                        bare_value,
                        important,
                        _selector_specificity(event_selector),
                        event_index,
                    )
        states[(target_context, target_selector)] = state

    resolved: dict[
        tuple[tuple[str, ...], str],
        dict[str, tuple[str, bool, tuple[int, int, int], int]],
    ] = {}

    def resolve(
        key: tuple[tuple[str, ...], str],
        visiting: frozenset[tuple[tuple[str, ...], str]] = frozenset(),
    ) -> dict[str, tuple[str, bool, tuple[int, int, int], int]]:
        if key in resolved:
            return resolved[key]
        if key in visiting:
            return dict(states[key])
        target_context, _target_selector = key
        merged: dict[str, tuple[str, bool, tuple[int, int, int], int]] = {}
        base_selector = base_by_key.get(key)
        if base_selector is not None:
            candidates = [
                candidate
                for candidate in states
                if candidate[1] == base_selector
                and len(candidate[0]) <= len(target_context)
                and target_context[: len(candidate[0])] == candidate[0]
            ]
            if candidates:
                base_key = max(candidates, key=lambda candidate: len(candidate[0]))
                merged.update(resolve(base_key, visiting | {key}))
        for property_name, value in states[key].items():
            prior = merged.get(property_name)
            if prior is None or ((value[1], value[2], value[3]) >= (prior[1], prior[2], prior[3])):
                merged[property_name] = value
        resolved[key] = merged
        return merged

    return tuple(
        (
            shown_selector,
            {
                name: value
                for name, (value, _important, _specificity, _order) in resolve(key).items()
            },
        )
        for key, shown_selector in display.items()
    )


def _grid_archetype(atom_name: str) -> str | None:
    if re.search(r"(?:^|-)card-grid(?:-|$)", atom_name, re.IGNORECASE):
        return "auto-fit-card-grid"
    if re.search(r"(?:^|-)split-rail(?:-|$)", atom_name, re.IGNORECASE):
        return "two-column-split-rail"
    return None


def _shape_archetype(atom_name: str) -> str | None:
    if re.search(r"(?:^|-)drawer(?:-|$)", atom_name, re.IGNORECASE):
        return "slide-drawer"
    if re.search(r"(?:^|-)well(?:-|$)", atom_name, re.IGNORECASE):
        return "micro-inset"
    if re.search(r"(?:^|-)(?:card|panel|box|tile)(?:-|$)", atom_name, re.IGNORECASE):
        return "macro-container"
    return None


def _scan_indent(rules: tuple[_CssRule, ...]) -> list[str]:
    findings: set[str] = set()
    allowed = _allowed_indent_values()
    for selector, declarations in _effective_declarations(rules):
        scoped_atoms = {
            f"{prefix}{name}"
            for prefix, name in _subject_selector_atoms(selector)
            if _INDENT_SCOPE_SEGMENT.search(name)
        }
        for property_name in ("margin-left", "padding-left"):
            value = declarations.get(property_name)
            if value is None:
                continue
            if _normalize_value(value) not in allowed:
                findings.update(
                    f"{atom}:{property_name}={_display_value(value)}" for atom in scoped_atoms
                )
    return sorted(findings)


def _scan_grid_columns(rules: tuple[_CssRule, ...]) -> list[str]:
    findings: set[str] = set()
    for selector, declarations in _effective_declarations(rules):
        value = declarations.get("grid-template-columns")
        if value is None:
            continue
        for prefix, name in _subject_selector_atoms(selector):
            archetype = _grid_archetype(name)
            if archetype is not None and _normalize_value(value) not in _grid_signatures(archetype):
                findings.add(f"{prefix}{name}:grid-template-columns={_display_value(value)}")
    return sorted(findings)


def _scan_shape_geometry(rules: tuple[_CssRule, ...]) -> list[str]:
    findings: set[str] = set()
    for selector, declarations in _effective_declarations(rules):
        radius = declarations.get("border-radius")
        border = declarations.get("border")
        elevation = declarations.get("box-shadow")
        if radius is None or border is None or elevation is None:
            continue
        values = (radius, border, elevation)
        if not _uses_only_token_geometry(values):
            continue
        normalized = tuple(_normalize_value(value) for value in values)
        for prefix, name in _subject_selector_atoms(selector):
            archetype = _shape_archetype(name)
            if archetype is not None and normalized not in _shape_signatures(archetype):
                findings.add(f"{prefix}{name}")
    return sorted(findings)


def _selector_class_compounds(
    signatures: Mapping[str, object], scope_pattern: re.Pattern[str]
) -> tuple[frozenset[str], ...]:
    compounds: list[frozenset[str]] = []
    for selector in signatures:
        classes = frozenset(name for prefix, name in _selector_atoms(selector) if prefix == ".")
        if classes and any(scope_pattern.search(name) for name in classes):
            compounds.append(classes)
    return tuple(compounds)


def _title_classes() -> frozenset[str]:
    return frozenset(
        name
        for selector, placement in TITLES_BY_SELECTOR.items()
        if placement.placement == "interior"
        for prefix, name in _selector_atoms(selector)
        if prefix == "."
    )


def _has_template_marker(value: str) -> bool:
    return "{" in value or "}" in value or "<%" in value or "%>" in value


_TEMPLATE_SPAN = re.compile(r"\{\{.*?\}\}|\{[^{}]*\}|<%.*?%>")


def _dynamic_class_pattern(atom: str) -> re.Pattern[str] | None:
    parts: list[str] = []
    cursor = 0
    matched = False
    for marker in _TEMPLATE_SPAN.finditer(atom):
        matched = True
        fixed = atom[cursor : marker.start()]
        if _has_template_marker(fixed):
            return None
        parts.extend((re.escape(fixed), ".*"))
        cursor = marker.end()
    trailing = atom[cursor:]
    if not matched or _has_template_marker(trailing):
        return None
    parts.append(re.escape(trailing))
    return re.compile("".join(parts))


def _dynamic_classes_can_form_container(
    class_values: tuple[str, ...],
    static_classes: frozenset[str],
    compounds: tuple[frozenset[str], ...],
) -> bool:
    patterns = tuple(
        pattern
        for value in class_values
        for atom in value.split()
        if _has_template_marker(atom)
        for pattern in (_dynamic_class_pattern(atom),)
    )
    return any(
        all(
            required in static_classes
            or any(pattern is None or pattern.fullmatch(required) for pattern in patterns)
            for required in compound
        )
        for compound in compounds
    )


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[_HtmlFrame] = []
        self.findings: set[str] = set()
        self.unverifiable: set[str] = set()
        self._container_compounds = _selector_class_compounds(
            SHAPES_BY_SELECTOR, _SHAPE_SCOPE_SEGMENT
        )
        self._accepted_titles = _title_classes()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        class_values = tuple(
            value for name, value in attrs if name.lower() == "class" and value is not None
        )
        classes = tuple(atom for value in class_values for atom in value.split())
        dynamic_class = any(_has_template_marker(value) for value in class_values)
        class_set = frozenset(classes)
        is_container = any(compound <= class_set for compound in self._container_compounds)
        heading = bool(_HEADING_TAG.fullmatch(tag))
        parent_within_container = bool(self._stack and self._stack[-1].within_container)
        parent_within_ambiguous = bool(self._stack and self._stack[-1].within_ambiguous_container)
        within_container = is_container or parent_within_container
        ambiguous_here = (
            not heading
            and dynamic_class
            and not is_container
            and _dynamic_classes_can_form_container(
                class_values,
                class_set,
                self._container_compounds,
            )
        )
        within_ambiguous = ambiguous_here or parent_within_ambiguous
        heading_allowed = bool(class_set & self._accepted_titles)
        frame = _HtmlFrame(
            tag=tag.lower(),
            classes=classes,
            dynamic=dynamic_class,
            within_container=within_container,
            within_ambiguous_container=within_ambiguous,
            heading_allowed=heading_allowed,
        )
        if heading and within_container and dynamic_class and not heading_allowed:
            self.unverifiable.add(f"dynamic-heading-class:{tag.lower()}")
        if heading and within_ambiguous and not within_container:
            self.unverifiable.add(f"dynamic-container-class:{tag.lower()}")
        self._stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        matching = next(
            (
                index
                for index in range(len(self._stack) - 1, -1, -1)
                if self._stack[index].tag == lowered
            ),
            None,
        )
        if matching is None:
            return
        # Implicitly/mismatched closed children are not statically verifiable.
        for child in self._stack[matching + 1 :]:
            if _HEADING_TAG.fullmatch(child.tag):
                self.unverifiable.add(f"unclosed-heading:{child.tag}")
        del self._stack[matching + 1 :]
        frame = self._stack.pop()
        if (
            _HEADING_TAG.fullmatch(frame.tag)
            and frame.within_container
            and not frame.heading_allowed
            and not frame.dynamic
        ):
            suffix = "".join(f".{name}" for name in frame.classes)
            self.findings.add(f"{frame.tag}{suffix}")

    def finalize(self) -> None:
        for frame in self._stack:
            if _HEADING_TAG.fullmatch(frame.tag):
                self.unverifiable.add(f"unclosed-heading:{frame.tag}")


def _scan_floating_titles(text: str) -> tuple[list[str], list[str]]:
    parser = _TitleParser()
    if _contains_dynamic_tag(text):
        parser.unverifiable.add("dynamic-tag")
    try:
        parser.feed(text)
        parser.close()
        parser.finalize()
    except (AssertionError, ValueError) as exc:
        parser.unverifiable.add(f"parser-error:{type(exc).__name__}")
    return sorted(parser.findings), sorted(parser.unverifiable)


def _unknown_custom_properties(rel: str, text: str) -> list[str]:
    scoped_local_names = {
        contract.name.removeprefix("--").lower()
        for contract in LOCAL_PROPERTY_CONTRACTS
        if rel in contract.surfaces
    }
    references = {match.group(1).lower() for match in _CUSTOM_PROPERTY_REFERENCE.finditer(text)}
    declarations = {match.group(1).lower() for match in _CUSTOM_PROPERTY_DECL.finditer(text)}
    invalid_references = references - (_CANONICAL_CUSTOM_PROPERTIES | scoped_local_names)
    invalid_declarations = declarations - (_CANONICAL_CUSTOM_PROPERTIES | scoped_local_names)
    return [f"--{name}" for name in sorted(invalid_references | invalid_declarations)]


def _local_property_value_findings(rel: str, text: str) -> list[str]:
    contracts = {
        contract.name.removeprefix("--").lower(): contract
        for contract in LOCAL_PROPERTY_CONTRACTS
        if rel in contract.surfaces
    }
    findings: set[str] = set()
    for match in _CUSTOM_PROPERTY_VALUE_DECL.finditer(text):
        name = match.group(1).lower()
        contract = contracts.get(name)
        if contract is None:
            continue
        value = _normalize_css_fragment(match.group(2))
        references = {item.group(1).lower() for item in _CUSTOM_PROPERTY_REFERENCE.finditer(value)}
        if references and references <= _CANONICAL_CUSTOM_PROPERTIES:
            continue
        if contract.value_grammar == "CSS <integer>" and re.fullmatch(r"\d+", value):
            continue
        if contract.value_grammar == "CSS <image>" and value.startswith("url("):
            continue
        findings.add(f"--{name}={value}")
    return sorted(findings)


def _named_color_findings(rules: tuple[_CssRule, ...]) -> list[str]:
    found: set[str] = set()
    for rule in rules:
        for property_name, value in rule.declarations:
            normalized_property = property_name.strip().lower()
            if normalized_property not in _COLOR_PROPERTIES:
                continue
            decoded_value = re.sub(
                r"\\([0-9a-fA-F]{1,6})(?:\s)?|\\(.)",
                lambda match: (
                    chr(int(match.group(1), 16)) if match.group(1) else (match.group(2) or "")
                ),
                value,
            )
            words = {word.lower() for word in _SIGNATURE_WORD.findall(decoded_value)}
            found.update(words & _CSS_NAMED_COLORS)
            simple = decoded_value.strip().lower()
            if (
                normalized_property
                in {"background-color", "border-color", "color", "fill", "stroke"}
                and re.fullmatch(r"[a-z]+", simple)
                and simple
                not in {"currentcolor", "inherit", "initial", "none", "transparent", "unset"}
            ):
                found.add(simple)
    return sorted(found)


def _consumer_css_selector(selector: str) -> str | None:
    """Recover a CSS selector, including one embedded in a JS string assignment."""

    normalized = selector.strip()
    if _CSS_SELECTOR_START.match(normalized):
        return normalized
    assigned = re.search(r"=\s*[`'\"]?\s*(?P<selector>[.#:\[].*)$", normalized)
    if assigned is None:
        return None
    candidate = assigned.group("selector").strip()
    return candidate if _CSS_SELECTOR_START.match(candidate) else None


def _canonical_token_redefinitions(rules: tuple[_CssRule, ...]) -> list[str]:
    findings: set[str] = set()
    for rule in rules:
        root_scoped = bool(rule.selectors) and all(
            _normalize_css_fragment(selector).startswith(":root") for selector in rule.selectors
        )
        for property_name, raw_value in rule.declarations:
            name = property_name.removeprefix("--").lower()
            if not property_name.startswith("--") or name not in _CANONICAL_CUSTOM_PROPERTIES:
                continue
            value = _normalize_css_fragment(raw_value)
            allowed = {
                _normalize_css_fragment(item) for item in _CANONICAL_CUSTOM_PROPERTY_VALUES[name]
            }
            if not root_scoped or value not in allowed:
                findings.add(f"--{name}={value}")
    return sorted(findings)


def _runtime_visual_mutations(rel: str, text: str) -> list[str]:
    counts: Counter[str] = Counter()
    findings: list[str] = []
    sanctioned_properties = {
        contract.property_name: contract
        for contract in RUNTIME_VISUAL_CONTRACTS
        if contract.surface == rel
    }
    runtime_text = _normalize_runtime_js_syntax(text)
    for match in _RUNTIME_VISUAL_MUTATION.finditer(runtime_text):
        style_property = match.groupdict().get("style_property")
        style_value = match.groupdict().get("style_value")
        if match.groupdict().get("outer_html_property"):
            style_property = "outerHTML"
            style_value = match.groupdict().get("outer_html_value")
        contract = sanctioned_properties.get(style_property or "")
        if contract is not None and style_value is not None:
            normalized_value = _normalize_css_fragment(style_value)
            if re.fullmatch(contract.value_pattern, normalized_value):
                continue
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _DYNAMIC_CREATE_ELEMENT.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _INDIRECT_VISUAL_INVOCATION.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _VISUAL_MUTATOR_ALIAS.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _RUNTIME_HTML_SINK.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _RUNTIME_STYLE_ELEMENT_SINK.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _RUNTIME_STYLESHEET_COLLECTION_SINK.finditer(runtime_text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _DYNAMIC_CLASS_LIST.finditer(text):
        args = split_top_commas(match.group("args"))
        class_args = args[:2] if ".replace" in match.group(0).lower() else args[:1]
        if class_args and all(_JS_STRING_LITERAL_ONLY.fullmatch(arg) for arg in class_args):
            continue
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _DYNAMIC_CLASS_ATTRIBUTE.finditer(text):
        if _JS_STRING_LITERAL_ONLY.fullmatch(match.group("value")):
            continue
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    return sorted(findings)


def _inline_style_apis(text: str) -> list[str]:
    """Reject React escape hatches that bypass the token/component vocabulary."""

    counts: Counter[str] = Counter()
    findings: list[str] = []
    for match in _JSX_INLINE_STYLE_API.finditer(text):
        signature = _normalize_css_fragment(match.group(0))
        counts[signature] += 1
        findings.append(f"{signature}|#{counts[signature]}")
    for match in _REACT_NATIVE_PROP_DECL.finditer(text):
        if re.search(r"\bOmit\s*<[^;{]*['\"]style['\"]", match.group("body"), re.IGNORECASE):
            continue
        findings.append(f"inherited-native-style:{match.group('name')}")
    return sorted(findings)


def _literal_inline_style_findings(text: str) -> list[str]:
    """Reject static inline presentation; dynamic recipes use a typed digest."""

    return sorted(
        {
            _normalize_css_fragment(match.group("value"))
            for match in _INLINE_STYLE_ATTRIBUTE.finditer(text)
            if not _DYNAMIC_VALUE_PLACEHOLDER.search(match.group("value"))
        }
    )


def _svg_presentation_findings(text: str) -> tuple[list[str], list[str]]:
    findings: set[str] = set()
    unverifiable: set[str] = set()
    allowed_keywords = {"currentcolor", "none", "inherit", "transparent"}
    for match in _SVG_PRESENTATION.finditer(text):
        attribute = match.group(1).lower()
        value = _normalize_css_fragment(match.group(3))
        if not value or "{" in value or "}" in value:
            unverifiable.add(f"dynamic-svg-presentation:{attribute}")
            continue
        lowered = value.lower()
        if lowered in allowed_keywords:
            continue
        token_match = _CUSTOM_PROPERTY.fullmatch(value)
        if token_match and token_match.group(1).lower() in _CANONICAL_CUSTOM_PROPERTIES:
            continue
        findings.add(f"{attribute}={value}")
    return sorted(findings), sorted(unverifiable)


def scan_surface_evidence(rel: str, text: str) -> SurfaceScanEvidence:
    """Return violations and explicit structural uncertainty for one surface."""
    if rel in EXEMPT:
        return SurfaceScanEvidence(findings=(), unverifiable_markup=())
    master_geometry = _master_geometry_contract_findings(rel, text)
    visual_source = text.source_text if isinstance(text, _ExtractedSurfaceText) else text
    object_styles = _object_style_findings(rel, visual_source)
    if rel in GLOBAL_MASTER_SOURCES:
        dynamic_findings, dynamic_unverifiable = _dynamic_visual_contract_findings(
            rel, visual_source
        )
        findings: list[tuple[str, tuple[str, ...]]] = []
        if master_geometry:
            findings.append(("master-geometry-contract", tuple(master_geometry)))
        if dynamic_findings:
            findings.append(("dynamic-visual-contract", tuple(dynamic_findings)))
        if object_styles:
            findings.append(("dynamic-visual-contract", tuple(object_styles)))
        unverifiable: list[str] = []
        if dynamic_unverifiable:
            unverifiable.append("dynamic-visual-value")
        if _dynamic_html_skeletons(visual_source):
            unverifiable.append("dynamic-html-markup")
        if dynamic_unverifiable:
            unverifiable.extend(_opaque_visual_composition_kinds(visual_source))
        return SurfaceScanEvidence(
            findings=tuple(findings), unverifiable_markup=tuple(sorted(set(unverifiable)))
        )
    found: dict[str, list[str]] = {}
    if master_geometry:
        found["master-geometry-contract"] = master_geometry
    if rel in MASTER_SOURCES and object_styles:
        found["dynamic-visual-contract"] = object_styles
    legacy_text = text.legacy_text if isinstance(text, _ExtractedSurfaceText) else text

    rules = _surface_css_rules(text)
    if rel.endswith((".py", ".ts", ".tsx", ".css")) and rel not in MASTER_SOURCES:
        consumer_declarations = sorted(
            {
                f"{governed_selector}:{property_name}={_normalize_css_fragment(value)}"
                for rule in rules
                if rule.selectors != ("[style]",)
                for selector in rule.selectors
                for governed_selector in (
                    _consumer_css_selector(selector)
                    or (
                        selector.strip()
                        if any("keyframes" in item.lower() for item in rule.context)
                        else None
                    ),
                )
                if governed_selector is not None
                for property_name, value in rule.declarations
            }
        )
        if consumer_declarations:
            found["consumer-visual-css"] = consumer_declarations
        if object_styles:
            found.setdefault("consumer-visual-css", []).extend(object_styles)
    colors = (
        set(_RAW_HEX.findall(legacy_text))
        | set(_FUNC_COLOR.findall(legacy_text))
        | set(_named_color_findings(rules))
    )
    if colors:
        found["color"] = sorted(colors)

    token_redefinitions = _canonical_token_redefinitions(rules)
    if token_redefinitions:
        found["canonical-token-redefinition"] = token_redefinitions

    if rel not in FONT_SIZE_EXEMPT:
        sizes: list[str] = []
        for raw_value in _FONT_SIZE_DECL.findall(text):
            value = re.sub(
                r"\s*!important\s*$", "", _normalize_css_fragment(raw_value), flags=re.IGNORECASE
            )
            if value in TYPE_SCALE_PX:
                continue
            token = _CUSTOM_PROPERTY.fullmatch(value)
            if token and token.group(1).lower() in _CANONICAL_CUSTOM_PROPERTIES:
                continue
            if value.lower() in {"inherit", "initial", "revert", "unset"}:
                continue
            sizes.append(value)
        # Retain the legacy token view for malformed/in-progress Python while
        # the semantic path above owns complete source.
        sizes.extend(
            match for match in _FONT_SIZE.findall(legacy_text) if match not in TYPE_SCALE_PX
        )
        sizes.extend(_RELATIVE_FONT_SIZE.findall(text))
        if sizes:
            found["font-size"] = sorted(set(sizes))

    shorthand = [
        _normalize_css_fragment(value)
        for value in _FONT_SHORTHAND.findall(text)
        if _normalize_css_fragment(value).lower()
        not in {"inherit", "initial", "revert", "revert-layer", "unset"}
    ]
    if shorthand:
        found["font-shorthand"] = sorted(set(shorthand))

    ok_radii = RADIUS_PX | RADIUS_SANCTIONED.get(rel, frozenset())
    radii = [
        px
        for value in _RADIUS_DECL.findall(legacy_text)
        for px in _PX.findall(value)
        if px not in ok_radii
    ]
    if radii:
        found["radius"] = sorted(set(radii))

    families: list[str] = []
    for value in _FONT_FAMILY.findall(legacy_text):
        for part in split_top_commas(value):
            if part in FONT_FAMILY_KEYWORDS or _FONT_TOKEN.match(part):
                continue
            families.append(part)
    if families:
        found["font-family"] = sorted(set(families))

    aliases = sorted({match.group(0) for match in _ALIAS.finditer(legacy_text)})
    if aliases:
        found["alias"] = aliases

    badges: list[str] = []
    for rule in re.split(r"(?<=})\s*", strip_css_comments(legacy_text)):
        head, _, body = rule.partition("{")
        named = _NAMED_BADGE.search(head)
        if named and not _KIT_BADGE.search(head) and _STATUS_FILL.search(body):
            badges.append(named.group(0))
    if badges:
        found["kit-badge"] = sorted(set(badges))

    weights = _FONT_WEIGHT.findall(legacy_text)
    if weights:
        found["font-weight"] = sorted(set(weights))

    if _TRANSITION_ALL.search(legacy_text):
        found["transition"] = ["all"]

    unknown_properties = _unknown_custom_properties(rel, text)
    if unknown_properties:
        found["unknown-custom-property"] = unknown_properties

    local_property_values = _local_property_value_findings(rel, text)
    if local_property_values:
        found["local-property-value"] = local_property_values

    runtime_mutations = _runtime_visual_mutations(rel, text)
    if runtime_mutations:
        found["runtime-visual-mutation"] = runtime_mutations

    inline_style_apis = _inline_style_apis(text)
    if inline_style_apis:
        found["inline-style-api"] = inline_style_apis

    inline_style_attributes = _literal_inline_style_findings(text)
    if inline_style_attributes:
        found["inline-style-attribute"] = inline_style_attributes

    svg_presentation, svg_unverifiable = _svg_presentation_findings(text)
    if svg_presentation:
        found["svg-presentation"] = svg_presentation

    dynamic_visual_findings, dynamic_visual_unverifiable = _dynamic_visual_contract_findings(
        rel, visual_source
    )
    if dynamic_visual_findings:
        found["dynamic-visual-contract"] = dynamic_visual_findings

    floating_titles, unverifiable_markup = _scan_floating_titles(strip_css_comments(text))
    unverifiable_markup.extend(svg_unverifiable)
    if _dynamic_html_skeletons(visual_source):
        unverifiable_markup.append("dynamic-html-markup")
    if dynamic_visual_unverifiable:
        unverifiable_markup.extend(_opaque_visual_composition_kinds(visual_source))
    has_dynamic_declaration = any(
        _DYNAMIC_VALUE_PLACEHOLDER.search(value)
        for rule in rules
        for _property_name, value in rule.declarations
    )
    has_dynamic_recipe = bool(_dynamic_visual_skeletons(visual_source))
    if dynamic_visual_unverifiable or (has_dynamic_declaration and not has_dynamic_recipe):
        unverifiable_markup.append("dynamic-visual-value")
    if isinstance(text, _ExtractedSurfaceText) and text.composition_limited:
        unverifiable_markup.append("static-composition-limit")
    if floating_titles:
        found["floating-card-title"] = floating_titles
    indents = _scan_indent(rules)
    if indents:
        found["off-scale-indent"] = indents
    shapes = _scan_shape_geometry(rules)
    if shapes:
        found["unsanctioned-shape-geometry"] = shapes
    columns = _scan_grid_columns(rules)
    if columns:
        found["off-scale-grid-column"] = columns

    return SurfaceScanEvidence(
        findings=tuple((dimension, tuple(values)) for dimension, values in sorted(found.items())),
        unverifiable_markup=tuple(sorted(set(unverifiable_markup))),
    )


def scan_surface(rel: str, text: str) -> dict[str, list[str]]:
    """Compatibility projection of deterministic violations only."""
    return scan_surface_evidence(rel, text).violations()
