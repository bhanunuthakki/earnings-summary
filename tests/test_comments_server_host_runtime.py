from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlunsplit
from uuid import uuid4

import comments_server
import pytest

from operations.host_runtime import (
    RELATIVE_RECEIPT,
    HostOwner,
    HostReceipt,
    HostRuntimeBundle,
)

ORIGIN = "https://review.example.ts.net"
CODE_IDENTITY = "isolated-reviewed-code"


@pytest.mark.parametrize("state", ["missing", "stale", "current", "invalid"])
def test_host_runtime_serves_only_cached_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    def configured_origin(**_: object) -> str:
        return ORIGIN

    def code_identity(_: Path) -> str:
        return CODE_IDENTITY

    monkeypatch.setattr(comments_server, "private_mobile_origin", configured_origin)
    monkeypatch.setattr(comments_server, "review_code_identity", code_identity)
    target = tmp_path / RELATIVE_RECEIPT
    if state != "missing":
        target.parent.mkdir(parents=True)
        if state == "invalid":
            target.write_text('{"private_payload":"DO_NOT_DISCLOSE"}')
        else:
            observed = datetime.now(UTC)
            if state == "stale":
                observed -= timedelta(hours=1)
            receipt = HostReceipt(
                observed_at=observed,
                config_sha256="a" * 64,
                owners=(HostOwner(name="dashboard", kind="service", state="Running"),),
            )
            target.write_text(receipt.model_dump_json())

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("request must not probe host or open financial database")

    monkeypatch.setattr(comments_server, "connect_sqlite", forbidden)
    monkeypatch.setattr("operations.host_runtime.collect_host_receipt", forbidden)
    app = comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
    response = app.test_client().get(
        "/api/operations/host-runtime",
        base_url="http://127.0.0.1:7421",
        headers={"Origin": "https://untrusted.example", "X-Forwarded-Host": "forged.example"},
    )

    assert response.status_code == 200
    bundle = HostRuntimeBundle.model_validate(response.get_json())
    assert bundle.receipt_state == state
    assert bundle.serving_origin_sha256 == hashlib.sha256(ORIGIN.encode()).hexdigest()
    assert bundle.code_instance_sha256 == hashlib.sha256(CODE_IDENTITY.encode()).hexdigest()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["ETag"] == f'"{bundle.content_sha256}"'
    assert "DO_NOT_DISCLOSE" not in response.get_data(as_text=True)
    assert (bundle.receipt is not None) == (state in {"current", "stale"})


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:7421"])
def test_host_runtime_refuses_missing_or_non_https_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str | None
) -> None:
    def configured_origin(**_: object) -> str | None:
        return origin

    monkeypatch.setattr(comments_server, "private_mobile_origin", configured_origin)
    response = (
        comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
        .test_client()
        .get("/api/operations/host-runtime")
    )
    assert response.status_code == 503
    assert "refusing to emit an identity" in response.get_json()["error"]
    assert "code_instance_sha256" not in response.get_json()


def test_host_runtime_rejects_invalid_configured_origin_without_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    invalid = urlunsplit(
        ("https", f"{uuid4().hex}:{marker}@review.example.ts.net", "/private", "token=hidden", "")
    )
    monkeypatch.setenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", invalid)
    response = (
        comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
        .test_client()
        .get("/api/operations/host-runtime")
    )
    assert response.status_code == 503
    assert marker not in response.get_data(as_text=True)
