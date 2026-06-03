"""DCF ⇄ Google Sheets round-trip.

Push a ticker's `dcf/<TICKER>.xlsx` to a Google Sheet so assumptions can be edited
in the browser, then pull the edited Sheet back and recompute the valuation.

Subcommands:
  export  --ticker T [--share-with EMAIL] [--new]      push dcf/<T>.xlsx -> Sheet
  import  --ticker T [--sheet-id ID|URL | --file PATH]  pull Sheet -> recompute dcf_runs
  auth                                                  one-time OAuth consent

Mirrors `execution/refresh_ir_kpis.py`: fetch an external spreadsheet, then ingest
it through the existing pipeline. Here "ingest" = place the .xlsx at
`dcf/<TICKER>.xlsx` and drive `refresh_dcf.refresh_one`, which reads the (now
user-edited) Forecast INPUTS, recomputes the Valuation, and upserts `dcf_runs`.

The Sheet is purely an input editor: like the Excel loop, recompute always
happens in Python here — pushing/pulling only moves the workbook to/from the
browser.

Credentials (see directives/dcf_gsheets_setup.md) are needed only for the live Sheets
calls. The re-ingest `--file` mode takes an already-downloaded .xlsx and needs no
credentials — the credential-free, higher-value half of the round-trip.

Examples:
  python execution/dcf_sheets.py export --ticker NU --share-with you@example.com
  python execution/dcf_sheets.py import --ticker NU            # pull linked Sheet
  python execution/dcf_sheets.py import --ticker NU --file dcf/NU.xlsx
  python execution/dcf_sheets.py auth
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling execution/ modules

import refresh_dcf  # noqa: E402

from integrations import gsheets  # noqa: E402

DCF_DIR_NAME = "dcf"
STAGING_DIR = Path("data") / "dcf_sheets"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "export":
        return _run_export(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "auth":
        return _run_auth(args)
    return 2  # pragma: no cover - argparse `required=True` makes this unreachable


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def _run_export(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()
    xlsx = repo_root / DCF_DIR_NAME / f"{ticker}.xlsx"
    if not xlsx.exists():
        return _emit(
            {"ticker": ticker, "status": "error", "reason": f"no DCF workbook at {xlsx}"}, code=2
        )

    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    holdings = _load_json(holdings_path)
    existing_id = None if args.new else _get_gsheet_id(holdings)

    # Service-account exports create a Sheet owned by the service account; without
    # an editor share the user can never open it. Require --share-with there.
    creds_path = gsheets.default_credentials_path(repo_root)
    if (
        not existing_id
        and creds_path.exists()
        and gsheets.credential_kind(creds_path) == "service_account"
        and not args.share_with
    ):
        return _emit(
            {
                "ticker": ticker,
                "status": "error",
                "reason": "service-account export needs --share-with EMAIL so you can open the "
                "created Sheet (it is owned by the service account).",
            },
            code=2,
        )

    try:
        result = gsheets.export_workbook(
            xlsx,
            title=f"DCF — {ticker}",
            sheet_id=existing_id,
            share_with=args.share_with,
            repo_root=repo_root,
        )
    except gsheets.GSheetsError as e:
        return _emit({"ticker": ticker, "status": "error", "reason": str(e)}, code=3)

    holdings_updated = _set_gsheet_id(holdings_path, result.sheet_id)
    payload: dict[str, object] = {
        "ticker": ticker,
        "status": "ok",
        "action": "created" if result.created else "updated",
        "sheet_id": result.sheet_id,
        "url": result.url,
        "holdings_updated": holdings_updated,
    }
    if not holdings_path.exists():
        payload["warning"] = (
            f"no holdings JSON at {holdings_path}; Sheet id not stored — pass --sheet-id on import"
        )
    return _emit(payload)


# --------------------------------------------------------------------------- #
# import (re-ingest)
# --------------------------------------------------------------------------- #
def _run_import(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return _emit({"ticker": ticker, "status": "error", "reason": f"no DB at {db_path}"}, code=2)

    dest = repo_root / DCF_DIR_NAME / f"{ticker}.xlsx"
    source, sheet_id, err = _resolve_source_xlsx(args, repo_root, ticker)
    if err is not None:
        return _emit({"ticker": ticker, "status": "error", "reason": err}, code=3)
    assert source is not None

    # Place the pulled workbook at the canonical path so the served /dcf/<T> route
    # and the next refresh both see the user's edits. Skip the copy if --file
    # already points at the canonical workbook.
    if source.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)

    # Recompute in-process: refresh_one reads the edited Forecast INPUTS, recomputes
    # the Valuation, and upserts dcf_runs (the same chain `refresh_dcf` runs).
    refresh = refresh_dcf.refresh_one(
        ticker, repo_root, db_path, valuation_year=args.valuation_year
    )
    status = str(refresh.get("status"))
    payload: dict[str, object] = {
        "ticker": ticker,
        "status": "ok" if status == "ok" else "failed",
        "source": str(source),
        "sheet_id": sheet_id,
        "workbook": str(dest),
        "refresh": refresh,
    }
    return _emit(payload, code=0 if status == "ok" else 1)


def _resolve_source_xlsx(
    args: argparse.Namespace, repo_root: Path, ticker: str
) -> tuple[Path | None, str | None, str | None]:
    """Resolve the source .xlsx for an import: (path, sheet_id, error).

    `--file` short-circuits to a local workbook (no credentials). Otherwise the
    Sheet id is taken from `--sheet-id` or the ticker's holdings JSON, and the
    Sheet is downloaded to the staging dir.
    """
    if args.file is not None:
        f = args.file.resolve()
        if not f.exists():
            return None, None, f"--file not found: {f}"
        return f, None, None

    sheet_id = gsheets.parse_sheet_id(args.sheet_id) if args.sheet_id else None
    if sheet_id is None:
        holdings = _load_json(repo_root / "micro_thesis" / "holdings" / f"{ticker}.json")
        sheet_id = _get_gsheet_id(holdings)
    if not sheet_id:
        return (
            None,
            None,
            "no Sheet id — pass --sheet-id/--file, or `export` first to link one in holdings",
        )

    staging = repo_root / STAGING_DIR / f"{ticker}_from_sheet.xlsx"
    try:
        gsheets.download_sheet_xlsx(sheet_id, staging, repo_root=repo_root)
    except gsheets.GSheetsError as e:
        return None, sheet_id, str(e)
    return staging, sheet_id, None


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def _run_auth(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    try:
        token_path = gsheets.authorize_interactive(repo_root)
    except gsheets.GSheetsError as e:
        return _emit({"status": "error", "reason": str(e)}, code=3)
    return _emit({"status": "ok", "token_path": str(token_path)})


# --------------------------------------------------------------------------- #
# holdings JSON helpers (Sheet id lives at dcf_defaults.gsheet_id)
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast("dict[str, object]", data)


def _get_gsheet_id(holdings: dict[str, object] | None) -> str | None:
    if not holdings:
        return None
    dd = holdings.get("dcf_defaults")
    if not isinstance(dd, dict):
        return None
    gid = cast("dict[str, object]", dd).get("gsheet_id")
    return gid if isinstance(gid, str) and gid else None


def _set_gsheet_id(path: Path, sheet_id: str) -> bool:
    """Write `dcf_defaults.gsheet_id` into the holdings JSON. False if unchanged/absent.

    Surgical: preserves every other key (and their order) and only rewrites when
    the stored id actually changes, to keep the holdings diff minimal.
    """
    data = _load_json(path)
    if data is None:
        return False
    dd_raw = data.get("dcf_defaults")
    dd = cast("dict[str, object]", dd_raw) if isinstance(dd_raw, dict) else {}
    if dd.get("gsheet_id") == sheet_id:
        return False
    dd["gsheet_id"] = sheet_id
    data["dcf_defaults"] = dd
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _emit(payload: dict[str, object], *, code: int = 0) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return code


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", help="push dcf/<T>.xlsx to a Google Sheet")
    e.add_argument("--ticker", required=True)
    e.add_argument(
        "--share-with",
        dest="share_with",
        help="email to grant editor access (required for service-account credentials)",
    )
    e.add_argument(
        "--new",
        action="store_true",
        help="create a fresh Sheet even if one is already linked in holdings",
    )
    e.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)

    i = sub.add_parser("import", help="pull the edited Sheet and recompute the DCF")
    i.add_argument("--ticker", required=True)
    src = i.add_mutually_exclusive_group()
    src.add_argument(
        "--sheet-id",
        dest="sheet_id",
        help="Sheet id or URL to pull (default: holdings dcf_defaults.gsheet_id)",
    )
    src.add_argument(
        "--file",
        type=Path,
        help="a local .xlsx already downloaded — recompute without any Google call",
    )
    i.add_argument(
        "--valuation-year",
        dest="valuation_year",
        type=int,
        default=date.today().year,
        help="cutoff: > this is forecast, <= is actuals. Default: current calendar year.",
    )
    i.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)

    a = sub.add_parser("auth", help="one-time OAuth browser consent (mints the token)")
    a.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)

    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
