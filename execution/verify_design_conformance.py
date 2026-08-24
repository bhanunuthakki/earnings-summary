"""Verify registry-backed design conformance with the canonical shared scanner.

Static source scanning is authoritative.  The optional live canary is a
supplementary, read-only check of the same scanner's structural title rule.
The opt-in browser canary additionally checks a bounded rendered specimen's
DOM and computed styles; it does not prove family-master conformance.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import date
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC))

from execution.design_route_canaries import (  # noqa: E402
    ROUTE_SCREEN_IDS,
    render_route_canary,
)
from ui.conformance_scan import (  # noqa: E402
    css_text,
    discover_emitters,
    finding_debt_id,
    geometry_debt_failures,
    geometry_debt_fingerprints,
    scan_surface_evidence,
    unverifiable_debt_id,
)
from ui.design_registry import (  # noqa: E402
    CARD_ARCHETYPES,
    GOVERNED,
    QUARANTINE_ENTRIES,
    REGISTERED,
    REGISTRY_VERSION,
    VISUAL_EMITTER_MANIFEST,
)


class _NoCanaryRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so every fetched URL is the validated CLI input."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


SCHEMA_VERSION = "1.3.0"
_CANARY_READ_LIMIT = 1_000_000
_CANARY_READ_CHUNK = 64 * 1024
_CANARY_WALL_TIMEOUT_SECONDS = 3.0
_BROWSER_CANARY_SETTLE_MILLISECONDS = 400

# These are the root tokens consumed by the canonical primitives below.  Keep
# this list deliberately small: a canary page need only prove that the design
# contract it exercises has a real canonical root, not reproduce every token
# used by every surface in the application.
_BROWSER_CANARY_ROOT_PROPERTIES = (
    "--fs-display",
    "--fs-title",
    "--fs-body",
    "--fs-caption",
    "--radius",
    "--radius-full",
    "--radius-card",
    "--bw-thin",
    "--shadow-card",
    "--surface",
    "--fg",
    "--muted",
    "--sans",
    "--sp-2",
    "--sp-3",
    "--indent-0",
    "--touch-target-size",
)
_BROWSER_CANARY_BASE_ROOT_PROPERTIES = (
    "--fs-display",
    "--fs-title",
    "--fs-body",
    "--fs-caption",
    "--radius",
    "--radius-full",
    "--radius-card",
    "--bw-thin",
    "--touch-target-size",
)


class _RouteRequest(Protocol):
    url: str


class _CanaryRoute(Protocol):
    request: _RouteRequest

    def abort(self) -> None: ...

    def continue_(self) -> None: ...

    def fulfill(self, *, status: int, content_type: str, body: str) -> None: ...


class _CanaryPage(Protocol):
    def evaluate(self, expression: str, arg: object) -> object: ...

    def route(self, url: str, handler: object) -> None: ...

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None: ...

    def wait_for_load_state(self, state: str, *, timeout: int) -> None: ...

    def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> object: ...

    def wait_for_function(self, expression: str, *, timeout: int) -> object: ...

    def wait_for_timeout(self, timeout: int) -> None: ...

    def content(self) -> str: ...

    def set_content(self, html: str, *, wait_until: str, timeout: int) -> None: ...


class _CanaryContext(Protocol):
    def new_page(self) -> _CanaryPage: ...

    def close(self) -> None: ...


class _CanaryBrowser(Protocol):
    def new_context(
        self,
        *,
        service_workers: str,
        reduced_motion: str | None = None,
        viewport: dict[str, int] | None = None,
    ) -> _CanaryContext: ...

    def close(self) -> None: ...


class _CanaryChromium(Protocol):
    def launch(self, *, headless: bool) -> _CanaryBrowser: ...


class _CanaryPlaywright(Protocol):
    chromium: _CanaryChromium


class _CanaryPlaywrightContext(Protocol):
    def __enter__(self) -> _CanaryPlaywright: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...


class _CanaryPlaywrightModule(Protocol):
    def sync_playwright(self) -> object: ...


# The base primitives intentionally do not include the small-button variant.
# Surface-specific min-height rules are allowed (and are not a property of the
# base primitive), while font/radius/border values are stable kit contracts.
_BROWSER_CANARY_PRIMITIVES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        ".k-btn:not(.k-btn-sm)",
        (
            ("font-size", "--fs-body"),
            ("border-radius", "--radius"),
            ("border-width", "--bw-thin"),
            ("min-height", "--touch-target-size"),
        ),
    ),
    (
        ".k-chip",
        (
            ("font-size", "--fs-caption"),
            ("border-radius", "--radius-full"),
            ("border-width", "--bw-thin"),
            ("min-height", "--touch-target-size"),
        ),
    ),
    (
        ".k-card",
        (
            ("border-radius", "--radius-card"),
            ("border-width", "--bw-thin"),
            ("min-height", "--touch-target-size"),
        ),
    ),
    (".k-well", (("border-radius", "--radius"), ("min-height", "--touch-target-size"))),
    (
        ".k-overlay",
        (
            ("border-radius", "--radius"),
            ("border-width", "--bw-thin"),
            ("min-height", "--touch-target-size"),
        ),
    ),
)

# The matrix is the mandatory route set. Markup is rendered through the
# production Work OS seam by ``design_route_canaries``; it is not a second
# hand-written page registry.
_ROUTE_CANARY_MATRIX: tuple[tuple[str, str, tuple[int, int]], ...] = tuple(
    (route, viewport, size)
    for route in ROUTE_SCREEN_IDS
    for viewport, size in (("desktop", (1440, 900)), ("narrow", (390, 844)))
)
_REQUIRED_ROUTE_CANARY_KEYS = frozenset(
    (route, viewport) for route in ROUTE_SCREEN_IDS for viewport in ("desktop", "narrow")
)
_ROUTE_CANARY_SETTLE_MILLISECONDS = 400

_ROUTE_CANARY_ROLE_CONTRACTS: dict[str, tuple[tuple[str, ...], bool]] = {
    # The Work OS shell hydrates some controls/tables from API payloads. Keep
    # each contract scoped to the production route's static seam; the matrix
    # still covers every required role across the complete production census.
    "cockpit": (("container", "type", "table", "help-footnote"), False),
    "performance": (("container", "control", "type", "help-footnote"), True),
    "risk-allocations": (("container", "control", "type", "help-footnote"), True),
    "company-desk": (("container", "control", "type", "help-footnote", "overlay"), True),
    "brief-library": (("container", "control", "type", "help-footnote"), True),
    "fact-metric-playground": (("container", "control", "type", "help-footnote"), True),
    "decision-audit": (("container", "control", "type", "help-footnote"), True),
    "operations": (("container", "type", "help-footnote"), False),
    "full-brief": (("container", "control", "type", "help-footnote"), True),
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Finding(_ClosedModel):
    surface: str
    dimension: str
    values: tuple[str, ...]
    disposition: Literal["live", "quarantined", "legacy-debt"]


class StaleQuarantine(_ClosedModel):
    surface: str
    dimension: str
    reason: Literal["clean", "expired"]


class UnverifiableMarkup(_ClosedModel):
    surface: str
    values: tuple[str, ...]
    disposition: Literal["live", "legacy-debt"] = "live"


class DesignDebtBaseline(_ClosedModel):
    schema_version: Literal["1.0.0"]
    registry_version: str
    findings: tuple[str, ...]
    geometry: tuple[str, ...]
    unverifiable: tuple[str, ...]


class EmitterEvidenceReceipt(_ClosedModel):
    path: str
    adapter_kinds: tuple[str, ...]
    evidence_modes: tuple[str, ...]
    evidence: tuple[str, ...]


class EmitterMismatch(_ClosedModel):
    path: str
    reason: str


class CanaryResult(_ClosedModel):
    status: Literal[
        "skipped:not-requested",
        "skipped:unavailable",
        "passed",
        "failed",
    ]
    reason: str | None = None
    findings: tuple[str, ...] = ()
    unverifiable_markup: tuple[str, ...] = ()


class RouteCanaryResult(_ClosedModel):
    route: str
    viewport: Literal["desktop", "narrow"]
    fixture: str
    status: Literal["passed", "failed", "unavailable"]
    reason: str | None = None
    findings: tuple[str, ...] = ()


class ConformanceReceipt(_ClosedModel):
    schema_version: Literal["1.3.0"] = SCHEMA_VERSION
    registry_version: str
    checked_surfaces: tuple[str, ...]
    emitter_evidence: tuple[EmitterEvidenceReceipt, ...]
    emitter_mismatches: tuple[EmitterMismatch, ...]
    unregistered_surfaces: tuple[str, ...]
    stale_registrations: tuple[str, ...]
    findings: tuple[Finding, ...]
    unverifiable_markup: tuple[UnverifiableMarkup, ...]
    stale_quarantine: tuple[StaleQuarantine, ...]
    debt_mismatches: tuple[str, ...]
    geometry_debt_count: int
    static_status: Literal["clean", "known-debt", "known-quarantine", "failed"]
    canary: CanaryResult
    verdict: Literal["pass", "fail", "hold"]
    route_canaries: tuple[RouteCanaryResult, ...] = ()


def _canonical_json(model: BaseModel) -> str:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _emit_event(event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    sys.stderr.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def _load_debt_baseline(project_root: Path) -> DesignDebtBaseline:
    path = project_root / "tests" / "design_conformance_debt.json"
    if not path.exists():
        return DesignDebtBaseline(
            schema_version="1.0.0",
            registry_version=REGISTRY_VERSION,
            findings=(),
            geometry=(),
            unverifiable=(),
        )
    return DesignDebtBaseline.model_validate_json(path.read_text("utf-8"))


def _scan_static(
    source_root: Path,
) -> tuple[
    tuple[str, ...],
    tuple[EmitterEvidenceReceipt, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[Finding, ...],
    tuple[UnverifiableMarkup, ...],
    tuple[StaleQuarantine, ...],
    tuple[str, ...],
    int,
    tuple[EmitterMismatch, ...],
    Literal["clean", "known-debt", "known-quarantine", "failed"],
]:
    project_root = source_root.parent if source_root.name == "src" else source_root
    raw_emitters = discover_emitters(project_root)
    merged_emitters: dict[str, EmitterEvidenceReceipt] = {}
    for emitter in raw_emitters:
        prior = merged_emitters.get(emitter.path)
        merged_emitters[emitter.path] = EmitterEvidenceReceipt(
            path=emitter.path,
            adapter_kinds=tuple(
                sorted(set(emitter.adapter_kinds) | set(prior.adapter_kinds if prior else ()))
            ),
            evidence_modes=tuple(
                sorted(set(emitter.evidence_modes) | set(prior.evidence_modes if prior else ()))
            ),
            evidence=tuple(sorted(set(emitter.evidence) | set(prior.evidence if prior else ()))),
        )
    emitter_evidence = tuple(merged_emitters[path] for path in sorted(merged_emitters))
    discovered_set = frozenset(item.path for item in emitter_evidence)
    manifest_paths = frozenset(entry.path for entry in VISUAL_EMITTER_MANIFEST)
    manifest_by_path = {entry.path: entry for entry in VISUAL_EMITTER_MANIFEST}
    governed_paths = GOVERNED if (project_root / "design-system" / "src").exists() else REGISTERED
    discovered = tuple(sorted(discovered_set & governed_paths))
    unregistered = tuple(sorted(discovered_set - manifest_paths))
    stale_registrations = tuple(sorted(governed_paths - discovered_set))
    emitter_mismatches: list[EmitterMismatch] = []
    for observed in emitter_evidence:
        contract = manifest_by_path.get(observed.path)
        if contract is None:
            continue
        declared_adapters = {str(item) for item in contract.adapter_kinds}
        unexpected_adapters = set(observed.adapter_kinds) - declared_adapters
        if unexpected_adapters:
            emitter_mismatches.append(
                EmitterMismatch(
                    path=observed.path,
                    reason=f"undeclared adapters: {','.join(sorted(unexpected_adapters))}",
                )
            )
        unobserved_adapters = declared_adapters - set(observed.adapter_kinds)
        if unobserved_adapters:
            emitter_mismatches.append(
                EmitterMismatch(
                    path=observed.path,
                    reason=f"unobserved adapters: {','.join(sorted(unobserved_adapters))}",
                )
            )
        declared_modes = {str(item) for item in contract.evidence_modes}
        unexpected_modes = set(observed.evidence_modes) - declared_modes
        if unexpected_modes:
            emitter_mismatches.append(
                EmitterMismatch(
                    path=observed.path,
                    reason=f"undeclared evidence modes: {','.join(sorted(unexpected_modes))}",
                )
            )
    ordered_emitter_mismatches = tuple(
        sorted(emitter_mismatches, key=lambda item: (item.path, item.reason))
    )
    debt_baseline = _load_debt_baseline(project_root)
    baseline_findings = frozenset(debt_baseline.findings)
    baseline_unverifiable = frozenset(debt_baseline.unverifiable)

    today = date.today()
    quarantine = {(entry.surface, entry.dimension): entry for entry in QUARANTINE_ENTRIES}
    scanned: dict[str, dict[str, list[str]]] = {}
    findings: list[Finding] = []
    unverifiable_markup: list[UnverifiableMarkup] = []
    observed_finding_debt: set[str] = set()
    observed_unverifiable_debt: set[str] = set()
    observed_geometry_debt: list[str] = []
    for surface in discovered:
        surface_path = source_root / surface
        if not surface_path.exists():
            surface_path = project_root / surface
        text = (
            css_text(surface_path)
            if surface_path.suffix == ".py"
            else surface_path.read_text("utf-8")
        )
        evidence = scan_surface_evidence(surface, text)
        observed_geometry_debt.extend(geometry_debt_fingerprints(surface, text))
        violations = evidence.violations()
        scanned[surface] = violations
        if evidence.unverifiable_markup:
            legacy_values: list[str] = []
            live_values: list[str] = []
            for value in evidence.unverifiable_markup:
                identity = unverifiable_debt_id(surface, value)
                observed_unverifiable_debt.add(identity)
                (legacy_values if identity in baseline_unverifiable else live_values).append(value)
            if legacy_values:
                unverifiable_markup.append(
                    UnverifiableMarkup(
                        surface=surface,
                        values=tuple(legacy_values),
                        disposition="legacy-debt",
                    )
                )
            if live_values:
                unverifiable_markup.append(
                    UnverifiableMarkup(surface=surface, values=tuple(live_values))
                )
        for dimension, raw_values in sorted(violations.items()):
            entry = quarantine.get((surface, dimension))
            legacy_values = []
            live_values = []
            for value in sorted(set(raw_values)):
                identity = finding_debt_id(surface, dimension, value)
                observed_finding_debt.add(identity)
                (legacy_values if identity in baseline_findings else live_values).append(value)
            if legacy_values:
                findings.append(
                    Finding(
                        surface=surface,
                        dimension=dimension,
                        values=tuple(legacy_values),
                        disposition="legacy-debt",
                    )
                )
            disposition: Literal["live", "quarantined", "legacy-debt"] = "live"
            if entry is not None and entry.expires_on >= today:
                disposition = "quarantined"
            if live_values:
                findings.append(
                    Finding(
                        surface=surface,
                        dimension=dimension,
                        values=tuple(live_values),
                        disposition=disposition,
                    )
                )

    stale_quarantine: list[StaleQuarantine] = []
    for entry in sorted(
        QUARANTINE_ENTRIES,
        key=lambda item: (item.surface, item.dimension),
    ):
        reason: Literal["clean", "expired"] | None = None
        if entry.expires_on < today:
            reason = "expired"
        elif entry.dimension not in scanned.get(entry.surface, {}):
            reason = "clean"
        if reason is not None:
            stale_quarantine.append(
                StaleQuarantine(
                    surface=entry.surface,
                    dimension=entry.dimension,
                    reason=reason,
                )
            )

    ordered_findings = tuple(sorted(findings, key=lambda item: (item.surface, item.dimension)))
    ordered_unverifiable = tuple(sorted(unverifiable_markup, key=lambda item: item.surface))
    ordered_stale = tuple(stale_quarantine)
    debt_mismatches = geometry_debt_failures(observed_geometry_debt, debt_baseline.geometry)
    debt_mismatches.extend(
        f"new finding debt: {item}" for item in sorted(observed_finding_debt - baseline_findings)
    )
    debt_mismatches.extend(
        f"stale finding debt: {item}" for item in sorted(baseline_findings - observed_finding_debt)
    )
    debt_mismatches.extend(
        f"new unverifiable debt: {item}"
        for item in sorted(observed_unverifiable_debt - baseline_unverifiable)
    )
    debt_mismatches.extend(
        f"stale unverifiable debt: {item}"
        for item in sorted(baseline_unverifiable - observed_unverifiable_debt)
    )
    if debt_baseline.registry_version != REGISTRY_VERSION:
        debt_mismatches.append(
            "design debt registry version "
            f"{debt_baseline.registry_version!r} != {REGISTRY_VERSION!r}"
        )
    ordered_debt_mismatches = tuple(sorted(debt_mismatches))
    has_live = any(item.disposition == "live" for item in ordered_findings)
    has_live_unverifiable = any(item.disposition == "live" for item in ordered_unverifiable)
    static_failed = bool(
        has_live
        or has_live_unverifiable
        or unregistered
        or stale_registrations
        or ordered_stale
        or ordered_debt_mismatches
        or ordered_emitter_mismatches
    )
    if static_failed:
        static_status: Literal["clean", "known-debt", "known-quarantine", "failed"] = "failed"
    elif any(item.disposition == "quarantined" for item in ordered_findings):
        static_status = "known-quarantine"
    elif (
        any(item.disposition == "legacy-debt" for item in ordered_findings)
        or any(item.disposition == "legacy-debt" for item in ordered_unverifiable)
        or observed_geometry_debt
    ):
        static_status = "known-debt"
    else:
        static_status = "clean"
    return (
        discovered,
        emitter_evidence,
        unregistered,
        stale_registrations,
        ordered_findings,
        ordered_unverifiable,
        ordered_stale,
        ordered_debt_mismatches,
        len(observed_geometry_debt),
        ordered_emitter_mismatches,
        static_status,
    )


def _validate_canary_url(canary_url: str) -> None:
    parsed_url = urllib.parse.urlsplit(canary_url)
    if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname is None:
        raise ValueError("canary URL must use http or https")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError("canary URL must not contain credentials")
    # Accessing ``port`` validates malformed bracketed/numbered authorities.
    # Do this before either transport is started so both canaries share the
    # same URL safety boundary.
    _ = parsed_url.port


def _canary_origin(url: str) -> tuple[str, str, int]:
    parsed_url = urllib.parse.urlsplit(url)
    scheme = parsed_url.scheme.lower()
    hostname = (parsed_url.hostname or "").lower()
    port = parsed_url.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _browser_canary_findings(
    page: _CanaryPage,
    *,
    expected_route: str | None = None,
    required_roles: Sequence[str] = (),
    require_keyboard: bool = True,
) -> tuple[str, ...]:
    """Return deterministic computed-style mismatches from a Playwright page."""

    # Keep DOM inspection in the browser process.  In particular, reading
    # page.content() alone cannot observe CSSOM rules or custom-property
    # mutations made by JavaScript after the original response was received.
    payload: object = page.evaluate(
        """
          ({rootProperties, primitiveContracts, cardContracts, expectedRoute, requiredRoles, requireKeyboard}) => {
          const root = getComputedStyle(document.documentElement);
          const rootValues = Object.fromEntries(
            rootProperties.map((name) => [name, root.getPropertyValue(name).trim()])
          );
          const findings = [];
          const routeRoot = document.querySelector('[data-conformance-route]');
          if (expectedRoute && !routeRoot) {
            findings.push({kind: "route", actual: "missing data-conformance-route root"});
          } else if (expectedRoute && routeRoot.getAttribute('data-conformance-route') !== expectedRoute) {
            findings.push({kind: "route", actual: `expected ${expectedRoute}, got ${routeRoot.getAttribute('data-conformance-route')}`});
          }
          const inspectInline = (node, selector, index) => {
            // Inline declarations are outside the canonical primitive CSS;
            // inspect every declaration, including custom properties, rather
            // than relying on a finite visual-property allowlist.
            for (let offset = 0; offset < node.style.length; offset += 1) {
              const property = node.style.item(offset);
              const actual = node.style.getPropertyValue(property).trim();
              if (actual) {
                findings.push({kind: "inline", selector, index, property, actual});
              }
            }
          };
          inspectInline(document.documentElement, "document.documentElement", 0);
          const composedParent = (node) => node.parentElement ||
            (node.getRootNode && node.getRootNode().host instanceof Element
              ? node.getRootNode().host : null);
          const composedElements = (rootNode) => {
            const nodes = [];
            const visit = (node) => {
              if (!(node instanceof Element)) return;
              nodes.push(node);
              if (node.shadowRoot) [...node.shadowRoot.children].forEach(visit);
              [...node.children].forEach(visit);
            };
            visit(rootNode);
            return nodes;
          };
          const composedClosest = (node, selector) => {
            for (let current = node; current; current = composedParent(current)) {
              if (current.matches && current.matches(selector)) return current;
            }
            return null;
          };
          const composedContains = (container, node) => {
            for (let current = node; current; current = composedParent(current)) {
              if (current === container) return true;
            }
            return false;
          };
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            for (let current = node; current; current = composedParent(current)) {
              if (current.matches && current.matches('details:not([open])')) {
                const summary = [...current.children]
                  .find((child) => child.tagName === 'SUMMARY');
                if (!summary || !composedContains(summary, node)) return false;
              }
              const style = getComputedStyle(current);
              if (style.display === 'none' || style.visibility === 'hidden' ||
                  Number.parseFloat(style.opacity || '1') <= 0) return false;
            }
            return true;
          };
          const scopedMatches = (selector) => {
            if (!expectedRoute || !routeRoot) return [...document.querySelectorAll(selector)];
            return composedElements(routeRoot).filter((node) => node.matches(selector));
          };
          const composedDescendants = (node, selector = '*') =>
            composedElements(node).slice(1).filter((candidate) => candidate.matches(selector));
          for (const contract of primitiveContracts) {
            const [selector, checks] = contract;
            const nodes = scopedMatches(selector);
            nodes.forEach((node, index) => {
              if (expectedRoute && !visible(node)) return;
              inspectInline(node, selector, index);
              const computed = getComputedStyle(node);
              for (const [property, variable] of checks) {
                const expected = rootValues[variable] || "";
                const actual = computed.getPropertyValue(property).trim();
                if (!expected) {
                  continue;
                }
                // Base primitives have no min-height declaration; their
                // browser initial value is not a component contract.  When a
                // surface or runtime rule supplies a real minimum, it must
                // use the canonical touch-target token.
                if (property === "min-height" && (actual === "0px" || actual === "auto")) {
                  continue;
                }
                if (actual !== expected) {
                  findings.push({kind: "computed", selector, index, property, actual, variable, expected});
                }
              }
            });
          }
          const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
          if (expectedRoute && routeRoot) {
            const tokenCache = new Map();
            const resolveToken = (variable, property) => {
              const key = `${variable}:${property}`;
              if (tokenCache.has(key)) return tokenCache.get(key);
              const probe = document.createElement('span');
              probe.style.position = 'fixed';
              probe.style.visibility = 'hidden';
              if (property === 'border-width') probe.style.borderStyle = 'solid';
              if (property === 'gap') probe.style.display = 'grid';
              probe.style.setProperty(property, `var(${variable})`);
              document.body.appendChild(probe);
              const resolved = getComputedStyle(probe).getPropertyValue(property).trim();
              probe.remove();
              tokenCache.set(key, resolved);
              return resolved;
            };
            const normalized = (value) => value.replace(/[\"']/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const cards = scopedMatches('.k-card').filter(visible);
            if (!cards.length) findings.push({kind: 'card', actual: 'route has no visible typed cards'});
            cards.forEach((card, index) => {
              const matches = cardContracts.filter((contract) => card.matches(contract.selector));
              if (matches.length !== 1) {
                findings.push({kind: 'card', actual: `card[${index}] must match exactly one archetype; matched ${matches.length}`});
                return;
              }
              const contract = matches[0];
              const style = getComputedStyle(card);
              const rect = card.getBoundingClientRect();
              const compare = (property, variable) => {
                const actual = style.getPropertyValue(property).trim();
                const expected = resolveToken(variable, property);
                if (normalized(actual) !== normalized(expected)) {
                  findings.push({kind: 'card', actual: `card[${index}] ${property} ${actual} != ${variable} ${expected}`});
                }
              };
              compare('padding-top', contract.paddingBlockToken);
              compare('padding-bottom', contract.paddingBlockToken);
              compare('padding-left', contract.paddingInlineToken);
              compare('padding-right', contract.paddingInlineToken);
              compare('border-radius', '--radius-card');
              compare('border-width', '--bw-thin');
              compare('box-shadow', '--shadow-card');
              compare('background-color', '--surface');
              if (rect.left < -0.5 || rect.right > window.innerWidth + 0.5) {
                findings.push({kind: 'card', actual: `card[${index}] overflows viewport`});
              }
              if (rect.bottom > 0 && rect.top < window.innerHeight &&
                  rect.right > 0 && rect.left < window.innerWidth) {
                const centerX = Math.min(window.innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
                const centerY = Math.min(window.innerHeight - 1, Math.max(0, rect.top + Math.min(rect.height / 2, window.innerHeight / 3)));
                const hitRoot = card.getRootNode();
                const hit = hitRoot && typeof hitRoot.elementFromPoint === 'function'
                  ? hitRoot.elementFromPoint(centerX, centerY)
                  : document.elementFromPoint(centerX, centerY);
                const coveringOverlay = hit
                  ? composedClosest(hit, '[data-conformance-role="overlay"]')
                  : null;
                if ((!hit || !composedContains(card, hit)) && !coveringOverlay) {
                  const hitLabel = hit
                    ? `${hit.tagName.toLowerCase()}${hit.id ? `#${hit.id}` : ''}`
                    : 'nothing';
                  findings.push({kind: 'card', actual: `card[${index}] is occluded at its visible center by ${hitLabel}`});
                }
              }
              if (contract.titleSelector) {
                const titles = composedDescendants(card, contract.titleSelector)
                  .filter(visible)
                  .filter((title) => composedClosest(title, '.k-card') === card);
                if (!titles.length) {
                  findings.push({kind: 'card', actual: `card[${index}] missing visible title ${contract.titleSelector}`});
                } else {
                  titles.forEach((title, titleIndex) => {
                    const titleStyle = getComputedStyle(title);
                    const titleRect = title.getBoundingClientRect();
                    const titleFloor = rect.top + Math.min(rect.height / 2, 80);
                    if (contract.name !== 'stat' &&
                        (titleRect.top < rect.top - 0.5 || titleRect.top > titleFloor)) {
                      findings.push({kind: 'card', actual: `card[${index}] title[${titleIndex}] is not in the upper title zone`});
                    }
                    const checks = [
                      ['font-size', contract.titleSizeToken],
                      ['font-family', contract.titleFamilyToken],
                      ['color', contract.titleColorToken],
                    ];
                    checks.forEach(([property, variable]) => {
                      const actual = titleStyle.getPropertyValue(property).trim();
                      const expected = resolveToken(variable, property);
                      if (normalized(actual) !== normalized(expected)) {
                        findings.push({kind: 'card', actual: `card[${index}] title[${titleIndex}] ${property} ${actual} != ${variable} ${expected}`});
                      }
                    });
                    if (Number.parseInt(titleStyle.fontWeight, 10) !== contract.titleWeight) {
                      findings.push({kind: 'card', actual: `card[${index}] title[${titleIndex}] font-weight ${titleStyle.fontWeight} != ${contract.titleWeight}`});
                    }
                  });
                }
              } else if (!card.getAttribute('aria-label') && !card.getAttribute('aria-labelledby')) {
                findings.push({kind: 'card', actual: `card[${index}] navigation card has no accessible name`});
              }
              composedDescendants(card, '.k-card-head')
                .filter((head) => composedClosest(head, '.k-card') === card)
                .forEach((head) => {
                const headStyle = getComputedStyle(head);
                const expectedGap = resolveToken('--sp-3', 'gap');
                if (headStyle.display !== 'flex' ||
                    (window.innerWidth > 760 && headStyle.alignItems !== 'flex-start') ||
                    headStyle.justifyContent !== 'space-between' ||
                    normalized(headStyle.gap) !== normalized(expectedGap)) {
                  findings.push({kind: 'card', actual: `card[${index}] header alignment violates canonical anatomy`});
                }
              });
              if (card.matches('.research-toolbar')) {
                const expectedDirection = window.innerWidth > 760 ? 'row' : 'column';
                const expectedAlign = window.innerWidth > 760 ? 'center' : 'stretch';
                if (style.display !== 'flex' || style.flexDirection !== expectedDirection ||
                    style.alignItems !== expectedAlign || style.justifyContent !== 'space-between') {
                  findings.push({kind: 'card', actual: `card[${index}] toolbar title/action alignment violates the responsive contract`});
                }
              }
              if (reduced) {
                [card, ...composedDescendants(card)].filter(visible).forEach((node) => {
                  const nodeStyle = getComputedStyle(node);
                  const durations = `${nodeStyle.transitionDuration},${nodeStyle.animationDuration}`
                    .split(',').map((value) => value.trim()).filter(Boolean)
                    .map((value) => value.endsWith('ms') ? Number.parseFloat(value) : Number.parseFloat(value) * 1000);
                  if (durations.some((duration) => duration > 1)) {
                    findings.push({kind: 'motion', actual: `card[${index}] descendant motion is not reduced`});
                  }
                });
              }
            });
            const visualCandidates = scopedMatches('article,section,aside,details,li,[role="group"],[role="region"],[class*="card" i],[class*="panel" i],[class*="tile" i],[class*="box" i]')
              .filter(visible)
              .filter((node) => !node.matches('.k-card,.k-well,.k-overlay,.k-table-shell'));
            visualCandidates.forEach((node, index) => {
              const style = getComputedStyle(node);
              const boxed = style.boxShadow !== 'none' || Number.parseFloat(style.borderTopWidth) > 0 ||
                (!['rgba(0, 0, 0, 0)', 'transparent'].includes(style.backgroundColor) && Number.parseFloat(style.borderRadius) > 0);
              const reportDocument = composedClosest(node, '.k-doc');
              if (boxed && reportDocument &&
                  reportDocument.getAttribute('data-conformance-card-exemption') !== 'editorial-document') {
                findings.push({kind: 'card', actual: `boxed report document subtree lacks explicit exemption receipt`});
              } else if (boxed && !reportDocument) {
                findings.push({kind: 'card', actual: `unregistered boxed card candidate[${index}] ${node.className}`});
              }
            });
          }
          const roles = expectedRoute && routeRoot
            ? scopedMatches('[data-conformance-role]')
            : [...document.querySelectorAll('[data-conformance-role]')];
          const unresolvedLoading = scopedMatches('.cc-loading,[aria-busy="true"],[hx-get]')
            .filter(visible)
            .filter((node) => node.hasAttribute('hx-get') ||
              node.getAttribute('aria-busy') === 'true' ||
              /^\\s*Loading\\b/i.test(node.textContent || ''));
          unresolvedLoading.forEach((node, index) => {
            const marker = node.hasAttribute('hx-get') ? 'hx-get' :
              (node.getAttribute('aria-busy') === 'true' ? 'aria-busy' : 'loading text');
            findings.push({kind: 'dynamic',
              actual: `unresolved visible loading shell[${index}] (${marker})`});
          });
          if (expectedRoute && !roles.length) {
            findings.push({kind: "role", actual: "no registered conformance roles"});
          }
          requiredRoles.forEach((role) => {
            if (expectedRoute && !routeRoot?.matches(`[data-conformance-role="${role}"]`)
                && !scopedMatches(`[data-conformance-role="${role}"]`).length) {
              findings.push({kind: "role", actual: `missing required role ${role}`});
            }
          });
          const scopedRoles = routeRoot
            ? [routeRoot, ...scopedMatches('[data-conformance-role]').filter((node) => node !== routeRoot)]
            : [...roles];
          scopedRoles.forEach((node, index) => {
            const role = node.getAttribute('data-conformance-role') || '';
            if (!role) return;
            if (!role || !visible(node)) {
              findings.push({kind: "role", actual: `${role || 'unnamed'}[${index}] is not visible`});
            }
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            if (role === 'control' && node.matches('.k-btn')
                && rootValues['--radius'] && style.borderRadius !== rootValues['--radius']) {
              findings.push({kind: "computed", selector: '[data-conformance-role="control"]', index,
                property: 'border-radius', actual: style.borderRadius, variable: '--radius', expected: rootValues['--radius']});
            }
            if (role === 'type') {
              const allowedSizes = ['--fs-display', '--fs-title', '--fs-body', '--fs-caption']
                .map((name) => rootValues[name]).filter(Boolean);
              if (!allowedSizes.includes(style.fontSize)) {
                findings.push({kind: "role", actual: `type[${index}] uses off-scale font-size ${style.fontSize}`});
              }
            }
            let horizontalScrollContained = false;
            let verticalScrollContained = false;
            for (let parent = node.parentElement; parent && role !== 'container'; parent = parent.parentElement) {
              if (parent === document.body || parent === document.documentElement) continue;
              const parentStyle = getComputedStyle(parent);
              horizontalScrollContained ||= /(auto|scroll)/.test(parentStyle.overflowX);
              verticalScrollContained ||= /(auto|scroll)/.test(parentStyle.overflowY);
              if (!/(hidden|clip)/.test(parentStyle.overflow + ' ' + parentStyle.overflowX + ' ' + parentStyle.overflowY)) continue;
              const parentRect = parent.getBoundingClientRect();
              const clippedHorizontally = !horizontalScrollContained &&
                (rect.left < parentRect.left || rect.right > parentRect.right);
              const clippedVertically = !verticalScrollContained &&
                (rect.top < parentRect.top || rect.bottom > parentRect.bottom);
              if (clippedHorizontally || clippedVertically) {
                findings.push({kind: "geometry", actual: `${role}[${index}] clipped by ${parent.tagName.toLowerCase()}`});
                break;
              }
            }
            if (role === 'overlay') {
              if (rect.left < 0 || rect.right > window.innerWidth ||
                  rect.top < 0 || rect.bottom > window.innerHeight) {
                findings.push({kind: "geometry", actual: `overlay[${index}] clipped by viewport`});
              }
              const centerX = Math.min(window.innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
              const centerY = Math.min(window.innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
              const hit = document.elementFromPoint(centerX, centerY);
              if (!hit || !(hit === node || node.contains(hit))) {
                findings.push({kind: "geometry", actual: `overlay[${index}] occluded`});
              }
              if (style.position !== 'fixed' && style.position !== 'absolute') {
                findings.push({kind: "geometry", actual: `overlay[${index}] is not positioned`});
              }
            }
          });
          const focusables = (routeRoot
            ? scopedMatches('a[href],button,input,select,textarea,[tabindex]')
            : [...document.querySelectorAll('a[href],button,input,select,textarea,[tabindex]')])
            .filter((node) => visible(node) && !node.hasAttribute('disabled') && node.getAttribute('tabindex') !== '-1');
          if (expectedRoute && requireKeyboard && !focusables.length) {
            findings.push({kind: "keyboard", actual: "no visible keyboard target"});
          }
          if (expectedRoute) {
            focusables.forEach((node, index) => {
              node.focus({preventScroll: true});
              let active = document.activeElement;
              while (active && active.shadowRoot && active.shadowRoot.activeElement) {
                active = active.shadowRoot.activeElement;
              }
              if (active !== node) {
                findings.push({kind: "keyboard", actual: `focus target[${index}] is unreachable`});
              }
            });
          }
          if (expectedRoute && !reduced) {
            findings.push({kind: "motion", actual: "reduced-motion media query is not active"});
          }
          document.querySelectorAll('[data-conformance-motion]').forEach((node, index) => {
            const style = getComputedStyle(node);
            if (expectedRoute && reduced && (style.transitionDuration !== '0s' || style.animationDuration !== '0s')) {
              findings.push({kind: "motion", actual: `motion[${index}] is not reduced`});
            }
          });
          document.querySelectorAll('[data-conformance-dynamic]').forEach((node, index) => {
            if (expectedRoute && node.getAttribute('data-conformance-dynamic-state') !== 'ready') {
              findings.push({kind: "dynamic", actual: `dynamic[${index}] did not settle`});
            }
          });
          return {rootValues, findings};
        }
        """,
        {
            "rootProperties": list(_BROWSER_CANARY_ROOT_PROPERTIES),
            "expectedRoute": expected_route,
            "requiredRoles": list(required_roles),
            "requireKeyboard": require_keyboard,
            "primitiveContracts": [
                [selector, [list(check) for check in checks]]
                for selector, checks in _BROWSER_CANARY_PRIMITIVES
            ],
            "cardContracts": [
                {
                    "name": contract.name,
                    "selector": contract.selector,
                    "paddingBlockToken": f"--{contract.padding_block_token}",
                    "paddingInlineToken": f"--{contract.padding_inline_token}",
                    "titleSelector": contract.title_selector,
                    "titleSizeToken": (
                        f"--{contract.title_size_token}" if contract.title_size_token else None
                    ),
                    "titleFamilyToken": (
                        f"--{contract.title_family_token}" if contract.title_family_token else None
                    ),
                    "titleColorToken": (
                        f"--{contract.title_color_token}" if contract.title_color_token else None
                    ),
                    "titleWeight": contract.title_weight,
                }
                for contract in CARD_ARCHETYPES
            ],
        },
    )
    if not isinstance(payload, dict):
        raise ValueError("browser canary returned invalid inspection payload")
    payload_dict = cast(dict[str, object], payload)
    root_values = payload_dict.get("rootValues")
    raw_findings = payload_dict.get("findings")
    if not isinstance(root_values, dict) or not isinstance(raw_findings, list):
        raise ValueError("browser canary returned invalid inspection payload")
    root_values_dict = cast(dict[str, object], root_values)
    raw_findings_list = cast(list[object], raw_findings)

    required_root_properties = (
        _BROWSER_CANARY_ROOT_PROPERTIES if expected_route else _BROWSER_CANARY_BASE_ROOT_PROPERTIES
    )
    findings = [
        f"root custom property missing: {name}"
        for name in required_root_properties
        if not isinstance(root_values_dict.get(name), str)
        or not cast(str, root_values_dict[name]).strip()
    ]
    for item in raw_findings_list:
        if not isinstance(item, dict):
            raise ValueError("browser canary returned invalid style finding")
        item_dict = cast(dict[str, object], item)
        selector = item_dict.get("selector", "browser")
        index = item_dict.get("index")
        property_name = item_dict.get("property")
        actual = item_dict.get("actual")
        variable = item_dict.get("variable")
        expected = item_dict.get("expected")
        if not isinstance(selector, str):
            raise ValueError("browser canary returned invalid style finding")
        if not isinstance(index, int):
            index = 0
        kind = item_dict.get("kind")
        if not isinstance(actual, str):
            raise ValueError("browser canary returned invalid style finding")
        if kind == "inline":
            if not isinstance(property_name, str):
                raise ValueError("browser canary returned invalid style finding")
            findings.append(f"{selector}[{index}] inline {property_name}: {actual!r}")
            continue
        if kind in {"route", "role", "geometry", "keyboard", "motion", "dynamic", "card"}:
            findings.append(f"{kind}: {actual!r}")
            continue
        if (
            kind != "computed"
            or not isinstance(property_name, str)
            or not isinstance(variable, str)
            or not isinstance(expected, str)
        ):
            raise ValueError("browser canary returned invalid style finding")
        findings.append(
            f"{selector}[{index}] {property_name}: computed {actual!r} != {variable} ({expected!r})"
        )
    return tuple(findings)


def _fetch_browser_canary(canary_url: str, deadline: float) -> tuple[str, tuple[str, ...]]:
    """Navigate and inspect a same-origin page with the optional Playwright adapter."""

    # Playwright is intentionally imported only for this opt-in path.  A
    # missing package or browser executable is a normal unavailable canary,
    # never a pass and never a static-scan failure.
    playwright_api = cast(
        _CanaryPlaywrightModule,
        importlib.import_module("playwright.sync_api"),
    )

    initial_origin = _canary_origin(canary_url)
    timeout_ms = max(1, int(max(0.001, deadline - time.monotonic()) * 1000))
    with cast(_CanaryPlaywrightContext, playwright_api.sync_playwright()) as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            try:
                page = context.new_page()

                def route_same_origin(route: _CanaryRoute) -> None:
                    request_origin = _canary_origin(route.request.url)
                    if request_origin != initial_origin:
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_same_origin)
                page.goto(canary_url, wait_until="domcontentloaded", timeout=timeout_ms)
                if time.monotonic() >= deadline:
                    raise TimeoutError("canary deadline expired")
                # Inline scripts and same-origin resources have run by load;
                # this keeps the result post-JS without waiting unboundedly for
                # long-polling/network-idle pages.
                page.wait_for_load_state("load", timeout=timeout_ms)
                remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError("canary deadline expired")
                # Catch delayed script/CSSOM mutations (including the ~250 ms
                # adversarial fixture) within the overall canary deadline.
                page.wait_for_timeout(min(_BROWSER_CANARY_SETTLE_MILLISECONDS, remaining_ms))
                if time.monotonic() >= deadline:
                    raise TimeoutError("canary deadline expired")
                html = page.content()
                if len(html.encode("utf-8")) > _CANARY_READ_LIMIT:
                    raise ValueError("canary response exceeded read limit")
                findings = _browser_canary_findings(page)
                return html, findings
            finally:
                context.close()
        finally:
            browser.close()


def _route_fixture_path(project_root: Path, route: str, viewport: str) -> Path:
    return project_root / "tests" / "fixtures" / "design_canaries" / f"{route}.{viewport}.html"


def _route_canary_source(route: str, viewport: str, fixture_root: Path | None) -> str:
    """Render production output unless a test explicitly supplies an isolated fixture root."""

    if fixture_root is None:
        return render_route_canary(route=route, viewport=viewport)
    override = _route_fixture_path(fixture_root, route, viewport)
    if not override.is_file():
        raise FileNotFoundError(override)
    return override.read_text("utf-8")


def _scan_route_canaries(
    project_root: Path = PROJECT_ROOT,
    *,
    fixture_root: Path | None = None,
) -> tuple[RouteCanaryResult, ...]:
    """Inspect production-rendered route output at desktop and narrow widths.

    The normal path always renders directly through the production Work OS
    seam. Tests may opt into an isolated fixture root for adversarial mutation;
    committed or stale fixture files can never replace the hosted CLI input.
    """

    fixtures: list[tuple[str, str, Path, tuple[int, int]]] = []
    for route, viewport, size in _ROUTE_CANARY_MATRIX:
        path = _route_fixture_path(project_root, route, viewport)
        fixtures.append((route, viewport, path, size))

    try:
        playwright_api = cast(
            _CanaryPlaywrightModule,
            importlib.import_module("playwright.sync_api"),
        )
        html_by_fixture = {
            path: _route_canary_source(route, viewport, fixture_root)
            for route, viewport, path, _size in fixtures
        }
        with cast(_CanaryPlaywrightContext, playwright_api.sync_playwright()) as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                results: list[RouteCanaryResult] = []
                for route, viewport, path, size in fixtures:
                    context: _CanaryContext | None = None
                    fixture_name = path.relative_to(project_root).as_posix()
                    try:
                        context = browser.new_context(
                            service_workers="block",
                            reduced_motion="reduce",
                            viewport={"width": size[0], "height": size[1]},
                        )
                        page = context.new_page()
                        canary_url = f"http://design-canary.local/{route}/{viewport}"

                        def serve_route(
                            request_route: _CanaryRoute,
                            _request: object,
                            expected_url: str = canary_url,
                            response_body: str = html_by_fixture[path],
                        ) -> None:
                            if request_route.request.url == expected_url:
                                request_route.fulfill(
                                    status=200,
                                    content_type="text/html; charset=utf-8",
                                    body=response_body,
                                )
                            else:
                                request_route.abort()

                        page.route("**/*", serve_route)
                        page.goto(canary_url, wait_until="load", timeout=5000)
                        if route == "full-brief":
                            page.wait_for_selector(
                                "#workOsBriefReader .work-os-report-host",
                                state="visible",
                                timeout=5000,
                            )
                            page.wait_for_function(
                                "document.querySelectorAll('#workOsBriefReaderSections .work-os-reader-group-button').length === 6",
                                timeout=5000,
                            )
                        settled_selectors = {
                            "cockpit": "#workOsActionQueue .k-card-action",
                            "performance": "#workOsPerformanceMount .performance-risk-panel",
                            "risk-allocations": "#workOsAllocationMount .portfolio-health-console",
                            "company-desk": "#deskCompanyName",
                            "brief-library": "#workOsBriefLibrary [data-artifact-id]",
                            "fact-metric-playground": "#workOsFactPlayground #vx-root",
                            "decision-audit": "#workOsAuditMount .portfolio-record-console",
                            "operations": "#workOsOperationsMount .operations-panel",
                        }
                        page.evaluate(
                            """
                            ({route, screenId}) => {
                              if (screenId.startsWith('screen-') && typeof window.navigateTo === 'function') {
                                window.navigateTo(screenId, {fromHistory: true});
                              }
                              const target = document.getElementById(screenId);
                              if (!target) throw new Error(`missing production screen ${screenId}`);
                              document.querySelectorAll('[data-conformance-role]').forEach((node) => node.removeAttribute('data-conformance-role'));
                              target.setAttribute('data-conformance-route', route);
                              const mark = (node, role) => {
                                if (node) node.setAttribute('data-conformance-role', role);
                                return node;
                              };
                              const firstVisible = (selector) => Array.from(target.querySelectorAll(selector))
                                .find((node) => { const rect = node.getBoundingClientRect(); return rect.width > 0 && rect.height > 0; }) || null;
                              mark(target, 'container');
                              mark(firstVisible('input, select, a[href], button.k-btn-primary:not(.k-btn-sm), button.k-btn-sm, button'), 'control');
                              mark(firstVisible('.k-card-title, .stat-number, h1, h2, h3'), 'type');
                              mark(firstVisible('table'), 'table');
                              mark(firstVisible('.stat-subtext, .k-card-meta, small'), 'help-footnote');
                              if (route === 'company-desk') {
                                const trigger = target.querySelector('#companyPickerTrigger');
                                if (trigger) trigger.click();
                              }
                              const overlay = target.querySelector('#drillDrawer, #tradeModal, .company-picker-popover');
                              if (overlay) {
                                overlay.setAttribute('data-conformance-role', 'overlay');
                              }
                              const dynamic = target.querySelector('[aria-live], [role="status"], [aria-busy]') || target;
                              dynamic.setAttribute('data-conformance-dynamic', '1');
                              dynamic.setAttribute('data-conformance-dynamic-state', 'pending');
                              window.setTimeout(() => dynamic.setAttribute('data-conformance-dynamic-state', 'ready'), 80);
                              const motion = firstVisible('.sidebar-collapse-toggle, .company-picker-trigger, .k-overlay, .drill-drawer, .report-sidebar-toggle');
                              if (motion) motion.setAttribute('data-conformance-motion', '1');
                              const reportHost = target.querySelector('.work-os-report-host');
                              const reportDocument = reportHost && reportHost.shadowRoot
                                ? reportHost.shadowRoot.querySelector('.k-doc') : null;
                              if (reportDocument) {
                                reportDocument.setAttribute('data-conformance-card-exemption', 'editorial-document');
                              }
                            }
                            """,
                            {"route": route, "screenId": ROUTE_SCREEN_IDS[route]},
                        )
                        settled_selector = settled_selectors.get(route)
                        if settled_selector:
                            page.wait_for_selector(
                                settled_selector,
                                state="visible",
                                timeout=5000,
                            )
                        if route == "company-desk":
                            page.wait_for_function(
                                "document.getElementById('deskCompanyName')?.textContent === 'Canary Company'",
                                timeout=5000,
                            )
                        page.evaluate(
                            """
                            ({route, screenId}) => {
                              const target = document.getElementById(screenId);
                              if (!target) throw new Error(`missing settled production screen ${screenId}`);
                              if (route === 'performance') {
                                target.querySelectorAll('.pf-alpha-details').forEach((details) => {
                                  details.open = true;
                                  details.setAttribute('data-conformance-state', 'expanded');
                                });
                              }
                              document.querySelectorAll('[data-conformance-role]').forEach((node) => node.removeAttribute('data-conformance-role'));
                              const firstVisible = (selector) => Array.from(target.querySelectorAll(selector))
                                .find((node) => { const rect = node.getBoundingClientRect(); return rect.width > 0 && rect.height > 0; }) || null;
                              const mark = (node, role) => { if (node) node.setAttribute('data-conformance-role', role); };
                              target.setAttribute('data-conformance-route', route);
                              mark(target, 'container');
                              mark(firstVisible('input, select, a[href], button.k-btn-primary:not(.k-btn-sm), button.k-btn-sm, button'), 'control');
                              mark(firstVisible('.k-card-title, .stat-number, h1, h2, h3'), 'type');
                              mark(firstVisible('table'), 'table');
                              mark(firstVisible('.stat-subtext, .k-card-meta, small'), 'help-footnote');
                              const overlay = target.querySelector('#drillDrawer, #tradeModal, .company-picker-popover');
                              mark(overlay, 'overlay');
                              const dynamic = target.querySelector('[aria-live], [role="status"], [aria-busy]') || target;
                              dynamic.setAttribute('data-conformance-dynamic', '1');
                              dynamic.setAttribute('data-conformance-dynamic-state', 'ready');
                              const reportHost = target.querySelector('.work-os-report-host');
                              const reportDocument = reportHost && reportHost.shadowRoot
                                ? reportHost.shadowRoot.querySelector('.k-doc') : null;
                              if (reportDocument) {
                                reportDocument.setAttribute('data-conformance-card-exemption', 'editorial-document');
                              }
                            }
                            """,
                            {"route": route, "screenId": ROUTE_SCREEN_IDS[route]},
                        )
                        page.wait_for_timeout(_ROUTE_CANARY_SETTLE_MILLISECONDS)
                        required_roles, require_keyboard = _ROUTE_CANARY_ROLE_CONTRACTS[route]
                        findings = _browser_canary_findings(
                            page,
                            expected_route=route,
                            required_roles=required_roles,
                            require_keyboard=require_keyboard,
                        )
                        results.append(
                            RouteCanaryResult(
                                route=route,
                                viewport=cast(Literal["desktop", "narrow"], viewport),
                                fixture=fixture_name,
                                status="failed" if findings else "passed",
                                findings=findings,
                            )
                        )
                    except Exception as exc:
                        results.append(
                            RouteCanaryResult(
                                route=route,
                                viewport=cast(Literal["desktop", "narrow"], viewport),
                                fixture=fixture_name,
                                status="unavailable",
                                reason=f"{type(exc).__name__}: {exc}",
                            )
                        )
                    finally:
                        if context is not None:
                            context.close()
                return tuple(results)
            finally:
                browser.close()
    except Exception as exc:
        reason = f"{type(exc).__name__}: route canary unavailable"
        return tuple(
            RouteCanaryResult(
                route=route,
                viewport=cast(Literal["desktop", "narrow"], viewport),
                fixture=path.relative_to(project_root).as_posix(),
                status="unavailable",
                reason=reason,
            )
            for route, viewport, path, _size in fixtures
        )


def _route_population_failures(
    results: tuple[RouteCanaryResult, ...],
) -> tuple[RouteCanaryResult, ...]:
    """Fail closed unless results contain the complete immutable route census."""

    keys = tuple((result.route, result.viewport) for result in results)
    actual = frozenset(keys)
    failures: list[RouteCanaryResult] = []
    for route, viewport in sorted(_REQUIRED_ROUTE_CANARY_KEYS - actual):
        failures.append(
            RouteCanaryResult(
                route=route,
                viewport=cast(Literal["desktop", "narrow"], viewport),
                fixture="<required-route-matrix>",
                status="unavailable",
                reason="required route/viewport is missing from the canary result population",
            )
        )
    for route, viewport in sorted(actual - _REQUIRED_ROUTE_CANARY_KEYS):
        failures.append(
            RouteCanaryResult(
                route=route,
                viewport=cast(Literal["desktop", "narrow"], viewport),
                fixture="<required-route-matrix>",
                status="failed",
                reason="unexpected route/viewport is outside the mandatory canary population",
            )
        )
    for route, viewport in sorted({key for key in keys if keys.count(key) > 1}):
        failures.append(
            RouteCanaryResult(
                route=route,
                viewport=cast(Literal["desktop", "narrow"], viewport),
                fixture="<required-route-matrix>",
                status="failed",
                reason="duplicate route/viewport result violates the mandatory canary population",
            )
        )
    return tuple(failures)


def _fetch_canary_html(canary_url: str, deadline: float) -> str:
    request = urllib.request.Request(
        canary_url,
        headers={"Accept": "text/html"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoCanaryRedirectHandler())
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("canary deadline expired")
    with opener.open(request, timeout=remaining) as response:
        payload = bytearray()
        while len(payload) <= _CANARY_READ_LIMIT:
            if time.monotonic() >= deadline:
                raise TimeoutError("canary deadline expired")
            read_size = min(
                _CANARY_READ_CHUNK,
                _CANARY_READ_LIMIT + 1 - len(payload),
            )
            chunk = response.read(read_size)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _CANARY_READ_LIMIT:
            raise ValueError("canary response exceeded read limit")
        charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def _scan_canary(canary_url: str | None, *, browser_canary: bool = False) -> CanaryResult:
    if canary_url is None:
        return CanaryResult(status="skipped:not-requested")

    try:
        _validate_canary_url(canary_url)
        deadline = time.monotonic() + _CANARY_WALL_TIMEOUT_SECONDS
        result_queue: queue.Queue[tuple[str, tuple[str, ...]] | str | Exception] = queue.Queue(
            maxsize=1
        )

        def fetch() -> None:
            try:
                if browser_canary:
                    result_queue.put(_fetch_browser_canary(canary_url, deadline))
                else:
                    result_queue.put(_fetch_canary_html(canary_url, deadline))
            except Exception as exc:
                result_queue.put(exc)

        threading.Thread(
            target=fetch,
            name="design-conformance-canary",
            daemon=True,
        ).start()
        try:
            result = result_queue.get(timeout=_CANARY_WALL_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise TimeoutError("canary deadline expired") from exc
        if isinstance(result, Exception):
            raise result
        browser_findings: tuple[str, ...] = ()
        if browser_canary:
            if not isinstance(result, tuple) or len(result) != 2:
                raise ValueError("browser canary returned invalid result")
            html, browser_findings = result
        else:
            if not isinstance(result, str):
                raise ValueError("canary returned invalid result")
            html = result
    except Exception as exc:
        return CanaryResult(
            status="skipped:unavailable",
            reason=f"{type(exc).__name__}: canary unavailable",
        )

    evidence = scan_surface_evidence("<canary>", html)
    title_findings = tuple(evidence.violations().get("floating-card-title", []))
    findings = tuple((*title_findings, *browser_findings))
    if findings or evidence.unverifiable_markup:
        return CanaryResult(
            status="failed",
            findings=findings,
            unverifiable_markup=evidence.unverifiable_markup,
        )
    return CanaryResult(status="passed")


def _build_receipt(
    source_root: Path,
    canary_url: str | None,
    *,
    require_canary: bool = False,
    browser_canary: bool = False,
    route_canaries: bool = False,
) -> ConformanceReceipt:
    (
        checked,
        emitter_evidence,
        unregistered,
        stale_registrations,
        findings,
        unverifiable_markup,
        stale_quarantine,
        debt_mismatches,
        geometry_debt_count,
        emitter_mismatches,
        static_status,
    ) = _scan_static(source_root)
    canary = _scan_canary(canary_url, browser_canary=browser_canary)
    scanned_route_results = _scan_route_canaries(source_root.parent) if route_canaries else ()
    route_results = (
        (*scanned_route_results, *_route_population_failures(scanned_route_results))
        if route_canaries
        else ()
    )
    failed = (
        static_status == "failed"
        or canary.status == "failed"
        or any(result.status != "passed" for result in route_results)
    )
    held = require_canary and canary.status in {
        "skipped:not-requested",
        "skipped:unavailable",
    }
    return ConformanceReceipt(
        registry_version=REGISTRY_VERSION,
        checked_surfaces=checked,
        emitter_evidence=emitter_evidence,
        emitter_mismatches=emitter_mismatches,
        unregistered_surfaces=unregistered,
        stale_registrations=stale_registrations,
        findings=findings,
        unverifiable_markup=unverifiable_markup,
        stale_quarantine=stale_quarantine,
        debt_mismatches=debt_mismatches,
        geometry_debt_count=geometry_debt_count,
        static_status=static_status,
        canary=canary,
        verdict="fail" if failed else "hold" if held else "pass",
        route_canaries=route_results,
    )


def _write_atomic(path: Path, payload: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--check",
        action="store_true",
        help="write the deterministic receipt to stdout",
    )
    modes.add_argument(
        "--emit-receipt",
        type=Path,
        metavar="PATH",
        help="atomically write the deterministic receipt to PATH",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=SRC,
        help="source tree to reconcile with the design registry (default: PROJECT_ROOT/src)",
    )
    parser.add_argument(
        "--canary-url",
        help="optional read-only rendered HTML canary URL",
    )
    parser.add_argument(
        "--browser-canary",
        action="store_true",
        help="use Playwright Chromium for bounded supplementary DOM/computed-style evidence",
    )
    parser.add_argument(
        "--require-canary",
        action="store_true",
        help="fail closed with HOLD when rendered canary evidence is unavailable",
    )
    parser.add_argument(
        "--route-canaries",
        action="store_true",
        help="run the complete production-route desktop+narrow Playwright matrix",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    source_root: Path = args.source_root.resolve()
    if not source_root.is_dir():
        parser.error(f"source root is not a directory: {source_root}")
    if args.browser_canary and args.canary_url is None:
        parser.error("--browser-canary requires --canary-url")

    try:
        receipt = _build_receipt(
            source_root,
            args.canary_url,
            require_canary=args.require_canary,
            browser_canary=args.browser_canary,
            route_canaries=args.route_canaries,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        _emit_event("design_conformance_input_error", error=type(exc).__name__)
        return 2

    _emit_event(
        "design_conformance_static_scan",
        checked_surfaces=len(receipt.checked_surfaces),
        findings=len(receipt.findings),
        status=receipt.static_status,
    )
    _emit_event("design_conformance_canary", status=receipt.canary.status)
    payload = _canonical_json(receipt)
    emit_receipt: Path | None = args.emit_receipt
    if emit_receipt is None:
        sys.stdout.write(payload)
    else:
        destination = emit_receipt.resolve()
        try:
            _write_atomic(destination, payload)
        except OSError as exc:
            _emit_event("design_conformance_receipt_error", error=type(exc).__name__)
            return 2
        summary = {
            "receipt_path": str(destination),
            "static_status": receipt.static_status,
            "verdict": receipt.verdict,
        }
        sys.stdout.write(json.dumps(summary, separators=(",", ":"), sort_keys=True) + "\n")
        _emit_event("design_conformance_receipt_written", path=str(destination))

    if receipt.verdict == "fail":
        return 1
    if receipt.verdict == "hold":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
