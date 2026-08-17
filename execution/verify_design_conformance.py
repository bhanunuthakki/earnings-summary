"""Verify registry-backed design conformance with the canonical shared scanner.

Static source scanning is authoritative.  The optional live canary is a
supplementary, read-only check of the same scanner's structural title rule.
"""

from __future__ import annotations

import argparse
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
from typing import IO, Literal

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.conformance_scan import (  # noqa: E402
    css_text,
    discover_surfaces,
    scan_surface_evidence,
)
from ui.design_registry import (  # noqa: E402
    QUARANTINE_ENTRIES,
    REGISTERED,
    REGISTRY_VERSION,
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


SCHEMA_VERSION = "1.0.0"
_CANARY_READ_LIMIT = 1_000_000
_CANARY_READ_CHUNK = 64 * 1024
_CANARY_WALL_TIMEOUT_SECONDS = 3.0


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Finding(_ClosedModel):
    surface: str
    dimension: str
    values: tuple[str, ...]
    disposition: Literal["live", "quarantined"]


class StaleQuarantine(_ClosedModel):
    surface: str
    dimension: str
    reason: Literal["clean", "expired"]


class UnverifiableMarkup(_ClosedModel):
    surface: str
    values: tuple[str, ...]


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


class ConformanceReceipt(_ClosedModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    registry_version: str
    checked_surfaces: tuple[str, ...]
    unregistered_surfaces: tuple[str, ...]
    stale_registrations: tuple[str, ...]
    findings: tuple[Finding, ...]
    unverifiable_markup: tuple[UnverifiableMarkup, ...]
    stale_quarantine: tuple[StaleQuarantine, ...]
    static_status: Literal["clean", "known-quarantine", "failed"]
    canary: CanaryResult
    verdict: Literal["pass", "fail"]


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


def _scan_static(
    source_root: Path,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[Finding, ...],
    tuple[UnverifiableMarkup, ...],
    tuple[StaleQuarantine, ...],
    Literal["clean", "known-quarantine", "failed"],
]:
    discovered = tuple(sorted(discover_surfaces(source_root)))
    discovered_set = frozenset(discovered)
    unregistered = tuple(sorted(discovered_set - REGISTERED))
    stale_registrations = tuple(sorted(REGISTERED - discovered_set))

    today = date.today()
    quarantine = {(entry.surface, entry.dimension): entry for entry in QUARANTINE_ENTRIES}
    scanned: dict[str, dict[str, list[str]]] = {}
    findings: list[Finding] = []
    unverifiable_markup: list[UnverifiableMarkup] = []
    for surface in discovered:
        evidence = scan_surface_evidence(surface, css_text(source_root / surface))
        violations = evidence.violations()
        scanned[surface] = violations
        if evidence.unverifiable_markup:
            unverifiable_markup.append(
                UnverifiableMarkup(surface=surface, values=evidence.unverifiable_markup)
            )
        for dimension, raw_values in sorted(violations.items()):
            entry = quarantine.get((surface, dimension))
            disposition: Literal["live", "quarantined"] = "live"
            if entry is not None and entry.expires_on >= today:
                disposition = "quarantined"
            findings.append(
                Finding(
                    surface=surface,
                    dimension=dimension,
                    values=tuple(sorted(set(raw_values))),
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
    has_live = any(item.disposition == "live" for item in ordered_findings)
    static_failed = bool(
        has_live or unregistered or stale_registrations or ordered_unverifiable or ordered_stale
    )
    if static_failed:
        static_status: Literal["clean", "known-quarantine", "failed"] = "failed"
    elif any(item.disposition == "quarantined" for item in ordered_findings):
        static_status = "known-quarantine"
    else:
        static_status = "clean"
    return (
        discovered,
        unregistered,
        stale_registrations,
        ordered_findings,
        ordered_unverifiable,
        ordered_stale,
        static_status,
    )


def _validate_canary_url(canary_url: str) -> None:
    parsed_url = urllib.parse.urlsplit(canary_url)
    if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname is None:
        raise ValueError("canary URL must use http or https")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError("canary URL must not contain credentials")


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


def _scan_canary(canary_url: str | None) -> CanaryResult:
    if canary_url is None:
        return CanaryResult(status="skipped:not-requested")

    try:
        _validate_canary_url(canary_url)
        deadline = time.monotonic() + _CANARY_WALL_TIMEOUT_SECONDS
        result_queue: queue.Queue[str | Exception] = queue.Queue(maxsize=1)

        def fetch() -> None:
            try:
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
        html = result
    except Exception as exc:
        return CanaryResult(
            status="skipped:unavailable",
            reason=f"{type(exc).__name__}: canary unavailable",
        )

    evidence = scan_surface_evidence("<canary>", html)
    title_findings = tuple(evidence.violations().get("floating-card-title", []))
    if title_findings or evidence.unverifiable_markup:
        return CanaryResult(
            status="failed",
            findings=title_findings,
            unverifiable_markup=evidence.unverifiable_markup,
        )
    return CanaryResult(status="passed")


def _build_receipt(source_root: Path, canary_url: str | None) -> ConformanceReceipt:
    (
        checked,
        unregistered,
        stale_registrations,
        findings,
        unverifiable_markup,
        stale_quarantine,
        static_status,
    ) = _scan_static(source_root)
    canary = _scan_canary(canary_url)
    failed = static_status == "failed" or canary.status == "failed"
    return ConformanceReceipt(
        registry_version=REGISTRY_VERSION,
        checked_surfaces=checked,
        unregistered_surfaces=unregistered,
        stale_registrations=stale_registrations,
        findings=findings,
        unverifiable_markup=unverifiable_markup,
        stale_quarantine=stale_quarantine,
        static_status=static_status,
        canary=canary,
        verdict="fail" if failed else "pass",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    source_root: Path = args.source_root.resolve()
    if not source_root.is_dir():
        parser.error(f"source root is not a directory: {source_root}")

    try:
        receipt = _build_receipt(source_root, args.canary_url)
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

    return 1 if receipt.verdict == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
