# pyright: reportPrivateUsage=false
"""Tests for execution/fetch_ir_documents.py — the IR document downloader.

The load-bearing fix guarded here: the downloader must send a real BROWSER
User-Agent. Issuer file CDNs (e.g. Brookfield's bam.brookfield.com) return 403 to
a self-identifying bot UA on the document fetch even when the (browser-UA) crawler
already harvested the link — which silently zeroed BN (51 links discovered, 0
downloaded, all 403). A blocked URL must still degrade to a skip, never a crash.
"""

from __future__ import annotations

import email.message
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest
from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import fetch_ir_documents as fid  # noqa: E402
from models.documents import DocType  # noqa: E402
from models.ir_uploads import CategorizationResult, Confidence  # noqa: E402
from pipeline.issuer_document_inventory import (  # noqa: E402
    ExpectedIssuerDocument,
    IssuerDocumentInventoryRequest,
)
from provenance import secure_file_install  # noqa: E402
from provenance.secure_file_install import (  # noqa: E402
    SecureFileInstallError,
    SecureFileInstallResult,
)


class _FakeResp:
    """Minimal urlopen() stand-in: context manager + headers.get + read."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers: dict[str, str] = {"Content-Type": "application/pdf"}

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


class _FakeOpener:
    """Minimal build_opener() result for URL-guarded downloader tests."""

    def __init__(
        self, open_fn: Callable[[urllib.request.Request, float | None], _FakeResp]
    ) -> None:
        self._open_fn = open_fn

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        return self._open_fn(req, timeout)


def _no_registered_urls(*_a: object, **_k: object) -> set[str]:
    """Typed stand-in for _registered_source_urls (a bare lambda trips pyright)."""
    return set()


def _no_sleep(_seconds: float) -> None:
    return None


def _write_manifest(root: Path, ticker: str, url: str) -> None:
    mdir = fid.manifest_dir(root)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{ticker}_urls.json").write_text(
        json.dumps([{"url": url, "doc_type": "press_release", "year": 2026, "quarter": "Q1"}]),
        encoding="utf-8",
    )


def _make_policy_db(db: Path, rows: list[tuple[str, str]]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL)",
            rows,
        )


def _allow_safe_url(_url: str) -> None:
    """Network admission is outside these transport-unit tests."""


def _no_curl_resolve_entries(_url: str) -> list[str]:
    return []


def test_downloader_sends_browser_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download Request must carry a browser UA + Accept — the BN 403 fix.

    A regression here (reverting to a self-identifying bot UA) silently 403s every
    file on bot-mitigating issuer CDNs, so guard the UA explicitly.
    """
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("BN", "portfolio")])
    _write_manifest(root, "BN", "https://bam.brookfield.com/x/Q1-26-BAM-Press-Release.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)
    monkeypatch.setattr(fid, "ensure_safe_public_url", _allow_safe_url)
    monkeypatch.setattr(fid, "curl_resolve_entries", _no_curl_resolve_entries)
    captured: dict[str, str | None] = {}

    def _fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        captured["ua"] = req.get_header("User-agent")
        captured["accept"] = req.get_header("Accept")
        return _FakeResp(b"%PDF-1.4 fake document bytes")

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_fake_urlopen)

    monkeypatch.setattr(
        "execution.fetch_ir_documents.urllib.request.build_opener",
        _fake_opener,
    )
    summary = fid.process_ticker("BN", root=root, db_path=db, categorize=False)

    assert summary["downloaded"] == 1
    ua = captured["ua"] or ""
    assert "Mozilla" in ua and "Chrome" in ua  # a real browser UA
    assert "InvestorResearchBot" not in ua  # NOT the old self-identifying bot UA
    assert "pdf" in (captured["accept"] or "")


@pytest.mark.parametrize("status", [401, 403])
def test_downloader_halts_on_explicit_auth_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("ZZ", "portfolio")])
    _write_manifest(root, "ZZ", "https://blocked.example/Q1.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)

    def _raise_auth(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        raise urllib.error.HTTPError(
            req.full_url,
            status,
            "Authentication denied",
            email.message.Message(),
            None,
        )

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_raise_auth)

    def safe_url(_url: str) -> None:
        return None

    monkeypatch.setattr(fid, "ensure_safe_public_url", safe_url)
    monkeypatch.setattr(fid, "build_public_opener", _fake_opener)
    with pytest.raises(fid.SourceAuthenticationDeniedError) as exc_info:
        fid.process_ticker("ZZ", root=root, db_path=db, categorize=False)
    assert exc_info.value.status_code == status


class _CurlResp:
    """Minimal curl_cffi response stand-in."""

    status_code = 200
    content = b"%PDF-1.4 lilly press release"
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/pdf"}


def test_downloader_falls_back_to_curl_cffi_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A urllib read-timeout (the TLS-tarpit signature) falls back to curl_cffi.

    investor.lilly.com tarpits any non-browser TLS fingerprint — urllib stalls to
    timeout, but a Chrome-impersonating curl_cffi GET is served. A 403 is NOT a
    tarpit, so it is not retried (covered above).
    """
    ccr = pytest.importorskip("curl_cffi.requests")
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("LLY", "portfolio")])
    _write_manifest(root, "LLY", "https://investor.lilly.com/static-files/uuid-1")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)
    monkeypatch.setattr(fid, "ensure_safe_public_url", _allow_safe_url)
    monkeypatch.setattr(fid, "curl_resolve_entries", _no_curl_resolve_entries)

    def _timeout_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = (req, timeout)
        raise TimeoutError("tarpit: the read operation timed out")

    class _FakeSession:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["curl_options"]

        def __enter__(self) -> _FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, **_kw: object) -> _CurlResp:
            _ = url
            return _CurlResp()

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_timeout_urlopen)

    monkeypatch.setattr(
        "execution.fetch_ir_documents.urllib.request.build_opener",
        _fake_opener,
    )
    monkeypatch.setattr(ccr, "Session", _FakeSession)
    summary = fid.process_ticker("LLY", root=root, db_path=db, categorize=False)
    assert summary["downloaded"] == 1  # recovered via curl_cffi after urllib stalled


def test_direct_ticker_and_all_cannot_bypass_stored_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "portfolio.db"
    _make_policy_db(
        db,
        [("PORT", "portfolio"), ("EVAL", "evaluation"), ("WATCH", "watchlist")],
    )
    for ticker in ("PORT", "EVAL", "WATCH", "UNKNOWN"):
        _write_manifest(tmp_path, ticker, f"https://issuer.example/{ticker}/2026Q1.pdf")
    calls: list[str] = []

    def _record(url: str, _dest: Path, _base: str) -> Path:
        calls.append(url)
        return tmp_path / "staged.pdf"

    monkeypatch.setattr(fid, "_download", _record)
    monkeypatch.setattr(fid.time, "sleep", _no_sleep)

    assert (
        fid.main(
            [
                "--ticker",
                "WATCH",
                "--repo-root",
                str(tmp_path),
                "--db",
                str(db),
            ]
        )
        == 2
    )
    assert calls == []
    assert "source_collection_policy_denied" in capsys.readouterr().err

    assert fid.main(["--all", "--repo-root", str(tmp_path), "--db", str(db)]) == 0
    assert calls == ["https://issuer.example/PORT/2026Q1.pdf"]


def test_fetch_boundary_skips_manifest_periods_outside_canonical_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "portfolio.db"
    _make_policy_db(db, [("PORT", "portfolio")])
    entries = [
        {
            "url": f"https://issuer.example/{year}Q{quarter}.pdf",
            "doc_type": "press_release",
            "year": year,
            "quarter": f"Q{quarter}",
        }
        for year, quarter in [(2026, 2), (2026, 1), (2025, 4), (2025, 3), (2025, 2), (2025, 1)]
    ]
    mdir = fid.manifest_dir(tmp_path)
    mdir.mkdir(parents=True)
    (mdir / "PORT_urls.json").write_text(json.dumps(entries), encoding="utf-8")
    calls: list[str] = []

    def _record(url: str, _dest: Path, _base: str) -> Path:
        calls.append(url)
        return tmp_path / "staged.pdf"

    monkeypatch.setattr(fid, "_download", _record)
    monkeypatch.setattr(fid.time, "sleep", _no_sleep)

    summary = fid.process_ticker("PORT", root=tmp_path, db_path=db)

    assert len(calls) == 5
    assert "https://issuer.example/2025Q1.pdf" not in calls
    assert summary["policy_skipped"] == 1


def test_staging_download_rejects_symlinked_objects_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / ".tmp" / "managed_ir_staging" / "attempt-0001"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.pdf"
    sentinel.write_bytes(b"unchanged")
    staging.mkdir(parents=True)
    (staging / "objects").symlink_to(outside, target_is_directory=True)

    def fake_fetch(_url: str) -> tuple[bytes, str, str]:
        return b"%PDF-1.4", "", "application/pdf"

    monkeypatch.setattr(fid, "_fetch_bytes", fake_fetch)

    with pytest.raises(fid.IssuerDocumentPreparationError, match="staging_destination_unsafe"):
        fid._download("https://issuer.example/report.pdf", staging / "objects", "report")

    assert sentinel.read_bytes() == b"unchanged"
    assert not (outside / "report.pdf").exists()


def test_staging_download_rejects_success_with_retained_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = tmp_path / "objects"
    residue = objects / ".report.pdf.retained.tmp"

    def fake_fetch(_url: str) -> tuple[bytes, str, str]:
        return b"%PDF-1.4", "", "application/pdf"

    def install_with_residue(
        root: Path, name: str, _payload: bytes, **_kwargs: object
    ) -> SecureFileInstallResult:
        return SecureFileInstallResult(
            root / name,
            created=False,
            residue_paths=(residue,),
        )

    monkeypatch.setattr(fid, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(fid, "install_bytes_no_clobber", install_with_residue)

    with pytest.raises(fid.IssuerDocumentPreparationError, match="staging_residue_retained") as exc:
        fid._download("https://issuer.example/report.pdf", objects, "report")

    assert exc.value.phase == "download"
    assert exc.value.residue_paths == (residue.name,)


def test_staging_download_preserves_installer_failure_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = tmp_path / "objects"
    residue = objects / ".report.pdf.retained.tmp"

    def fake_fetch(_url: str) -> tuple[bytes, str, str]:
        return b"%PDF-1.4", "", "application/pdf"

    def fail_install(*_args: object, **_kwargs: object) -> SecureFileInstallResult:
        raise SecureFileInstallError("secure_install_failed", residue_paths=(residue,))

    monkeypatch.setattr(fid, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(fid, "install_bytes_no_clobber", fail_install)

    with pytest.raises(
        fid.IssuerDocumentPreparationError, match="staging_destination_unsafe"
    ) as exc:
        fid._download("https://issuer.example/report.pdf", objects, "report")

    assert exc.value.phase == "download"
    assert exc.value.residue_paths == (residue.name,)


def test_attempt_receipt_publish_preserves_installer_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt-0001"
    residue = attempt / ".staging_receipt.json.retained.tmp"

    def fail_install(*_args: object, **_kwargs: object) -> SecureFileInstallResult:
        raise SecureFileInstallError("secure_install_failed", residue_paths=(residue,))

    monkeypatch.setattr(fid, "install_bytes_no_clobber", fail_install)
    with pytest.raises(fid.IssuerDocumentPreparationError) as exc:
        fid._publish_attempt_text(attempt / "staging_receipt.json", "{}")

    assert exc.value.code == "staging_receipt_publish_failed"
    assert exc.value.attempt_id == "attempt-0001"
    assert exc.value.phase == "publish"
    assert exc.value.residue_paths == (residue.name,)


def test_final_preparation_reports_owned_receipt_after_post_rename_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt target is retained when the installer loses only final verification."""
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    request = _preparation_request()
    monkeypatch.setattr(fid, "authorize_stored_collection_target", _allow_preparation_policy)
    monkeypatch.setattr(fid, "reported_quarter_is_in_window", _preparation_window)
    monkeypatch.setattr(fid, "_download", _prepare_staged_object)
    monkeypatch.setattr(fid, "classify_ir_file", _classify_prepared_object)

    def reject_final_verification(*_args: object, **_kwargs: object) -> None:
        raise SecureFileInstallError("installed_target_unsafe")

    monkeypatch.setattr(
        secure_file_install, "_verify_no_clobber_install", reject_final_verification
    )
    with pytest.raises(fid.IssuerDocumentPreparationError) as raised:
        fid.prepare_issuer_document_sources(
            request, state_root=root, db_path=root / "data" / "portfolio.db"
        )

    assert raised.value.code == "staging_receipt_publish_failed"
    assert raised.value.attempt_id == request.attempt_id
    assert raised.value.phase == "publish"
    assert raised.value.residue_paths == ("q2.pdf", "staging_receipt.json")
    receipt_path = (
        root / ".tmp" / "managed_ir_staging" / request.attempt_id / "staging_receipt.json"
    )
    assert receipt_path.exists()


def _supplement_preparation_request() -> fid.IssuerDocumentStagingRequest:
    inventory = IssuerDocumentInventoryRequest(
        ticker="MELI",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=(
            ExpectedIssuerDocument(
                source_url="https://issuer.test/q2-supplement.xlsx",
                document_type="ir_supplement",
            ),
        ),
    )
    return fid.IssuerDocumentStagingRequest(
        attempt_id="attempt-0002",
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
    )


def _supplement_preparation_outcome() -> CategorizationResult:
    return CategorizationResult(
        ticker="MELI",
        doc_type=DocType.IR_SUPPLEMENT,
        period_end=date(2026, 6, 30),
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["issuer"],
        doc_type_evidence=["xlsx_extension"],
        period_evidence=["q2"],
    )


def _classify_supplement_prepared_object(
    _path: Path,
    *,
    ticker_hint: str | None = None,
    calendar_override: str | None = None,
    source_url: str | None = None,
) -> CategorizationResult:
    del ticker_hint, calendar_override, source_url
    return _supplement_preparation_outcome()


def _allow_managed_preparation_policy(*_args: object, **_kwargs: object) -> None:
    return None


def _prepare_staged_supplement(_url: str, objects: Path, _name: str) -> Path:
    objects.mkdir(parents=True, exist_ok=True)
    path = objects / "q2-supplement.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "Financial Supplement"
    worksheet.append(["MELI", "Q2 2026", "Revenue"])
    worksheet.append(["Mercado Libre", "2026-06-30", 123])
    workbook.save(path)
    workbook.close()
    return path


def test_preparation_accepts_actual_xlsx_supplement(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XLSX classification and the staged/inventory contract share ir_supplement."""
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    request = _supplement_preparation_request()
    monkeypatch.setattr(fid, "authorize_stored_collection_target", _allow_preparation_policy)
    monkeypatch.setattr(fid, "reported_quarter_is_in_window", _preparation_window)
    monkeypatch.setattr(fid, "_download", _prepare_staged_supplement)
    monkeypatch.setattr(fid, "classify_ir_file", _classify_supplement_prepared_object)
    monkeypatch.setattr("pipeline.managed_ir_sources._policy", _allow_managed_preparation_policy)
    monkeypatch.setattr(
        "pipeline.managed_ir_sources.classify_ir_file", _classify_supplement_prepared_object
    )

    receipt = fid.prepare_issuer_document_sources(request, state_root=root, db_path=db_path)

    assert receipt.documents[0].document_type == "ir_supplement"
    assert receipt.documents[0].media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert receipt.documents[0].object_path.endswith(".xlsx")


def _preparation_request() -> fid.IssuerDocumentStagingRequest:
    inventory = IssuerDocumentInventoryRequest(
        ticker="MELI",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=(
            ExpectedIssuerDocument(
                source_url="https://issuer.test/q2.pdf", document_type="ir_presentation"
            ),
        ),
    )
    return fid.IssuerDocumentStagingRequest(
        attempt_id="attempt-0001",
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
    )


def _preparation_outcome() -> CategorizationResult:
    return CategorizationResult(
        ticker="MELI",
        doc_type=DocType.IR_PRESENTATION,
        period_end=date(2026, 6, 30),
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["issuer"],
        doc_type_evidence=["slides"],
        period_evidence=["q2"],
    )


def _prepare_staged_object(_url: str, objects: Path, _name: str) -> Path:
    objects.mkdir(parents=True, exist_ok=True)
    path = objects / "q2.pdf"
    path.write_bytes(b"presentation")
    return path


def _allow_preparation_policy(*_args: object, **_kwargs: object) -> object:
    return type("Allowed", (), {"allowed": True, "fiscal_year_end_month": 12})()


def _preparation_window(
    *,
    fiscal_year: int,
    fiscal_quarter: int,
    fiscal_year_end_month: int,
    as_of: date,
    max_quarters: int = 5,
) -> bool:
    del fiscal_year, fiscal_quarter, fiscal_year_end_month, as_of, max_quarters
    return True


def _classify_prepared_object(
    _path: Path,
    *,
    ticker_hint: str | None = None,
    calendar_override: str | None = None,
    source_url: str | None = None,
) -> CategorizationResult:
    del ticker_hint, calendar_override, source_url
    return _preparation_outcome()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("receipt", "staging_receipt_publish_failed"),
        ("validation", "staging_validation_failed"),
    ],
)
def test_final_preparation_errors_report_staged_objects_and_direct_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected_code: str
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    request = _preparation_request()
    objects = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects"
    monkeypatch.setattr(fid, "authorize_stored_collection_target", _allow_preparation_policy)
    monkeypatch.setattr(fid, "reported_quarter_is_in_window", _preparation_window)
    monkeypatch.setattr(fid, "_download", _prepare_staged_object)
    monkeypatch.setattr(fid, "classify_ir_file", _classify_prepared_object)
    if failure == "receipt":
        residue = objects.parent / ".staging_receipt.json.retained.tmp"

        def fail_publish(*_args: object, **_kwargs: object) -> None:
            raise fid.IssuerDocumentPreparationError(
                "staging_receipt_publish_failed",
                attempt_id=request.attempt_id,
                phase="publish",
                residue_paths=(residue.name,),
            )

        monkeypatch.setattr(fid, "_publish_attempt_text", fail_publish)
    else:
        residue = objects / ".validator.retained.tmp"

        def fail_validation(*_args: object, **_kwargs: object) -> object:
            raise fid.PreparedIssuerDocumentPublisherError(
                "staged_object_invalid", remaining_paths=(str(residue),)
            )

        monkeypatch.setattr(fid, "validate_prepared_staging", fail_validation)

    with pytest.raises(fid.IssuerDocumentPreparationError) as raised:
        fid.prepare_issuer_document_sources(
            request, state_root=root, db_path=root / "data" / "portfolio.db"
        )
    assert raised.value.code == expected_code
    assert raised.value.attempt_id == request.attempt_id
    assert raised.value.phase == "publish"
    assert raised.value.residue_paths == tuple(sorted(("q2.pdf", residue.name)))


def test_staging_replay_error_reports_existing_objects_and_direct_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    request = _preparation_request()
    staging = root / ".tmp" / "managed_ir_staging" / request.attempt_id
    objects = staging / "objects"
    _prepare_staged_object("", objects, "")
    (staging / "staging_receipt.json").write_text("{}\n", encoding="utf-8")
    residue = objects / ".replay.retained.tmp"
    monkeypatch.setattr(fid, "authorize_stored_collection_target", _allow_preparation_policy)
    monkeypatch.setattr(fid, "reported_quarter_is_in_window", _preparation_window)

    def fail_replay(*_args: object, **_kwargs: object) -> object:
        raise fid.PreparedIssuerDocumentPublisherError(
            "staging_receipt_invalid", remaining_paths=(str(residue),)
        )

    monkeypatch.setattr(fid, "validate_prepared_staging", fail_replay)
    with pytest.raises(fid.IssuerDocumentPreparationError) as raised:
        fid.prepare_issuer_document_sources(
            request, state_root=root, db_path=root / "data" / "portfolio.db"
        )
    assert raised.value.code == "staging_replay_invalid"
    assert raised.value.attempt_id == request.attempt_id
    assert raised.value.phase == "replay"
    assert raised.value.residue_paths == tuple(sorted(("q2.pdf", residue.name)))
