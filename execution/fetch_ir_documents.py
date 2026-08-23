"""
execution/fetch_ir_documents.py
--------------------------------
Download IR documents from the per-ticker URL manifest
(``.tmp/ir_url_manifest/<T>_urls.json``, written by ``discover_ir_documents.py``)
into a ticker staging folder, then — with ``--categorize`` — hand them to
``categorize_ir_uploads.py`` for canonical registration.

Each URL is downloaded to ``ir_documents/<T>/<descriptive>__<url_sha8>.<ext>`` (a
bare filename directly under the ticker folder, NOT a period subdir). That layout
gives the categorizer a strong parent-folder ticker hint and the correct
canonical destination root, so the categorizer can content-classify the file and
move it to ``ir_documents/<T>/<period_end>/<doc_type>__<sha8>.<ext>`` + insert the
``documents`` row (and the legacy JSON-index mirror the LLM step reads). The real
source URL is recorded in ``.tmp/ir_incoming_urls.json`` so the categorizer stamps
it as ``documents.source_url`` (provenance) instead of the ``manual_upload:`` placeholder.

Idempotent: a URL already present in the ``documents`` table (source_url) is
skipped. Best-effort transport failures are logged and skipped, while an explicit
HTTP 401/403 is a typed authentication denial that halts the current job; auth is
never bypassed.

Usage:
    python execution/fetch_ir_documents.py --ticker GOOG
    python execution/fetch_ir_documents.py --ticker NU --categorize --calendar calendar
    python execution/fetch_ir_documents.py --all --categorize
    python execution/fetch_ir_documents.py --ticker GOOG --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from db_paths import configured_db_path  # noqa: E402
from ir_pipeline._net import (  # noqa: E402
    UnsafeURLError,
    build_public_opener,
    curl_resolve_entries,
    ensure_safe_public_url,
    safe_redirect_url,
)
from ir_uploads import CategorizationFailure, classify_ir_file, sha256_of  # noqa: E402
from log_redact import redact  # noqa: E402
from pipeline.managed_ir_sources import (  # noqa: E402
    IssuerDocumentStagingReceipt,
    IssuerDocumentStagingRequest,
    PreparedIssuerDocumentPublisherError,
    StagedIssuerDocument,
    classification_evidence,
    classifier_code_identity,
    validate_prepared_staging,
    verifier_code_identity,
)
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    StoredCollectionAuthorization,
    authorize_stored_collection_target,
    reported_quarter_is_in_window,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    require_no_reparse_points,
)
from provenance.secure_file_install import (  # noqa: E402
    SecureFileInstallError,
    install_bytes_no_clobber,
)
from runtime.job_runtime import JobLock  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)


class IssuerDocumentPreparationError(RuntimeError):
    """Stable, non-sensitive failure result for managed issuer staging."""

    def __init__(
        self,
        code: str,
        *,
        attempt_id: str | None = None,
        phase: str | None = None,
        residue_paths: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.attempt_id = attempt_id
        self.phase = phase
        self.residue_paths = residue_paths
        super().__init__(code)


# A real browser User-Agent. Many issuer file CDNs (e.g. Brookfield's bam.brookfield.com)
# return 403 to a self-identifying bot UA on the DOCUMENT fetch even when robots.txt allows
# it and the (browser-UA) Playwright crawler already harvested the link — so a link we can
# SEE, we couldn't DOWNLOAD (BN: 51 links discovered, 0 downloaded, all 403). Matching the
# crawler's browser UA fixes it. robots.txt is still honored upstream in the crawler; this
# is the same accepted pattern as the robots-UA fix, not auth-wall bypass.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CONNECT_READ_TIMEOUT = 60
_RATE_LIMIT_S = 0.5
_CATEGORIZER = SCRIPT_DIR / "categorize_ir_uploads.py"
_MAX_REDIRECTS = 5
_QUARTER = re.compile(r"^Q([1-4])$")


class SourceAuthenticationDeniedError(RuntimeError):
    """An origin explicitly denied authorization; callers must halt this job."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"source authentication denied with HTTP {status_code}")


def resolve_root(arg_repo_root: str | None) -> Path:
    """Root holding ``ir_documents/``, ``data/portfolio.db`` and ``.tmp/``.

    ``--repo-root`` wins; else ``IR_PROJECT_ROOT`` (a worktree points this at the
    main checkout where the gitignored data lives); else this repo.
    """
    if arg_repo_root:
        return Path(arg_repo_root).resolve()
    env = os.environ.get("IR_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return PROJECT_ROOT


def manifest_dir(root: Path) -> Path:
    return root / ".tmp" / "ir_url_manifest"


def incoming_urls_path(root: Path) -> Path:
    return root / ".tmp" / "ir_incoming_urls.json"


def load_url_manifest(root: Path, ticker: str) -> list[dict[str, object]]:
    """Load ``.tmp/ir_url_manifest/<T>_urls.json`` (a list of entry dicts)."""
    path = manifest_dir(root) / f"{ticker.upper()}_urls.json"
    if not path.exists():
        log.warning({"event": "manifest_not_found", "ticker": ticker, "path": str(path)})
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [cast("dict[str, object]", e) for e in cast("list[object]", raw) if isinstance(e, dict)]


def _reported_quarter(entry: dict[str, object]) -> tuple[int, int] | None:
    year = entry.get("year")
    quarter = entry.get("quarter")
    match = _QUARTER.fullmatch(str(quarter)) if quarter is not None else None
    if not isinstance(year, int) or match is None:
        return None
    return year, int(match.group(1))


def _bounded_manifest_entries(
    entries: list[dict[str, object]],
    *,
    max_quarters: int,
) -> tuple[list[dict[str, object]], int]:
    """Admit only entries in the newest typed reported-quarter window."""

    periods = sorted(
        {period for entry in entries if (period := _reported_quarter(entry)) is not None},
        reverse=True,
    )
    admitted_periods = frozenset(periods[:max_quarters])
    admitted = [entry for entry in entries if _reported_quarter(entry) in admitted_periods]
    return admitted, len(entries) - len(admitted)


def _emit_policy_denial(ticker: str, authorization: StoredCollectionAuthorization) -> None:
    target = authorization.target
    decision = authorization.decision
    payload = {
        "event": "source_collection_policy_denied",
        "ticker": ticker,
        "coverage_role": target.coverage_role.value if target is not None else None,
        "source": CollectionSource.IR.value,
        "artifact_kind": ArtifactKind.IR_DOCUMENT.value,
        "reason": decision.reason.value if decision is not None else authorization.status.value,
    }
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


def _url_sha8(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def _safe_token(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "x"


def _staging_base_name(ticker: str, doc_type: str, year: object, quarter: object, url: str) -> str:
    """``<T>_<doctype>_<period>__<url_sha8>`` (no extension; period 'undated' if unknown)."""
    period = f"{year}{quarter}" if (year and quarter) else "undated"
    return f"{ticker.upper()}_{_safe_token(doc_type or 'document')}_{_safe_token(period)}__{_url_sha8(url)}"


def _ext_from_headers(url: str, content_disposition: str, content_type: str) -> str:
    """Decide the on-disk extension. The content classifier dispatches on suffix,
    so this must match the bytes (an xlsx saved as .pdf would fail to fingerprint)."""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', content_disposition)
    if m:
        name = m.group(1).lower()
        for e in (".xlsx", ".xls", ".pdf"):
            if name.endswith(e):
                return e
    ct = content_type.lower()
    if "spreadsheet" in ct or "excel" in ct:
        return ".xlsx"
    if "pdf" in ct:
        return ".pdf"
    low = url.lower()
    for e in (".xlsx", ".xls", ".pdf"):
        if e in low:
            return e
    return ".pdf"


def _registered_source_urls(db_path: Path, ticker: str) -> set[str]:
    """source_urls already in the ``documents`` table for ``ticker`` (idempotency key)."""
    if not db_path.exists():
        return set()
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows = conn.execute(
                "SELECT source_url FROM documents WHERE ticker = ? AND source_url IS NOT NULL",
                (ticker.upper(),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows if r[0]}


# curl_cffi impersonation target — the rolling "latest Chrome" alias replays a
# real Chrome JA3/HTTP2 fingerprint, which is what the tarpit hosts check for.
_IMPERSONATE = "chrome"


def _fetch_curl_cffi(url: str) -> tuple[bytes, str, str] | None:
    """Fetch ``url`` impersonating Chrome's full TLS/HTTP2 fingerprint.

    Some issuer hosts (e.g. ``investor.lilly.com``) tarpit any client whose TLS
    fingerprint isn't a real browser's — they accept the connection but never
    respond, so a plain urllib GET stalls until timeout, while a real Chrome is
    served instantly. ``curl_cffi`` replays Chrome's JA3/HTTP2 fingerprint, which
    clears the tarpit. Optional dep (the ``ir`` extra); a missing dep or any fetch
    failure degrades to a logged skip, never a crash.

    Returns ``(data, content_disposition, content_type)`` or None.
    """
    try:
        from curl_cffi import CurlOpt
        from curl_cffi import requests as cc
    except ImportError:
        log.error({"event": "curl_cffi_unavailable", "url": redact(url)})
        return None
    current = url
    cc_any = cast(Any, cc)
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            with cc_any.Session(
                trust_env=False,
                curl_options={CurlOpt.RESOLVE: curl_resolve_entries(current)},
            ) as session:
                ensure_safe_public_url(current)
                resp = session.get(
                    current,
                    impersonate=_IMPERSONATE,
                    headers={
                        "Accept": (
                            "application/pdf,application/vnd.ms-excel,application/octet-stream,*/*"
                        )
                    },
                    timeout=_CONNECT_READ_TIMEOUT,
                    allow_redirects=False,
                )
        except (UnsafeURLError, ValueError) as e:
            log.warning({"event": "unsafe_url_blocked", "url": redact(current), "error": redact(e)})
            return None
        except Exception as e:  # curl_cffi exposes a broad error tree; degrade safely
            log.error(
                {"event": "curl_cffi_failed", "url": redact(current), "error": redact(e)[:120]}
            )
            return None
        if 300 <= resp.status_code < 400:
            try:
                current = safe_redirect_url(current, resp.headers.get("Location", "") or "")
            except UnsafeURLError as e:
                log.warning(
                    {
                        "event": "unsafe_redirect_blocked",
                        "url": redact(current),
                        "error": redact(e),
                    }
                )
                return None
            continue
        if resp.status_code in {401, 403}:
            log.error(
                {
                    "event": "source_authentication_denied",
                    "url": redact(current),
                    "status": resp.status_code,
                }
            )
            raise SourceAuthenticationDeniedError(resp.status_code)
        if resp.status_code != 200:
            log.error(
                {"event": "curl_cffi_http", "url": redact(current), "status": resp.status_code}
            )
            return None
        cd = resp.headers.get("Content-Disposition", "") or ""
        ct = resp.headers.get("Content-Type", "") or ""
        return resp.content, cd, ct
    log.warning({"event": "too_many_redirects", "url": redact(url)})
    return None


def _fetch_bytes(url: str) -> tuple[bytes, str, str] | None:
    """``(data, content_disposition, content_type)`` for ``url``, or None.

    A plain browser-UA urllib GET first (fast, no extra dep for the ~95% of CDNs
    that serve cleanly); on a connection/timeout failure — the signature of a
    TLS-fingerprint tarpit — fall back to a Chrome-impersonating ``curl_cffi`` GET.
    An explicit HTTP error (403/404) is a real refusal, not a tarpit, so it is NOT
    retried.
    """
    try:
        ensure_safe_public_url(url)
    except UnsafeURLError as e:
        log.warning({"event": "unsafe_url_blocked", "url": redact(url), "error": redact(e)})
        return None
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            # Browser-like Accept so strict CDNs serve the file rather than 406/403.
            "Accept": "application/pdf,application/vnd.ms-excel,application/octet-stream,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        opener = build_public_opener()
        ensure_safe_public_url(url)
        with opener.open(req, timeout=_CONNECT_READ_TIMEOUT) as resp:
            return (
                resp.read(),
                resp.headers.get("Content-Disposition", "") or "",
                resp.headers.get("Content-Type", "") or "",
            )
    except urllib.error.HTTPError as e:
        if e.code in {401, 403}:
            log.error(
                {
                    "event": "source_authentication_denied",
                    "url": redact(url),
                    "status": e.code,
                }
            )
            raise SourceAuthenticationDeniedError(e.code) from None
        log.error({"event": "http_error", "url": redact(url), "status": e.code})
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Connection refused / DNS / read-timeout (the tarpit signature) → try the
        # TLS-impersonating client before giving up.
        log.warning(
            {"event": "urllib_failed_trying_curl", "url": redact(url), "error": redact(e)[:120]}
        )
        return _fetch_curl_cffi(url)


def _download(url: str, dest_dir: Path, base_name: str) -> Path | None:
    """GET ``url`` into ``dest_dir/<base_name><ext>``; ext from response headers.

    Returns the written Path, or None on any network/HTTP error (logged, skipped).
    Falls back to a Chrome-TLS-impersonating client for hosts that tarpit urllib.
    """
    fetched = _fetch_bytes(url)
    if fetched is None:
        return None
    data, cd, ct = fetched
    ext = _ext_from_headers(url, cd, ct)
    dest = dest_dir / f"{base_name}{ext}"
    try:
        require_no_reparse_points(dest)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise IssuerDocumentPreparationError("staging_destination_unsafe") from exc
    # Some IR doc links serve an HTML error/redirect page (with a .pdf
    # Content-Disposition). Don't stage HTML as a .pdf — it just fails pypdf
    # downstream and clutters the quarantine.
    if dest.suffix in {".pdf", ".xlsx", ".xls"} and data[:512].lstrip()[:15].lower().startswith(
        (b"<!doctype", b"<html", b"<?xml")
    ):
        log.warning({"event": "html_not_document", "url": redact(url)})
        return None

    try:
        _secure_staging_directory(dest_dir)
        installed = install_bytes_no_clobber(
            dest_dir,
            dest.name,
            data,
            expected_sha256=hashlib.sha256(data).hexdigest(),
            expected_size=len(data),
        )
    except SecureFileInstallError as exc:
        raise IssuerDocumentPreparationError(
            "staging_destination_unsafe",
            phase="download",
            residue_paths=tuple(path.name for path in exc.residue_paths),
        ) from exc
    if installed.residue_paths:
        raise IssuerDocumentPreparationError(
            "staging_residue_retained",
            phase="download",
            residue_paths=tuple(path.name for path in installed.residue_paths),
        )
    dest = installed.path
    try:
        require_no_reparse_points(dest)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise IssuerDocumentPreparationError("staged_object_unsafe") from exc
    log.info({"event": "downloaded", "dest": str(dest), "size_kb": len(data) // 1024})
    return dest


def _secure_staging_directory(directory: Path) -> None:
    """Create one attempt-private directory without accepting reparse parents."""
    try:
        require_no_reparse_points(directory)
        directory.mkdir(parents=True, exist_ok=True)
        require_no_reparse_points(directory)
        metadata = directory.lstat()
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise IssuerDocumentPreparationError("staging_directory_unsafe") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise IssuerDocumentPreparationError("staging_directory_unsafe")


def _retained_staging_names(directory: Path, direct: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Report retained attempt-local names without following an unsafe directory."""
    names = {Path(name).name for name in direct}
    try:
        require_no_reparse_points(directory)
        names.update(entry.name for entry in directory.iterdir())
    except (OSError, ImmutableArtifactConflictError):
        pass
    return tuple(sorted(names))


def _preparation_failure(
    code: str,
    *,
    request: IssuerDocumentStagingRequest,
    phase: str,
    objects: Path,
    direct_residues: tuple[str, ...] = (),
) -> IssuerDocumentPreparationError:
    return IssuerDocumentPreparationError(
        code,
        attempt_id=request.attempt_id,
        phase=phase,
        residue_paths=_retained_staging_names(objects, direct_residues),
    )


def _publisher_residue_names(exc: PreparedIssuerDocumentPublisherError) -> tuple[str, ...]:
    """Keep direct publisher/installer residues in the attempt-local report."""
    return tuple(
        dict.fromkeys(
            (*exc.remaining_paths, *exc.owned_artifacts, *exc.created, *exc.removed_paths)
        )
    )


def _write_incoming_sidecar(root: Path, mapping: dict[str, str]) -> None:
    """Merge ``{staging_filename: source_url}`` into ``.tmp/ir_incoming_urls.json``."""
    if not mapping:
        return
    path = incoming_urls_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = {str(k): str(v) for k, v in cast("dict[str, object]", loaded).items()}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(mapping)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _publish_attempt_text(path: Path, text: str) -> None:
    """Seal attempt-private JSON through the shared no-replace transaction."""
    payload = (text + "\n").encode("utf-8")
    try:
        result = install_bytes_no_clobber(
            path.parent,
            path.name,
            payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
        )
    except SecureFileInstallError as exc:
        retained = (*exc.residue_paths, *((exc.ownership.path,) if exc.ownership else ()))
        raise IssuerDocumentPreparationError(
            "staging_receipt_publish_failed",
            attempt_id=path.parent.name,
            phase="publish",
            residue_paths=tuple(dict.fromkeys(item.name for item in retained)),
        ) from exc
    if result.residue_paths:
        raise IssuerDocumentPreparationError(
            "staging_receipt_residue_retained",
            attempt_id=path.parent.name,
            phase="publish",
            residue_paths=tuple(item.name for item in result.residue_paths),
        )


def _run_categorize(ticker: str, root: Path, db_path: Path, calendar: str | None) -> int:
    """Invoke categorize_ir_uploads.py for ``ticker`` (content-classify + register)."""
    argv = [
        *managed_python_prefix(PROJECT_ROOT),
        str(_CATEGORIZER),
        "--ticker",
        ticker.upper(),
        "--source-dir",
        str(root / "ir_documents"),
        "--db-path",
        str(db_path),
        "--rel-root",
        str(root),
    ]
    if calendar:
        argv += ["--calendar", calendar]
    env = dict(os.environ, IR_PROJECT_ROOT=str(root))
    proc = subprocess.run(argv, env=env, check=False)
    return proc.returncode


def prepare_issuer_document_sources(
    request: IssuerDocumentStagingRequest, *, state_root: Path, db_path: Path
) -> IssuerDocumentStagingReceipt:
    """Prepare exact issuer bytes below attempt-private staging only."""
    root = state_root.resolve(strict=True)
    if db_path.resolve() != configured_db_path(root):
        raise IssuerDocumentPreparationError("configured_database_mismatch")
    inventory = request.inventory_request
    # Preparation owns only ir-discovery. Authorization/window reads are
    # read-only snapshots, so long downloads never reserve the DB writer lane.
    authorization = authorize_stored_collection_target(
        db_path,
        inventory.ticker,
        requested=True,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    in_window = authorization.fiscal_year_end_month is not None and reported_quarter_is_in_window(
        fiscal_year=inventory.fiscal_year,
        fiscal_quarter=inventory.fiscal_quarter,
        fiscal_year_end_month=authorization.fiscal_year_end_month,
        as_of=date.today(),
    )
    if not authorization.allowed:
        raise IssuerDocumentPreparationError("source_policy_denied")
    if not in_window:
        raise IssuerDocumentPreparationError("reported_quarter_window_denied")
    staging = root / ".tmp" / "managed_ir_staging" / request.attempt_id
    receipt_path = staging / "staging_receipt.json"
    try:
        require_no_reparse_points(staging)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise IssuerDocumentPreparationError("staging_directory_unsafe") from exc
    if receipt_path.exists():
        with JobLock(root, "managed-ir-stage-replay", ["ir-discovery"], wait_s=0):
            try:
                return validate_prepared_staging(request, state_root=root, db_path=db_path)
            except PreparedIssuerDocumentPublisherError as exc:
                raise _preparation_failure(
                    "staging_replay_invalid",
                    request=request,
                    phase="replay",
                    objects=staging / "objects",
                    direct_residues=_publisher_residue_names(exc),
                ) from exc
    with JobLock(root, "managed-ir-stage", ["ir-discovery"], wait_s=0):
        objects = staging / "objects"
        _secure_staging_directory(objects)
        documents: list[StagedIssuerDocument] = []
        for expected in inventory.expected_documents:
            try:
                destination = _download(
                    expected.source_url,
                    objects,
                    hashlib.sha256(expected.source_url.encode("utf-8")).hexdigest()[:16],
                )
            except IssuerDocumentPreparationError as exc:
                raise _preparation_failure(
                    exc.code,
                    request=request,
                    phase=exc.phase or "download",
                    objects=objects,
                    direct_residues=exc.residue_paths,
                ) from exc
            if destination is None:
                raise _preparation_failure(
                    "staging_download_failed",
                    request=request,
                    phase="download",
                    objects=objects,
                )
            try:
                outcome = classify_ir_file(destination, ticker_hint=inventory.ticker)
            except (OSError, ValueError) as exc:
                raise _preparation_failure(
                    "staging_classification_failed",
                    request=request,
                    phase="classify",
                    objects=objects,
                ) from exc
            if isinstance(outcome, CategorizationFailure) or (
                outcome.ticker != inventory.ticker
                or outcome.doc_type.value != expected.document_type
                or outcome.period_end != inventory.period_end
                or outcome.confidence.value == "low"
            ):
                raise _preparation_failure(
                    "staging_classification_mismatch",
                    request=request,
                    phase="classify",
                    objects=objects,
                )
            media_type = (
                "application/pdf"
                if destination.suffix.lower() == ".pdf"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            try:
                size_bytes = destination.stat().st_size
                digest = sha256_of(destination)
            except OSError as exc:
                raise _preparation_failure(
                    "staging_object_invalid",
                    request=request,
                    phase="classify",
                    objects=objects,
                ) from exc
            try:
                documents.append(
                    StagedIssuerDocument(
                        source_url=expected.source_url,
                        document_type=expected.document_type,
                        object_path=f"objects/{destination.name}",
                        sha256=digest,
                        byte_size=size_bytes,
                        fetched_at=datetime.now(UTC),
                        media_type=media_type,
                        ticker=outcome.ticker,
                        period_end=outcome.period_end.isoformat(),
                        classification_confidence=outcome.confidence.value,
                        classification_evidence_sha256=classification_evidence(outcome),
                    )
                )
            except ValueError as exc:
                raise _preparation_failure(
                    "staging_document_invalid",
                    request=request,
                    phase="classify",
                    objects=objects,
                ) from exc
        try:
            documents.sort(key=lambda item: (item.source_url, item.document_type))
            unsigned = {
                "schema_version": "issuer_document_staging_receipt.v1",
                "request": request.model_dump(mode="json"),
                "documents": [item.model_dump(mode="json") for item in documents],
                "classifier_code_sha256": classifier_code_identity(),
                "verifier_code_sha256": verifier_code_identity(),
                "canonical_mutations": False,
            }
            receipt = IssuerDocumentStagingReceipt.model_validate(
                {
                    **unsigned,
                    "receipt_sha256": hashlib.sha256(
                        json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        except ValueError as exc:
            raise _preparation_failure(
                "staging_receipt_invalid",
                request=request,
                phase="publish",
                objects=objects,
            ) from exc
        try:
            _publish_attempt_text(receipt_path, receipt.canonical_json)
            return validate_prepared_staging(request, state_root=root, db_path=db_path)
        except IssuerDocumentPreparationError as exc:
            raise _preparation_failure(
                exc.code,
                request=request,
                phase=exc.phase or "publish",
                objects=objects,
                direct_residues=exc.residue_paths,
            ) from exc
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise _preparation_failure(
                "staging_receipt_publish_failed",
                request=request,
                phase="publish",
                objects=objects,
            ) from exc
        except PreparedIssuerDocumentPublisherError as exc:
            raise _preparation_failure(
                "staging_validation_failed",
                request=request,
                phase="publish",
                objects=objects,
                direct_residues=_publisher_residue_names(exc),
            ) from exc


def process_ticker(
    ticker: str,
    *,
    root: Path,
    db_path: Path,
    dry_run: bool = False,
    categorize: bool = False,
    calendar: str | None = None,
    owner_requested: bool = False,
) -> dict[str, object]:
    """Download every manifest URL for ``ticker`` into staging; optionally categorize."""
    ticker = ticker.upper()
    authorization = authorize_stored_collection_target(
        db_path,
        ticker,
        requested=owner_requested,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        _emit_policy_denial(ticker, authorization)
        denied: dict[str, object] = {
            "ticker": ticker,
            "status": "policy_denied",
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "policy_skipped": 0,
        }
        print(json.dumps(denied))
        return denied
    entries = load_url_manifest(root, ticker)
    if not entries:
        empty: dict[str, object] = {
            "ticker": ticker,
            "status": "no_manifest",
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "policy_skipped": 0,
        }
        print(json.dumps(empty))
        return empty
    entries, policy_skipped = _bounded_manifest_entries(
        entries,
        max_quarters=SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
    )

    already = _registered_source_urls(db_path, ticker)
    dest_dir = root / "ir_documents" / ticker
    sidecar: dict[str, str] = {}
    downloaded = skipped = failed = 0

    for entry in entries:
        url = str(entry.get("url") or "")
        if not url:
            continue
        if url in already:
            skipped += 1
            continue
        base = _staging_base_name(
            ticker, str(entry.get("doc_type") or ""), entry.get("year"), entry.get("quarter"), url
        )
        if dry_run:
            print(f"[DRY RUN] would download {url} -> {dest_dir / base}.<ext>")
            skipped += 1
            continue
        dest = _download(url, dest_dir, base)
        time.sleep(_RATE_LIMIT_S)
        if dest is None:
            failed += 1
            continue
        sidecar[dest.name] = url
        downloaded += 1

    if not dry_run:
        _write_incoming_sidecar(root, sidecar)

    cat_rc: int | None = None
    if categorize and not dry_run and downloaded:
        cat_rc = _run_categorize(ticker, root, db_path, calendar)

    summary: dict[str, object] = {
        "ticker": ticker,
        "status": "done",
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "policy_skipped": policy_skipped,
        "categorize_rc": cat_rc,
    }
    print(json.dumps(summary))
    return summary


def verify_ticker(root: Path, db_path: Path, ticker: str) -> None:
    """Report which manifest URLs are registered in the ``documents`` table."""
    ticker = ticker.upper()
    entries = load_url_manifest(root, ticker)
    registered = _registered_source_urls(db_path, ticker)
    if not entries:
        print(f"[{ticker}] no manifest.")
        return
    for entry in entries:
        url = str(entry.get("url") or "")
        mark = "✓" if url in registered else "✗ not registered"
        print(f"  [{mark}] {ticker} {entry.get('doc_type')} {url[:80]}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = resolve_root(args.repo_root)
    db_path = Path(args.db) if args.db else root / "data" / "portfolio.db"

    if args.verify:
        if args.all:
            for p in sorted(manifest_dir(root).glob("*_urls.json")):
                verify_ticker(root, db_path, p.stem.replace("_urls", ""))
        else:
            verify_ticker(root, db_path, args.ticker)
        return 0

    if args.all:
        manifests = sorted(manifest_dir(root).glob("*_urls.json"))
        if not manifests:
            print(f"No URL manifests in {manifest_dir(root)}", file=sys.stderr)
            return 1
        try:
            for p in manifests:
                process_ticker(
                    p.stem.replace("_urls", ""),
                    root=root,
                    db_path=db_path,
                    dry_run=args.dry_run,
                    categorize=args.categorize,
                    calendar=args.calendar,
                    owner_requested=False,
                )
        except SourceAuthenticationDeniedError:
            return 10
        return 0

    try:
        summary = process_ticker(
            args.ticker,
            root=root,
            db_path=db_path,
            dry_run=args.dry_run,
            categorize=args.categorize,
            calendar=args.calendar,
            owner_requested=not args.automatic,
        )
    except SourceAuthenticationDeniedError:
        return 10
    return 2 if summary["status"] == "policy_denied" else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download IR documents from discovered URL manifests.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker (e.g. GOOG)")
    group.add_argument(
        "--all", action="store_true", help="Process every manifest in .tmp/ir_url_manifest/"
    )
    p.add_argument(
        "--categorize", action="store_true", help="Run categorize_ir_uploads.py after downloading"
    )
    p.add_argument(
        "--calendar",
        help="Fiscal-calendar id passed to the categorizer (auto-fetch attribution for "
        "tickers not in ISSUER_REGISTRY; see ir_uploads.calendar_id_from_fye)",
    )
    p.add_argument(
        "--verify", action="store_true", help="Report manifest-vs-documents coverage; no download"
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would download; no writes")
    p.add_argument("--db", help="portfolio.db path (default: <root>/data/portfolio.db)")
    p.add_argument(
        "--repo-root", dest="repo_root", help="Root holding ir_documents/ + data/ + .tmp/"
    )
    p.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
