"""Owner opt-out is admission, never an authentication-triggered paid fallback."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter

from execution import fetch_fmp_earnings_calendar as calendar
from execution import fetch_fmp_news, fetch_news
from net.client import FmpClient, HostRateBudget, HttpCallError, HttpClient, require_fmp_admission

_ORIGINAL_REQUEST = requests.Session.request


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("test attempted network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.delenv("EARNINGS_SUMMARY_FMP_POLICY_FILE", raising=False)


def _policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fmp-policy.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_FMP_POLICY_FILE", str(path))
    return path


@pytest.mark.parametrize("lane", ["adapter", "shared"])
def test_disabled_blocks_before_rate_and_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lane: str
) -> None:
    _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":false}')

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("disabled provider reached rate or network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(HostRateBudget, "acquire", forbidden)
    monkeypatch.setattr(HttpClient, "set_host_rate", forbidden)
    client = HttpClient()
    with pytest.raises(HttpCallError) as caught:
        if lane == "adapter":
            FmpClient(http=client).get_json("profile")
        else:
            client.request("GET", "https://financialmodelingprep.com/stable/profile")
    assert caught.value.kind.value == "provider_disabled_by_owner"
    assert not caught.value.retryable
    assert caught.value.status_code is None


def test_disabled_auto_never_falls_back_but_manual_websearch_remains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":false}')
    calls: list[str] = []

    def websearch(ticker: str, **kwargs: object) -> list[fetch_news.NewsRow]:
        calls.append(ticker)
        return []

    monkeypatch.setattr(fetch_news, "_safe_websearch", websearch)
    for source in ("auto", "fmp"):
        unavailable: list[str] = []
        assert (
            fetch_news.collect_primary(
                ["NU"],
                source=source,
                db_path="unused",
                days=2,
                limit=10,
                unavailable_feeds=unavailable,
            )
            == []
        )
        assert unavailable == ["fmp:provider_disabled_by_owner"]
    assert calls == []
    fetch_news.collect_primary(["NU"], source="websearch", db_path="unused", days=2, limit=10)
    assert calls == ["NU"]


def test_calendar_force_cannot_override_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":false}')
    monkeypatch.setattr("sys.argv", ["calendar", "--ticker", "NU", "--force"])
    with pytest.raises(SystemExit) as caught:
        calendar.main()
    assert caught.value.code == 1
    assert "provider_disabled_by_owner" in capsys.readouterr().err


def test_news_disabled_precedes_credentials_and_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":false}')
    monkeypatch.setattr(fetch_fmp_news, "FMP_API_KEY", "")
    assert fetch_fmp_news.run(["NU"], db_path="unused", days=2, limit=10) == 1
    assert "provider_disabled_by_owner" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "null",
        '{"schema_version":1,"enabled":"false"}',
        '{"schema_version":true,"enabled":true}',
        '{"schema_version":1,"enabled":true,"extra":1}',
        "x" * 4097,
        "not-json",
    ],
)
def test_config_invalid_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    _policy(monkeypatch, tmp_path, raw)
    with pytest.raises(HttpCallError) as caught:
        require_fmp_admission()
    assert caught.value.kind.value == "provider_admission_invalid"
    assert not caught.value.retryable
    assert raw not in str(caught.value)


def test_unset_enabled_hot_disable_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    require_fmp_admission()
    path = _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":true}')
    require_fmp_admission()
    path.write_text('{"schema_version":1,"enabled":false}', encoding="utf-8")
    with pytest.raises(HttpCallError, match="provider_disabled_by_owner"):
        require_fmp_admission()
    path.unlink()
    with pytest.raises(HttpCallError, match="provider_admission_invalid"):
        require_fmp_admission()


@pytest.mark.parametrize("path", ["", "relative.json"])
def test_bad_config_path(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_FMP_POLICY_FILE", path)
    with pytest.raises(HttpCallError, match="provider_admission_invalid"):
        require_fmp_admission()


def test_approved_environment_loader_supplies_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runtime.secrets import load_project_env

    path = _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":false}')
    monkeypatch.delenv("EARNINGS_SUMMARY_FMP_POLICY_FILE")
    env = tmp_path / "runtime.env"
    env.write_text(f'EARNINGS_SUMMARY_FMP_POLICY_FILE="{path.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env))
    assert load_project_env(tmp_path)
    with pytest.raises(HttpCallError, match="provider_disabled_by_owner"):
        require_fmp_admission()


def test_disable_during_rate_wait_prevents_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":true}')

    def disable(_self: HostRateBudget, host: str) -> None:
        path.write_text('{"schema_version":1,"enabled":false}', encoding="utf-8")

    monkeypatch.setattr(HostRateBudget, "acquire", disable)
    with pytest.raises(HttpCallError, match="provider_disabled_by_owner"):
        HttpClient().request("GET", "https://financialmodelingprep.com/stable/profile")


def test_disable_during_retry_sleep_stops_second_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _policy(monkeypatch, tmp_path, '{"schema_version":1,"enabled":true}')
    calls: list[str] = []

    def request(
        _self: requests.Session, method: str, url: str, **kwargs: object
    ) -> requests.Response:
        calls.append(url)
        response = requests.Response()
        response.status_code = 500
        response.raw = BytesIO(b"{}")
        return response

    def disable(delay: float) -> None:
        path.write_text('{"schema_version":1,"enabled":false}', encoding="utf-8")

    monkeypatch.setattr(requests.Session, "request", request)
    client = HttpClient(
        rate_budget=HostRateBudget({}), sleep=disable, measurement_sink=lambda _: True
    )
    with pytest.raises(HttpCallError, match="provider_disabled_by_owner"):
        client.request("GET", "https://financialmodelingprep.com/stable/profile")
    assert len(calls) == 1


@pytest.mark.parametrize("initial_fmp", [False, True])
def test_real_redirect_transport_never_sends_disabled_fmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_fmp: bool
) -> None:
    path = _policy(
        monkeypatch,
        tmp_path,
        '{"schema_version":1,"enabled":true}'
        if initial_fmp
        else '{"schema_version":1,"enabled":false}',
    )
    sent: list[str] = []
    hooks_called: list[str] = []

    class RedirectAdapter(BaseAdapter):
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: Mapping[str, str] | None = None,
        ) -> requests.Response:
            sent.append(str(request.url))
            assert len(sent) == 1, "redirect issued a forbidden second send"
            if initial_fmp:
                path.write_text('{"schema_version":1,"enabled":false}', encoding="utf-8")
            response = requests.Response()
            response.status_code = 302
            response.url = str(request.url)
            response.request = request
            response.headers["Location"] = "https://financialmodelingprep.com/stable/profile"
            response.raw = BytesIO(b"")
            return response

        def close(self) -> None:
            pass

    def existing_hook(response: requests.Response, **kwargs: object) -> requests.Response:
        hooks_called.append(response.url)
        return response

    monkeypatch.setattr(requests.Session, "request", _ORIGINAL_REQUEST)
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", RedirectAdapter())
        session.mount("http://", RedirectAdapter())
        session.hooks["response"].append(existing_hook)
        client = HttpClient(
            session=session, rate_budget=HostRateBudget({}), measurement_sink=lambda _: True
        )
        start = (
            "https://financialmodelingprep.com/first"
            if initial_fmp
            else "https://example.test/first"
        )
        with pytest.raises(HttpCallError, match="provider_disabled_by_owner"):
            client.request("GET", start)
    assert sent == [start]
    assert hooks_called == [start]


@pytest.mark.skipif(os.name != "nt", reason="executes the real Windows batch wrapper")
@pytest.mark.parametrize("first,second,expected", [(0, 0, 0), (1, 0, 1), (0, 2, 2), (1, 2, 1)])
def test_calendar_wrapper_retains_first_failure_and_runs_fallback(
    tmp_path: Path, first: int, second: int, expected: int
) -> None:
    cron = tmp_path / "cron"
    cron.mkdir()
    wrapper = cron / "run_fetch_fmp_earnings_calendar.bat"
    source = Path(__file__).resolve().parents[1] / "cron" / wrapper.name
    wrapper.write_bytes(source.read_bytes())
    (cron / "run_python.bat").write_text(
        '@echo off\necho %~1>> "%~dp0calls.txt"\n'
        f'if "%~1"=="fetch-fmp-earnings-calendar" exit /b {first}\n'
        f"exit /b {second}\n",
        encoding="utf-8",
    )
    command = shutil.which("cmd.exe")
    assert command is not None
    result = subprocess.run(
        [command, "/d", "/c", str(wrapper)],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected
    assert (cron / "calls.txt").read_text().splitlines() == [
        "fetch-fmp-earnings-calendar",
        "refresh-expected-earnings",
    ]
