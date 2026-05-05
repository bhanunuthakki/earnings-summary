"""Flask backend for the retail-investor dashboard.

Wires the existing static frontend (src/static/) to the SQLite portfolio DB
and the on-disk research artifacts produced by execution/build_artifacts.py.

Run:
    python src/app.py
    PORTFOLIO_REPO_ROOT=/path/to/repo python src/app.py   # override repo root

API surface — frontend contract:
    GET    /                              static index.html
    GET    /<asset>                       static asset (style.css, script.js)
    GET    /api/companies                 SEC ticker registry (ticker → name)
    GET    /api/portfolio                 tracked_companies WHERE list_type='portfolio'
    GET    /api/watchlist                 tracked_companies WHERE list_type='watchlist'
    POST   /api/track                     {ticker, name, list_type} → upsert
    DELETE /api/track/<ticker>            remove from tracked_companies
    GET    /api/artifacts/<ticker>        quarterly_artifacts rows
    GET    /api/thesis                    list of holdings + verdict (from thesis_state)
    GET    /api/thesis/<ticker>           one holdings JSON enriched with breach status
    GET    /api/calendar                  next earnings per tracked ticker
    GET    /api/research                  high-signal news cards (placeholder)

API surface — research artifacts (produced by execution/build_artifacts.py):
    GET    /api/research/<ticker>          latest sections.json
    GET    /api/research/<ticker>/html     long-form HTML report (transcripts embedded in §10)
    GET    /api/research/<ticker>/sheet    latest DCF workbook (.xlsx)

Repo-root resolution: PORTFOLIO_REPO_ROOT env var > project root inferred from
this file. We override db / calendar_manager module paths after import so a
single process can serve a different repo's data without touching those modules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import calendar_manager
import db
from alias_manager import resolve_ticker

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = Path(os.environ.get("PORTFOLIO_REPO_ROOT", _DEFAULT_REPO_ROOT)).resolve()

# Override module-level paths so db / calendar_manager read from the chosen repo.
db.PROJECT_ROOT = str(REPO_ROOT)
db.DATA_DIR = str(REPO_ROOT / "data")
db.DB_PATH = str(REPO_ROOT / "data" / "portfolio.db")
db.FMP_DIR = str(REPO_ROOT / "data" / "historical" / "fmp")
calendar_manager.PROJECT_ROOT = str(REPO_ROOT)
calendar_manager.FMP_DIR = str(REPO_ROOT / "data" / "historical" / "fmp")

HOLDINGS_DIR = REPO_ROOT / "micro_thesis" / "holdings"
RESEARCH_DIR = REPO_ROOT / "output" / "research"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


@app.route("/")
def index() -> object:
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:asset>")
def static_asset(asset: str) -> object:
    if asset.startswith("api/"):
        abort(404)
    return send_from_directory(STATIC_DIR, asset)


# ---------------------------------------------------------------------------
# Tracked-company CRUD
# ---------------------------------------------------------------------------


@app.route("/api/companies")
def list_companies() -> object:
    """Return the SEC ticker registry as [{ticker, name}, ...]."""
    tickers = db._load_sec_tickers()
    return jsonify([{"ticker": t, "name": n} for t, n in tickers.items()])


@app.route("/api/portfolio")
def list_portfolio() -> object:
    return jsonify([c for c in db.get_tracked_companies() if c["list_type"] == "portfolio"])


@app.route("/api/watchlist")
def list_watchlist() -> object:
    return jsonify([c for c in db.get_tracked_companies() if c["list_type"] == "watchlist"])


@app.route("/api/track", methods=["POST"])
def track() -> object:
    body = request.get_json(force=True)
    ticker = body.get("ticker")
    name = body.get("name")
    list_type = body.get("list_type")
    if not ticker or not name or list_type not in {"portfolio", "watchlist"}:
        return jsonify({"error": "ticker, name, list_type ∈ {portfolio, watchlist} required"}), 400
    db.track_company(ticker=ticker, name=name, list_type=list_type)
    return jsonify({"ok": True, "ticker": resolve_ticker(ticker).upper()})


@app.route("/api/track/<ticker>", methods=["DELETE"])
def untrack(ticker: str) -> object:
    db.remove_company(ticker)
    return jsonify({"ok": True, "ticker": ticker.upper()})


@app.route("/api/artifacts/<ticker>")
def artifacts(ticker: str) -> object:
    return jsonify(db.get_company_artifacts(ticker))


# ---------------------------------------------------------------------------
# Thesis
# ---------------------------------------------------------------------------


def _read_holdings_file(path: Path) -> dict[str, object]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _verdict_for(ticker: str) -> tuple[str, str]:
    """Map thesis_state.breach_status (if present) to (verdict, color)."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT breach_status FROM thesis_state WHERE ticker = ?",
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None or not row["breach_status"]:
        return ("pending", "gray")
    status = row["breach_status"].lower()
    if status == "ok":
        return ("intact", "green")
    if status == "warn":
        return ("watch", "yellow")
    if status == "breach":
        return ("broken", "red")
    return (status, "gray")


@app.route("/api/thesis")
def list_thesis() -> object:
    if not HOLDINGS_DIR.exists():
        return jsonify([])
    rows: list[dict[str, object]] = []
    for path in sorted(HOLDINGS_DIR.glob("*.json")):
        h = _read_holdings_file(path)
        ticker = str(h.get("ticker", path.stem))
        verdict, color = _verdict_for(ticker)
        kpis = h.get("tier_1_kpis") or []
        key_driver = kpis[0]["name"] if kpis and isinstance(kpis[0], dict) else None
        rows.append(
            {
                "ticker": ticker,
                "verdict": verdict,
                "verdict_color": color,
                "key_driver": key_driver,
                "thesis": h.get("thesis", ""),
            }
        )
    return jsonify(rows)


@app.route("/api/thesis/<ticker>")
def get_thesis(ticker: str) -> object:
    path = HOLDINGS_DIR / f"{resolve_ticker(ticker).upper()}.json"
    if not path.exists():
        abort(404)
    h = _read_holdings_file(path)
    verdict, color = _verdict_for(ticker)
    h["verdict"] = verdict
    h["verdict_color"] = color
    return jsonify(h)


# ---------------------------------------------------------------------------
# Calendar / news
# ---------------------------------------------------------------------------


@app.route("/api/calendar")
def calendar() -> object:
    """Return next upcoming earnings event per tracked company."""
    out: list[dict[str, object]] = []
    for c in db.get_tracked_companies():
        if c["list_type"] not in {"portfolio", "watchlist"}:
            continue
        ticker = str(c["ticker"])
        event = calendar_manager.get_next_earnings_event(ticker)
        if event is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "name": c["name"],
                "list_type": c["list_type"],
                "date": event["date"],
                "epsEstimated": event.get("epsEstimated"),
                "revenueEstimated": event.get("revenueEstimated"),
            }
        )
    out.sort(key=lambda r: r["date"])
    return jsonify(out)


@app.route("/api/research")
def research_feed() -> object:
    """High-signal research/news cards. Reads data/research_feed.json if present."""
    feed_path = REPO_ROOT / "data" / "research_feed.json"
    if not feed_path.exists():
        return jsonify([])
    with open(feed_path, encoding="utf-8") as f:
        return jsonify(json.load(f))


# ---------------------------------------------------------------------------
# Research artifacts (produced by execution/build_artifacts.py)
# ---------------------------------------------------------------------------


def _latest_artifact(ticker: str, suffix: str) -> Path | None:
    """Latest file matching {RESEARCH_DIR}/{TICKER}/*_{suffix} sorted by name (ISO date)."""
    ticker_dir = RESEARCH_DIR / resolve_ticker(ticker).upper()
    if not ticker_dir.exists():
        return None
    matches = sorted(ticker_dir.glob(f"*{suffix}"))
    return matches[-1] if matches else None


@app.route("/api/research/<ticker>")
def research_sections(ticker: str) -> object:
    path = _latest_artifact(ticker, "_sections.json")
    if path is None:
        return (
            jsonify(
                {
                    "error": "no research artifact built yet",
                    "hint": f"run: python execution/build_artifacts.py --ticker {ticker.upper()}",
                }
            ),
            404,
        )
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/research/<ticker>/html")
def research_html(ticker: str) -> object:
    path = _latest_artifact(ticker, "_report.html")
    if path is None:
        abort(404)
    return send_file(path, mimetype="text/html")


@app.route("/api/research/<ticker>/sheet")
def research_sheet(ticker: str) -> object:
    path = _latest_artifact(ticker, "_dcf.xlsx")
    if path is None:
        abort(404)
    return send_file(
        path,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=path.name,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
