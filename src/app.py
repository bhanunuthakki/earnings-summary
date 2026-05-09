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
    GET    /api/research/<ticker>/dates    list of available report dates (YYYY-MM-DD)
    GET    /api/research/<ticker>/html     long-form HTML report (?date=YYYY-MM-DD optional)
    GET    /api/research/<ticker>/sheet    DCF workbook .xlsx (?date=YYYY-MM-DD optional)

API surface — jobs / actions:
    POST   /api/jobs/build                 body {ticker, enable_llm?} → JobState
    POST   /api/jobs/pipeline              body {ticker} → JobState
    POST   /api/jobs/categorize            body {ticker?} → JobState
    POST   /api/upload                     multipart 'file' → list of saved names
    GET    /api/jobs                       list of recent jobs
    GET    /api/jobs/<job_id>              JobState

API surface — chat:
    POST   /api/ask                        body {query, ticker?} → context-aware Claude answer

Repo-root resolution: PORTFOLIO_REPO_ROOT env var > project root inferred from
this file. We override db / calendar_manager module paths after import so a
single process can serve a different repo's data without touching those modules.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

import calendar_manager
import chat
import db
from alias_manager import resolve_ticker
from jobs import JobManager

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_ROOT = _DEFAULT_REPO_ROOT
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
IR_DROPBOX = REPO_ROOT / "ir_documents"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_TICKER_RX = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_UPLOAD_EXTS = frozenset({".pdf", ".xlsx", ".xls", ".html", ".htm", ".txt", ".csv"})
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES
job_manager = JobManager(REPO_ROOT, code_root=CODE_ROOT)


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


def _resolve_artifact(ticker: str, suffix: str, date_str: str | None) -> Path | None:
    """Return {RESEARCH_DIR}/{TICKER}/{date}_{suffix} or the latest by name.

    `date_str` must match YYYY-MM-DD or be None. If None, we return the
    last file by sorted name (ISO date prefix gives chronological order).
    """
    ticker_dir = RESEARCH_DIR / resolve_ticker(ticker).upper()
    if not ticker_dir.exists():
        return None
    if date_str is not None:
        if not _DATE_RX.match(date_str):
            return None
        candidate = ticker_dir / f"{date_str}{suffix}"
        return candidate if candidate.exists() else None
    matches = sorted(ticker_dir.glob(f"*{suffix}"))
    return matches[-1] if matches else None


@app.route("/api/research/<ticker>")
def research_sections(ticker: str) -> object:
    path = _resolve_artifact(ticker, "_sections.json", request.args.get("date"))
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


@app.route("/api/research/<ticker>/dates")
def research_dates(ticker: str) -> object:
    """List available report dates (YYYY-MM-DD) for ticker, newest first."""
    ticker_dir = RESEARCH_DIR / resolve_ticker(ticker).upper()
    if not ticker_dir.exists():
        return jsonify([])
    dates: set[str] = set()
    for p in ticker_dir.glob("*_report.html"):
        prefix = p.name.split("_report.html", 1)[0]
        if _DATE_RX.match(prefix):
            dates.add(prefix)
    return jsonify(sorted(dates, reverse=True))


@app.route("/api/research/<ticker>/html")
def research_html(ticker: str) -> object:
    path = _resolve_artifact(ticker, "_report.html", request.args.get("date"))
    if path is None:
        abort(404)
    return send_file(path, mimetype="text/html")


@app.route("/api/research/<ticker>/sheet")
def research_sheet(ticker: str) -> object:
    path = _resolve_artifact(ticker, "_dcf.xlsx", request.args.get("date"))
    if path is None:
        abort(404)
    return send_file(
        path,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=path.name,
    )


# ---------------------------------------------------------------------------
# Jobs (build / pipeline / categorize)
# ---------------------------------------------------------------------------


def _ticker_or_400(body: dict[str, object]) -> str:
    raw = body.get("ticker")
    if not isinstance(raw, str):
        abort(400, description="ticker required")
    ticker = resolve_ticker(raw).upper()
    if not _TICKER_RX.match(ticker):
        abort(400, description=f"invalid ticker {raw!r}")
    return ticker


@app.route("/api/jobs/build", methods=["POST"])
def jobs_build() -> object:
    body = request.get_json(force=True) or {}
    ticker = _ticker_or_400(body)
    enable_llm = bool(body.get("enable_llm", False))
    state = job_manager.start_build(ticker, enable_llm=enable_llm)
    return jsonify(state.to_dict())


@app.route("/api/jobs/pipeline", methods=["POST"])
def jobs_pipeline() -> object:
    body = request.get_json(force=True) or {}
    ticker = _ticker_or_400(body)
    state = job_manager.start_pipeline(ticker)
    return jsonify(state.to_dict())


@app.route("/api/jobs/categorize", methods=["POST"])
def jobs_categorize() -> object:
    body = request.get_json(silent=True) or {}
    raw = body.get("ticker")
    ticker: str | None = None
    if isinstance(raw, str) and raw.strip():
        ticker = resolve_ticker(raw).upper()
        if not _TICKER_RX.match(ticker):
            abort(400, description=f"invalid ticker {raw!r}")
    state = job_manager.start_categorize(ticker)
    return jsonify(state.to_dict())


@app.route("/api/jobs")
def jobs_list() -> object:
    return jsonify([j.to_dict() for j in job_manager.list_recent()])


@app.route("/api/jobs/<job_id>")
def jobs_get(job_id: str) -> object:
    state = job_manager.get(job_id)
    if state is None:
        abort(404)
    return jsonify(state.to_dict())


# ---------------------------------------------------------------------------
# IR document upload
# ---------------------------------------------------------------------------


@app.route("/api/upload", methods=["POST"])
def upload_ir_documents() -> object:
    """Accept one or more files and drop them at ir_documents/<filename>.

    Filename is sanitized via werkzeug.secure_filename so path traversal can't
    escape the dropbox. Extension allowlist mirrors the IR classifier's
    supported types. The actual classification is a separate step — call
    POST /api/jobs/categorize after upload to register them.
    """
    files = request.files.getlist("file")
    if not files:
        abort(400, description="no files in request")

    IR_DROPBOX.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    rejected: list[dict[str, str]] = []
    for f in files:
        if not f or not f.filename:
            continue
        safe = secure_filename(f.filename)
        if not safe:
            rejected.append({"name": f.filename, "reason": "unsafe filename"})
            continue
        ext = Path(safe).suffix.lower()
        if ext not in _ALLOWED_UPLOAD_EXTS:
            rejected.append({"name": safe, "reason": f"extension {ext} not allowed"})
            continue
        target = IR_DROPBOX / safe
        if target.exists():
            stem = target.stem
            i = 1
            while True:
                cand = IR_DROPBOX / f"{stem}__{i}{ext}"
                if not cand.exists():
                    target = cand
                    break
                i += 1
        f.save(str(target))
        saved.append(target.name)

    return jsonify({"saved": saved, "rejected": rejected, "dropbox": str(IR_DROPBOX)})


# ---------------------------------------------------------------------------
# Chatbot — context-aware Claude (Sonnet 4.6) over the project DB
# ---------------------------------------------------------------------------


@app.route("/api/ask", methods=["POST"])
def ask() -> object:
    """POST {query, ticker?} → {answer, tickers, sources, elapsed_ms}.

    If `ticker` is omitted, we try to detect from the query against
    tracked_companies (word-boundary match on ticker + name).
    """
    body = request.get_json(force=True) or {}
    query = str(body.get("query") or "").strip()
    if not query:
        abort(400, description="missing 'query'")
    explicit_ticker = body.get("ticker")
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        contexts: list[chat.ChatContext] = []
        if isinstance(explicit_ticker, str) and explicit_ticker.strip():
            ticker = resolve_ticker(explicit_ticker).upper()
            if not _TICKER_RX.match(ticker):
                abort(400, description=f"invalid ticker {explicit_ticker!r}")
            contexts.append(chat.build_context(ticker, conn, REPO_ROOT))
        else:
            detected = chat.detect_ticker(query, conn)
            if detected:
                contexts.append(chat.build_context(detected, conn, REPO_ROOT))
        result = chat.answer(query, contexts)
        return jsonify(result)
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
