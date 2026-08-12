from __future__ import annotations

import email.message
import urllib.error

import pytest

from ir_pipeline.config import IrConfig
from ir_pipeline.discover import IrDiscoveryAuthenticationDeniedError, mz

_PAGE = """
<script>
const fmId = 'issuer-id';
const fmBase = 'https://apicatalog.mziq.com/filemanager';
</script>
<script>
categories.push({ internal_name: 'apresentacao_resultados' });
categories.push({ internal_name: 'planilha_resultados' });
</script>
"""


def test_catalog_discovery_uses_current_mz_api_without_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_get(_url: str, timeout: int = 30) -> str:
        del timeout
        return _PAGE

    def fake_post(url: str, payload: dict[str, object], timeout: int = 30) -> object:
        del timeout
        calls.append((url, payload))
        if url.endswith("/language/years"):
            return {"success": True, "data": [2026, 2025]}
        return {
            "success": True,
            "data": {
                "document_metas": [
                    {
                        "internal_name": "planilha_resultados",
                        "file_title": "Nu Holdings Historical Data 1Q26",
                        "file_quarter": 1,
                        "permalink": "https://api.mziq.com/mzfilemanager/v2/d/id/file?origin=2",
                    }
                ]
            },
        }

    def unexpected_browser(_url: str, _timeout_ms: int = 60_000) -> list[str]:
        pytest.fail("browser fallback must not run")

    monkeypatch.setattr(mz, "_get_text", fake_get)

    monkeypatch.setattr(mz, "_post_json", fake_post)
    monkeypatch.setattr(mz, "_visible_filemanager_hrefs", unexpected_browser)

    config = IrConfig(
        ticker="NU",
        platform="mz",
        results_center_url="https://www.investidores.nu/financials/results-center/",
    )
    assert mz.discover_documents(config) == {
        "spreadsheet": "https://api.mziq.com/mzfilemanager/v2/d/id/file?origin=2"
    }
    assert calls[0][0].endswith("/language/years")
    assert calls[1][1]["year"] == "2026"


def test_catalog_schema_failure_falls_back_to_guarded_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(_url: str, timeout: int = 30) -> str:
        del timeout
        return _PAGE

    def bad_post(_url: str, _payload: dict[str, object], timeout: int = 30) -> object:
        del timeout
        return {"success": True, "data": "bad"}

    def browser_fallback(_url: str, _timeout_ms: int = 60_000) -> list[str]:
        return ["https://api.mziq.com/mzfilemanager/v2/d/id/file"]

    def advertised_filename(_url: str, timeout: int = 30) -> str:
        del timeout
        return "Historical Data.xlsx"

    monkeypatch.setattr(mz, "_get_text", fake_get)
    monkeypatch.setattr(mz, "_post_json", bad_post)
    monkeypatch.setattr(mz, "_visible_filemanager_hrefs", browser_fallback)
    monkeypatch.setattr(mz, "filename_for_url", advertised_filename)

    config = IrConfig(
        ticker="NU",
        platform="mz",
        results_center_url="https://www.investidores.nu/financials/results-center/",
    )
    assert mz.discover_documents(config) == {
        "spreadsheet": "https://api.mziq.com/mzfilemanager/v2/d/id/file"
    }


@pytest.mark.parametrize(("helper", "status"), [("get", 401), ("post", 403)])
def test_mz_http_auth_denial_is_typed_at_the_network_boundary(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    status: int,
) -> None:
    class Opener:
        def open(self, request: object, timeout: int = 30) -> object:
            del timeout
            url = getattr(request, "full_url", "https://issuer.example/")
            raise urllib.error.HTTPError(str(url), status, "denied", email.message.Message(), None)

    monkeypatch.setattr(mz, "ensure_safe_public_url", lambda _url: None)
    monkeypatch.setattr(mz, "build_public_opener", lambda: Opener())

    with pytest.raises(IrDiscoveryAuthenticationDeniedError) as exc_info:
        if helper == "get":
            mz._get_text("https://issuer.example/")
        else:
            mz._post_json("https://issuer.example/api", {})

    assert exc_info.value.status_code == status
