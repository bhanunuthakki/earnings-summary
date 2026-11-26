"""Unit tests for pandas-free index constituent fetching."""

from __future__ import annotations

import pytest

from execution import load_index_constituents as loader


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_fetch_sp500_parses_html_table_without_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = "\n".join(
        f"<tr><td>{i}.A</td><td>Company {i}</td><td>Tech</td></tr>"
        if i == 1
        else f"<tr><td>T{i:04d}</td><td>Company {i}</td><td>Tech</td></tr>"
        for i in range(1, 410)
    )
    rows = rows.replace("1.A", "BRK.B")
    html = f"<table><tr><th>Symbol</th><th>Security</th><th>Sector</th></tr>{rows}</table>"

    def _fake_get(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(html)

    monkeypatch.setattr(loader.requests, "get", _fake_get)

    parsed = loader.fetch_sp500()
    assert len(parsed) == 409
    assert parsed[0] == ("BRK-B", "Company 1")


def test_fetch_russell2000_parses_csv_without_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ["Ticker,Name"] + [f"R{i:04d},Name {i}" for i in range(1505)]
    lines[1] = "BF.B,Brown-Forman"
    lines[2] = "XYZ.WS,Ignored Warrant"
    csv_text = "\n".join(lines)

    def _fake_get(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(csv_text)

    monkeypatch.setattr(loader.requests, "get", _fake_get)

    parsed = loader.fetch_russell2000()
    assert len(parsed) == 1504
    assert parsed[0] == ("BF-B", "Brown-Forman")
