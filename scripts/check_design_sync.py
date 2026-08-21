"""Fail fast when canonical design assets or HTML surfaces drift.

This is the repository-level design-sync entry point. It delegates generated
token and control-kit parity to their source-specific generators, then checks
the production Work OS prototype/hydration contract.  Visual conformance and
geometry policy live in the design-conformance scanner, not in this wrapper.

Usage::

    python scripts/check_design_sync.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC))

from scripts.gen_design_controls import check as check_generated_controls  # noqa: E402
from scripts.gen_design_tokens import check as check_generated_tokens  # noqa: E402
from ui.tokens import CHROME_TOKENS, PALETTE_DARK, SPACING_SCALE, TYPE_SCALE  # noqa: E402

WORK_OS_PROTOTYPE = PROJECT_ROOT / "mockups" / "harvey_sidebar_flow.html"
WORK_OS_RENDERER = SRC / "pipeline" / "work_os_shell.py"
WORK_OS_STYLE_MASTER = SRC / "pipeline" / "work_os_styles.py"
_ROOT_RULE = re.compile(r":root(?:\[[^\]]+\])?\s*\{[^{}]*\}", re.DOTALL)
_RAW_FONT_SIZE = re.compile(r"font-size\s*:\s*[0-9.]+px")
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


def generated_control_failures() -> list[str]:
    """Expose generation drift without duplicating control/parity policy."""
    return (
        []
        if check_generated_controls()
        else ["generated React controls drifted from src/ui/controls.py"]
    )


def conformance_receipt_failures() -> list[str]:
    """Delegate visual evidence and debt policy to the canonical verifier."""

    result = subprocess.run(
        [sys.executable, "execution/verify_design_conformance.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []
    try:
        receipt = json.loads(result.stdout)
    except (TypeError, ValueError):
        return [f"design conformance verifier failed with exit {result.returncode}"]
    return [
        "design conformance failed: "
        f"{len(receipt.get('unregistered_surfaces', []))} unregistered, "
        f"{len(receipt.get('stale_registrations', []))} stale, "
        f"{len(receipt.get('debt_mismatches', []))} debt mismatches"
    ]


def work_os_contract_failures(
    *, mockup_text: str | None = None, renderer_source: str | None = None
) -> list[str]:
    """Verify that the prototype used by ``/`` is the canonical new design."""
    mockup = WORK_OS_PROTOTYPE.read_text(encoding="utf-8") if mockup_text is None else mockup_text
    renderer = (
        WORK_OS_RENDERER.read_text(encoding="utf-8")
        + "\n"
        + WORK_OS_STYLE_MASTER.read_text(encoding="utf-8")
        if renderer_source is None
        else renderer_source
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
        "k-card-action",
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


def main() -> int:
    failures: list[str] = []
    if not check_generated_tokens():
        failures.append("generated React tokens drifted from src/ui/tokens.py")
    failures.extend(generated_control_failures())
    failures.extend(conformance_receipt_failures())
    failures.extend(work_os_contract_failures())
    if failures:
        print("design sync: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("design sync: guard checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
