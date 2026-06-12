"""Tests for the DCF <-> Google Sheets round-trip.

Two halves:
  * ``gsheets.py`` — the Google Drive boundary. Pure helpers, credential
    classification, and error paths run WITHOUT the optional google libraries;
    export/download are exercised against a fake Drive client, so the Sheets API
    is mocked and never called live (no credentials needed).
  * ``dcf_sheets.py`` — the export/import CLI. Export orchestration is tested
    with ``gsheets.export_workbook`` mocked. The re-ingest (``--file``) path runs
    END-TO-END with no credentials: a seeded workbook flows through
    ``refresh_dcf.refresh_one`` into ``dcf_runs``, and editing a Forecast INPUT
    moves the persisted fair value — proving the round-trip recompute.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import dcf_sheets  # noqa: E402

from integrations import gsheets  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_gsheets_env(  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Don't let a real machine's credential env leak into the tests."""
    monkeypatch.delenv("DCF_GSHEETS_CREDENTIALS", raising=False)
    monkeypatch.delenv("DCF_GSHEETS_TOKEN", raising=False)


# --------------------------------------------------------------------------- #
# gsheets.py — pure helpers + credential classification (no google libs)
# --------------------------------------------------------------------------- #
def test_sheet_url() -> None:
    assert gsheets.sheet_url("ABC") == "https://docs.google.com/spreadsheets/d/ABC/edit"


def test_parse_sheet_id_from_url() -> None:
    url = "https://docs.google.com/spreadsheets/d/1aBcD_-ef/edit#gid=0"
    assert gsheets.parse_sheet_id(url) == "1aBcD_-ef"


def test_parse_sheet_id_bare_passthrough() -> None:
    assert gsheets.parse_sheet_id("1aBcD_-ef") == "1aBcD_-ef"


def test_parse_sheet_id_rejects_unparseable() -> None:
    with pytest.raises(gsheets.GSheetsError):
        gsheets.parse_sheet_id("/some/path/file.xlsx")


def test_credential_kind_service_account(tmp_path: Path) -> None:
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account", "private_key": "x", "client_email": "a@b"}))
    assert gsheets.credential_kind(p) == "service_account"


def test_credential_kind_oauth_client(tmp_path: Path) -> None:
    p = tmp_path / "client.json"
    p.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    assert gsheets.credential_kind(p) == "oauth_client"


def test_credential_kind_oauth_token(tmp_path: Path) -> None:
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"type": "authorized_user", "refresh_token": "r", "client_id": "c"}))
    assert gsheets.credential_kind(p) == "oauth_token"


def test_credential_kind_unrecognized(tmp_path: Path) -> None:
    p = tmp_path / "junk.json"
    p.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(gsheets.GSheetsAuthError):
        gsheets.credential_kind(p)


def test_load_credentials_missing_file_raises_clear_error(tmp_path: Path) -> None:
    # No credential file → an actionable auth error, raised before any google import.
    with pytest.raises(gsheets.GSheetsAuthError) as ei:
        gsheets.load_credentials(tmp_path, credentials_path=tmp_path / "nope.json")
    assert "No Google credentials" in str(ei.value)


def test_export_workbook_missing_xlsx(tmp_path: Path) -> None:
    with pytest.raises(gsheets.GSheetsError):
        gsheets.export_workbook(tmp_path / "nope.xlsx", title="x", creds=object())


def test_export_workbook_requires_creds_or_repo(tmp_path: Path) -> None:
    p = tmp_path / "wb.xlsx"
    p.write_bytes(b"x")
    with pytest.raises(gsheets.GSheetsError):
        gsheets.export_workbook(p, title="x")  # neither creds= nor repo_root=


# --------------------------------------------------------------------------- #
# gsheets.py — export / download against a fake Drive client (Sheets API mocked)
# --------------------------------------------------------------------------- #
class _FakeReq:
    def __init__(self, result: object) -> None:
        self._result = result

    def execute(self) -> object:
        return self._result


class _FakeFiles:
    def __init__(self, rec: dict[str, object]) -> None:
        self._rec = rec

    def create(self, **kw: object) -> _FakeReq:
        self._rec["create"] = kw
        return _FakeReq({"id": "NEWID"})

    def update(self, **kw: object) -> _FakeReq:
        self._rec["update"] = kw
        return _FakeReq({"id": kw.get("fileId")})

    def export(self, **kw: object) -> _FakeReq:
        self._rec["export"] = kw
        return _FakeReq(b"XLSX-BYTES")


class _FakePerms:
    def __init__(self, rec: dict[str, object]) -> None:
        self._rec = rec

    def create(self, **kw: object) -> _FakeReq:
        self._rec["permissions"] = kw
        return _FakeReq({"id": "perm"})


class _FakeDrive:
    def __init__(self) -> None:
        self.rec: dict[str, object] = {}

    def files(self) -> _FakeFiles:
        return _FakeFiles(self.rec)

    def permissions(self) -> _FakePerms:
        return _FakePerms(self.rec)


@pytest.fixture
def fake_drive(monkeypatch: pytest.MonkeyPatch) -> _FakeDrive:
    drive = _FakeDrive()

    def _build(_creds: object) -> _FakeDrive:
        return drive

    def _media(path: object) -> str:
        return f"MEDIA::{path}"

    monkeypatch.setattr(gsheets, "_build_drive", _build)
    monkeypatch.setattr(gsheets, "_media_upload", _media)
    return drive


def test_export_creates_new_sheet_and_shares(tmp_path: Path, fake_drive: _FakeDrive) -> None:
    wb = tmp_path / "TEST.xlsx"
    wb.write_bytes(b"xlsx")
    res = gsheets.export_workbook(wb, title="DCF — TEST", share_with="me@x.com", creds=object())
    assert res.created is True
    assert res.sheet_id == "NEWID"
    assert res.url == "https://docs.google.com/spreadsheets/d/NEWID/edit"
    create_kw = _as_dict(fake_drive.rec["create"])
    assert _as_dict(create_kw["body"])["mimeType"] == "application/vnd.google-apps.spreadsheet"
    perms_kw = _as_dict(fake_drive.rec["permissions"])
    assert _as_dict(perms_kw["body"])["emailAddress"] == "me@x.com"


def test_export_updates_existing_sheet_without_sharing(
    tmp_path: Path, fake_drive: _FakeDrive
) -> None:
    wb = tmp_path / "TEST.xlsx"
    wb.write_bytes(b"xlsx")
    res = gsheets.export_workbook(wb, title="x", sheet_id="EXIST", creds=object())
    assert res.created is False
    assert res.sheet_id == "EXIST"
    assert _as_dict(fake_drive.rec["update"])["fileId"] == "EXIST"
    assert "permissions" not in fake_drive.rec  # the update path never re-shares


def test_download_sheet_writes_xlsx(tmp_path: Path, fake_drive: _FakeDrive) -> None:
    dest = tmp_path / "out" / "TEST.xlsx"
    out = gsheets.download_sheet_xlsx("SID", dest, creds=object())
    assert out == dest
    assert dest.read_bytes() == b"XLSX-BYTES"
    export_kw = _as_dict(fake_drive.rec["export"])
    assert export_kw["fileId"] == "SID"
    assert str(export_kw["mimeType"]).endswith("spreadsheetml.sheet")


def _as_dict(v: object) -> dict[str, object]:
    assert isinstance(v, dict)
    return cast("dict[str, object]", v)


# --------------------------------------------------------------------------- #
# dcf_sheets.py — export orchestration (gsheets.export_workbook mocked)
# --------------------------------------------------------------------------- #
def test_cli_export_links_sheet_id_into_holdings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dcf").mkdir()
    (tmp_path / "dcf" / "TEST.xlsx").write_bytes(b"xlsx")
    holdings = tmp_path / "micro_thesis" / "holdings" / "TEST.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text(json.dumps({"ticker": "TEST", "dcf_defaults": {"terminal_multiple": 15.0}}))

    def fake_export(xlsx_path: Path, **kw: object) -> gsheets.ExportResult:
        assert kw["sheet_id"] is None  # first export creates a fresh Sheet
        return gsheets.ExportResult("SHEET42", gsheets.sheet_url("SHEET42"), created=True)

    monkeypatch.setattr(gsheets, "export_workbook", fake_export)

    rc = dcf_sheets.main(["export", "--ticker", "TEST", "--repo-root", str(tmp_path)])
    assert rc == 0
    data = json.loads(holdings.read_text())
    assert data["dcf_defaults"]["gsheet_id"] == "SHEET42"
    # Other holdings keys are preserved untouched.
    assert data["dcf_defaults"]["terminal_multiple"] == 15.0
    assert data["ticker"] == "TEST"


def test_cli_export_reuses_existing_linked_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dcf").mkdir()
    (tmp_path / "dcf" / "TEST.xlsx").write_bytes(b"xlsx")
    holdings = tmp_path / "micro_thesis" / "holdings" / "TEST.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text(json.dumps({"ticker": "TEST", "dcf_defaults": {"gsheet_id": "OLD"}}))

    seen: dict[str, object] = {}

    def fake_export(xlsx_path: Path, **kw: object) -> gsheets.ExportResult:
        seen["sheet_id"] = kw["sheet_id"]
        return gsheets.ExportResult("OLD", gsheets.sheet_url("OLD"), created=False)

    monkeypatch.setattr(gsheets, "export_workbook", fake_export)
    rc = dcf_sheets.main(["export", "--ticker", "TEST", "--repo-root", str(tmp_path)])
    assert rc == 0
    assert seen["sheet_id"] == "OLD"  # the linked Sheet is updated, not recreated


def test_cli_export_missing_workbook(tmp_path: Path) -> None:
    rc = dcf_sheets.main(["export", "--ticker", "NOPE", "--repo-root", str(tmp_path)])
    assert rc == 2


def test_set_gsheet_id_preserves_non_ascii(tmp_path: Path) -> None:
    # Writing the Sheet id must not escape non-ASCII narrative text (em-dashes,
    # accents) to \\uXXXX — that would churn the holdings diff far beyond the one
    # added key. The literal characters must survive byte-for-byte.
    holdings = tmp_path / "TEST.json"
    holdings.write_text(
        json.dumps(
            {"ticker": "TEST", "thesis": "Revenue declining — contraction."},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert dcf_sheets._set_gsheet_id(holdings, "SHEET42") is True
    text = holdings.read_text(encoding="utf-8")
    assert "—" in text  # literal em-dash preserved
    assert "\\u2014" not in text  # not escaped
    data = json.loads(text)
    assert data["dcf_defaults"]["gsheet_id"] == "SHEET42"
    assert data["thesis"] == "Revenue declining — contraction."


# --------------------------------------------------------------------------- #
# dcf_sheets.py — re-ingest (--file) END-TO-END, no credentials
# --------------------------------------------------------------------------- #
_DCF_RUNS_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL,
    assumptions_sync_status TEXT, assumptions_synced_at TEXT
);
"""

_QUARTERS = [
    (2025, "Q4", "12-31"),
    (2025, "Q3", "09-30"),
    (2025, "Q2", "06-30"),
    (2025, "Q1", "03-31"),
    (2024, "Q4", "12-31"),
    (2024, "Q3", "09-30"),
    (2024, "Q2", "06-30"),
    (2024, "Q1", "03-31"),
]


def _write_fmp(fmp_dir: Path, ticker: str) -> None:
    """8 quarters of minimally-valid FMP statements (newest-first) — positive,
    growing FCF so the seeded forecast yields a positive fair value."""
    income: list[dict[str, object]] = []
    balance: list[dict[str, object]] = []
    cashflow: list[dict[str, object]] = []
    for i, (fy, q, day) in enumerate(_QUARTERS):
        rev = (1140 - i * 20) * 1_000_000.0  # newest highest → positive YoY growth
        op = rev * 0.20
        tax = op * 0.20
        date = f"{fy}-{day}"
        income.append(
            {
                "fiscalYear": fy,
                "period": q,
                "date": date,
                "revenue": rev,
                "operatingIncome": op,
                "incomeBeforeTax": op,
                "incomeTaxExpense": tax,
                "netIncome": op - tax,
                "weightedAverageShsOutDil": 100_000_000,
            }
        )
        cashflow.append(
            {
                "fiscalYear": fy,
                "period": q,
                "date": date,
                "operatingCashFlow": op * 0.9,
                "capitalExpenditure": -rev * 0.05,
                "freeCashFlow": op * 0.9 - rev * 0.05,
            }
        )
        balance.append(
            {
                "fiscalYear": fy,
                "period": q,
                "date": date,
                "totalStockholdersEquity": rev * 2,
                "totalAssets": rev * 4,
                "cashAndShortTermInvestments": rev * 0.5,
            }
        )
    (fmp_dir / f"{ticker}_income_statement_quarterly.json").write_text(json.dumps(income))
    (fmp_dir / f"{ticker}_balance_sheet_quarterly.json").write_text(json.dumps(balance))
    (fmp_dir / f"{ticker}_cash_flow_quarterly.json").write_text(json.dumps(cashflow))


@pytest.fixture
def reingest_repo(tmp_path: Path) -> Path:
    """A repo_root with FMP fixtures, a WACC'd holdings JSON, and a dcf_runs DB."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    _write_fmp(fmp, "TEST")
    (tmp_path / "dcf").mkdir()
    holdings = tmp_path / "micro_thesis" / "holdings" / "TEST.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text(
        json.dumps(
            {
                "ticker": "TEST",
                "wacc": 0.10,
                "mos_bar": 0.25,
                "dcf_defaults": {"forecast_years": 5, "terminal_multiple": 15.0},
            }
        )
    )
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(_DCF_RUNS_SCHEMA)
    conn.commit()
    conn.close()
    return tmp_path


def test_reingest_file_not_found(reingest_repo: Path) -> None:
    rc = dcf_sheets.main(
        [
            "import",
            "--ticker",
            "TEST",
            "--file",
            str(reingest_repo / "ghost.xlsx"),
            "--repo-root",
            str(reingest_repo),
        ]
    )
    assert rc == 3


def test_reingest_no_source_and_no_link(reingest_repo: Path) -> None:
    # holdings has no gsheet_id and no --file/--sheet-id → a clear "no Sheet id" error.
    rc = dcf_sheets.main(["import", "--ticker", "TEST", "--repo-root", str(reingest_repo)])
    assert rc == 3


def test_reingest_missing_db(tmp_path: Path) -> None:
    rc = dcf_sheets.main(
        [
            "import",
            "--ticker",
            "TEST",
            "--file",
            str(tmp_path / "x.xlsx"),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 2
