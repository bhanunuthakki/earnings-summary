"""BHA-92 report renderer and citation design contracts.

These tests keep the report's serialized CSS on the shared design scale.  The
scanner assertions are intentionally exact: each entry is one known debt item,
so a future change cannot hide a new literal behind a broad allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_styles import CSS as WORKSPACE_CSS
from ui.cite_marks import CITE_MARKS_CSS
from ui.conformance_scan import css_text, scan_surface
from ui.tokens import palette_css

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _property_value(css: str, selector: str, property_name: str) -> str:
    rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css)
    assert rule, selector
    declaration = re.search(rf"(?:^|;)\s*{re.escape(property_name)}\s*:\s*([^;]+)", rule[1])
    assert declaration, (selector, property_name)
    return declaration[1].strip()


def _resolve_palette_var(value: str, palette: str) -> str:
    token = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
    assert token, value
    declaration = re.search(rf"--{re.escape(token[1])}:\s*([^;]+)", palette)
    assert declaration, token[1]
    return declaration[1].strip()


@pytest.mark.parametrize(
    "rel",
    (
        "report/renderers/workspace_charts.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_styles.py",
    ),
)
def test_bha92_report_radius_debt_is_zero(rel: str) -> None:
    """Each owned report surface has no scanner-reported radius debt."""
    assert scan_surface(rel, css_text(SRC / rel)).get("radius", []) == []


@pytest.mark.parametrize("dimension", ("color", "radius"))
def test_bha92_cite_marks_have_no_tokenless_color_or_radius_fallback(
    dimension: str,
) -> None:
    """Citation CSS requires the host palette; it is not a standalone theme."""
    findings = scan_surface("ui/cite_marks.py", css_text(SRC / "ui/cite_marks.py"))
    assert findings.get(dimension, []) == []
    assert "rgba(" not in CITE_MARKS_CSS
    assert not re.search(r"var\([^)]*,\s*[^)]*\)", CITE_MARKS_CSS)


def test_cite_marks_production_host_is_palette_first_and_resolves_required_tokens() -> None:
    """Inventory the supported report host and prove its cite tokens resolve.

    ``workspace_html`` emits ``workspace_styles.CSS`` before ``workspace_chat.CSS``;
    the latter owns the cite fragment.  This is the minimal supported-host
    contract after retiring cite's tokenless fallbacks.
    """
    production_hosts = {"workspace-report": WORKSPACE_CSS + CHAT_CSS}
    palette = palette_css("paper")
    palette_tokens = set(re.findall(r"--([a-z0-9-]+):", palette))
    cite_tokens = set(re.findall(r"var\(--([a-z0-9-]+)\)", CITE_MARKS_CSS))

    assert production_hosts
    for host_name, host_css in production_hosts.items():
        assert host_css.index(palette) < host_css.index(CITE_MARKS_CSS), host_name
        assert cite_tokens <= palette_tokens, (host_name, sorted(cite_tokens - palette_tokens))

    minimal_supported_host = (
        f"<style>{palette}\n{CITE_MARKS_CSS}</style>"
        '<span class="cite-wrap"><span class="cite-val">42</span>'
        '<span class="cite-badge">1</span><span class="cite-pop">Source</span></span>'
    )
    assert minimal_supported_host.index(palette) < minimal_supported_host.index(CITE_MARKS_CSS)
    expected_computed_tokens = {
        (".cite-wrap .cite-pop", "border-radius"): "radius",
        (".cite-wrap .cite-pop", "box-shadow"): "shadow-pop",
        (".cite-wrap .cite-pop", "font-size"): "fs-caption",
        (".cite-wrap .cite-badge", "border-radius"): "radius-full",
        (".cite-wrap .cite-val", "border-radius"): "radius",
    }
    for (selector, property_name), expected_token in expected_computed_tokens.items():
        tokenized = _property_value(CITE_MARKS_CSS, selector, property_name)
        assert tokenized == f"var(--{expected_token})"
        assert _resolve_palette_var(tokenized, palette)
