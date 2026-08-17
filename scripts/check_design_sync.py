"""Fail fast when canonical design assets or HTML surfaces drift.

This is the repository-level design-sync entry point. It checks the generated
React token mirror, the shared application-chrome selectors, the production
Work OS prototype/hydration path, and standalone HTML documents emitted from
``execution/`` (which the src-only CSS guard does not discover).

Usage::

    python scripts/check_design_sync.py
"""

from __future__ import annotations

import json
import re
import sys
import token as _token
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC))

from scripts.gen_design_tokens import check as check_generated_tokens  # noqa: E402
from ui.controls import controls_css  # noqa: E402
from ui.tokens import CHROME_TOKENS, PALETTE_DARK, SPACING_SCALE, TYPE_SCALE  # noqa: E402

REACT_CONTROLS = PROJECT_ROOT / "design-system" / "src" / "styles" / "controls.css"
GEOMETRY_BASELINE = PROJECT_ROOT / "tests" / "design_geometry_baseline.json"
WORK_OS_PROTOTYPE = PROJECT_ROOT / "mockups" / "harvey_sidebar_flow.html"
WORK_OS_RENDERER = SRC / "pipeline" / "work_os_shell.py"
REQUIRED_CONTROL_SELECTORS: tuple[str, ...] = (
    ".k-sidebar {",
    ".k-icon {",
    ".k-icon-btn {",
    ".k-nav-item {",
    ".k-card {",
    ".k-card-dense {",
    ".k-card-stack {",
    ".k-card-title {",
    ".k-card-row-title {",
    ".k-card-meta {",
)
DOCUMENT_MARKERS: tuple[str, ...] = ("<!doctype html>", "<!DOCTYPE html>")
_RAW_HEX = re.compile(r"(?<![\w-])#[0-9a-fA-F]{3,8}\b")
_RAW_FONT_SIZE = re.compile(r"font-size\s*:\s*[0-9.]+px")
_RAW_RADIUS = re.compile(r"border-radius\s*:\s*[0-9.]+px")
_RAW_FONT_FAMILY = re.compile(r"font-family\s*:(?!\s*var\()[^;}]+")
_ROOT_RULE = re.compile(r":root(?:\[[^\]]+\])?\s*\{[^{}]*\}", re.DOTALL)
_RAW_GEOMETRY_DECL = re.compile(
    r"(?<![\w-])(?:"
    r"(?:min-|max-)?(?:width|height)|"
    r"(?:margin|padding)(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?|"
    r"(?:row-|column-)?gap|"
    r"(?:top|right|bottom|left|inset)(?:-(?:top|right|bottom|left|inline|block))?|"
    r"border(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?(?:-width)?|"
    r"outline(?:-width|-offset)?|box-shadow|text-shadow|"
    r"filter|backdrop-filter|transform|flex-basis|"
    r"grid-template(?:-columns|-rows)?|background-(?:position|size)"
    r")\s*:\s*[^;{}\n]*\b\d+(?:\.\d+)?px\b",
    re.IGNORECASE,
)
_STRING_TOKENS = frozenset({_token.STRING})
_SKIP_TOKENS = frozenset(
    {_token.NL, _token.INDENT, _token.DEDENT, _token.COMMENT, _token.ENCODING, _token.ENDMARKER}
)
_TOKEN_DECL = re.compile(r"--(?P<name>[\w-]+)\s*:\s*(?P<value>[^;]+);")
_WORK_OS_CHROME_KEYS: tuple[str, ...] = (
    "sidebar-width",
    "sidebar-collapsed-width",
    "header-height",
    "nav-item-height",
    "icon-size",
    "icon-button-size",
    "mobile-control-font-size",
    "touch-target-size",
)


def has_html_class(text: str, class_name: str) -> bool:
    """Return whether a quoted HTML class attribute composes ``class_name``."""
    class_attrs = re.finditer(r"(?<![\w:-])class\s*=\s*([\"'])(?P<classes>.*?)\1", text, re.DOTALL)
    return any(class_name in match.group("classes").split() for match in class_attrs)


_WORK_OS_PALETTE_KEYS: tuple[str, ...] = (
    "bg",
    "surface",
    "paper",
    "fg",
    "fg-soft",
    "muted",
    "border",
    "border-2",
    "hairline",
    "accent",
    "accent-soft",
    "accent-contrast",
    "ok",
    "warn",
    "bad",
    "mark",
    "series-qqq",
)


def _rule_body(css: str, selector: str) -> str | None:
    """Return a whitespace-normalized declaration body for one exact selector."""
    start = css.find(selector)
    if start < 0:
        return None
    body_start = start + len(selector)
    body_end = css.find("}", body_start)
    if body_end < 0:
        return None
    return " ".join(css[body_start:body_end].split())


def control_parity_failures() -> list[str]:
    canonical = controls_css("paper")
    react = REACT_CONTROLS.read_text(encoding="utf-8")
    failures: list[str] = []
    for selector in REQUIRED_CONTROL_SELECTORS:
        canonical_body = _rule_body(canonical, selector)
        react_body = _rule_body(react, selector)
        if canonical_body is None:
            failures.append(f"canonical controls missing {selector}")
        elif react_body is None:
            failures.append(f"React controls missing {selector}")
        elif canonical_body != react_body:
            failures.append(f"Python/React declarations drifted for {selector}")
    return failures


def work_os_contract_failures(
    *, mockup_text: str | None = None, renderer_source: str | None = None
) -> list[str]:
    """Verify that the prototype used by ``/`` is the canonical new design."""
    mockup = WORK_OS_PROTOTYPE.read_text(encoding="utf-8") if mockup_text is None else mockup_text
    renderer = (
        WORK_OS_RENDERER.read_text(encoding="utf-8") if renderer_source is None else renderer_source
    )
    root_match = _ROOT_RULE.search(mockup)
    if root_match is None:
        return ["Work OS prototype has no :root token block"]
    actual_tokens = {
        match.group("name"): " ".join(match.group("value").split())
        for match in _TOKEN_DECL.finditer(root_match.group(0))
    }
    expected_tokens = {
        **TYPE_SCALE,
        **SPACING_SCALE,
        **{name: CHROME_TOKENS[name] for name in _WORK_OS_CHROME_KEYS},
        **{name: PALETTE_DARK[name] for name in _WORK_OS_PALETTE_KEYS},
    }
    failures = [
        f"Work OS token --{name} is {actual_tokens.get(name)!r}; expected {value!r}"
        for name, value in expected_tokens.items()
        if actual_tokens.get(name) != value
    ]
    outside_root = _ROOT_RULE.sub("", mockup)
    match = _RAW_FONT_SIZE.search(outside_root)
    if match:
        failures.append(f"Work OS prototype has off-token type {match.group(0)!r}")
    for required in (
        '"mockups" / "harvey_sidebar_flow.html"',
        "_prototype_html()",
        'palette_css("dark")',
        'controls_css("dark")',
        "font-size: var(--mobile-control-font-size) !important",
        "width: var(--sidebar-collapsed-width)",
        "min-width: var(--sidebar-collapsed-width)",
        "min-block-size: var(--touch-target-size)",
    ):
        if required not in renderer:
            failures.append(f"Work OS renderer missing production design contract {required!r}")
    action_classes = (
        "k-card",
        "k-card-dense",
        "k-card-interactive",
        "k-card-row-title",
        "k-card-meta",
    )
    for class_name in action_classes:
        if not has_html_class(mockup, class_name):
            failures.append(f"Work OS static Action Queue missing class {class_name!r}")
    for class_name in action_classes:
        if not has_html_class(renderer, class_name):
            failures.append(f"Work OS hydrated Action Queue missing class {class_name!r}")
    return failures


def _python_string_payload(path: Path) -> str:
    """Return string payloads while excluding comments and bare docstrings."""
    out: list[str] = []
    depth = 0
    line_start = True
    with path.open("rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                token_type, token_text = tok.type, tok.string
                if token_type == _token.OP:
                    if token_text in "([{":
                        depth += 1
                    elif token_text in ")]}":
                        depth = max(0, depth - 1)
                if token_type == _token.NEWLINE:
                    line_start = True
                    continue
                if token_type in _SKIP_TOKENS:
                    continue
                is_fstring = tokenize.tok_name.get(token_type, "").startswith("FSTRING")
                if token_type in _STRING_TOKENS and line_start and depth == 0:
                    line_start = False
                    continue
                if token_type in _STRING_TOKENS or is_fstring:
                    out.append(token_text)
                line_start = False
        except tokenize.TokenError:
            pass
    return "\n".join(out)


def geometry_counts() -> dict[str, int]:
    """Count raw geometry declarations outside token-definition root blocks."""
    surfaces: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "var(--" in source:
            key = path.relative_to(PROJECT_ROOT).as_posix()
            surfaces[key] = _python_string_payload(path)
    for path in sorted((PROJECT_ROOT / "execution").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in DOCUMENT_MARKERS):
            key = path.relative_to(PROJECT_ROOT).as_posix()
            surfaces[key] = _python_string_payload(path)
    return {
        rel: len(_RAW_GEOMETRY_DECL.findall(_ROOT_RULE.sub("", payload)))
        for rel, payload in surfaces.items()
        if _RAW_GEOMETRY_DECL.search(_ROOT_RULE.sub("", payload))
    }


def geometry_baseline_failures(
    *, actual: dict[str, int] | None = None, baseline: dict[str, int] | None = None
) -> list[str]:
    """Enforce an exact, shrink-only baseline for legacy raw geometry.

    Any cleanup must lower the checked-in baseline in the same change. Any new
    declaration or unclassified surface fails instead of silently expanding
    the tolerated debt.
    """
    observed = geometry_counts() if actual is None else actual
    expected = (
        json.loads(GEOMETRY_BASELINE.read_text(encoding="utf-8")) if baseline is None else baseline
    )
    failures: list[str] = []
    for rel in sorted(set(observed) | set(expected)):
        count = observed.get(rel, 0)
        ceiling = expected.get(rel, 0)
        if count > ceiling:
            failures.append(f"{rel}: raw geometry grew {ceiling} -> {count}")
        elif count < ceiling:
            failures.append(
                f"{rel}: raw geometry shrank {ceiling} -> {count}; lower the checked-in baseline"
            )
    return failures


def _standalone_surface_failures() -> list[str]:
    failures: list[str] = []
    for path in sorted((PROJECT_ROOT / "execution").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(marker in source for marker in DOCUMENT_MARKERS):
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if "palette_css(" not in source:
            failures.append(f"{rel}: standalone document does not compose palette_css")
        if "controls_css(" not in source:
            failures.append(f"{rel}: standalone document does not compose controls_css")
        for label, pattern in (
            ("raw color", _RAW_HEX),
            ("off-token font size", _RAW_FONT_SIZE),
            ("off-token radius", _RAW_RADIUS),
            ("off-token font family", _RAW_FONT_FAMILY),
        ):
            match = pattern.search(source)
            if match:
                failures.append(f"{rel}: {label} {match.group(0)!r}")
        if 'class="badge ' in source or "class='badge " in source:
            failures.append(f"{rel}: freehand badge component")
    return failures


def main() -> int:
    failures: list[str] = []
    if not check_generated_tokens():
        failures.append("generated React tokens drifted from src/ui/tokens.py")
    failures.extend(control_parity_failures())
    failures.extend(work_os_contract_failures())
    failures.extend(geometry_baseline_failures())
    failures.extend(_standalone_surface_failures())
    if failures:
        print("design sync: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("design sync: guard checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
