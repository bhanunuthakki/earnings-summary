"""
src/output_consolidator.py
--------------------------
Consolidate every per-ticker artifact (memo HTML, thesis tracker MD, master
PDF) into a single browseable folder tree at `outputs/<TICKER>/`, plus a
top-level `outputs/index.html` portfolio dashboard. Every artifact is also
registered in the SQLite `output_artifacts` table so any downstream consumer
can query "latest memo for X" without hitting the filesystem.

Layout produced:

  outputs/
    index.html                       (portfolio + watchlist dashboard)
    <TICKER>/
      index.html                     (per-ticker landing)
      memo_<YYYY-MM-DD>.html         (copy of latest from transcripts/memos/)
      thesis_tracker_<YYYY-MM-DD>.md (copy of latest from micro_thesis/)
      master_transcripts.pdf         (copy of transcripts/master/<TICKER>_Master_Transcripts.pdf)

Idempotent: re-running just refreshes copies and re-registers latest paths.
Older versions in `outputs/<TICKER>/` are NOT pruned (manual housekeeping if
disk pressure becomes an issue).
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import db
from portfolio import PortfolioEntry, get_portfolio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Aligns with the pre-existing `output/research/<TICKER>/<DATE>_report.html`
# convention already used by other research artefacts in this project.
OUTPUTS_DIR = PROJECT_ROOT / "output" / "research"
MEMOS_SRC = PROJECT_ROOT / "transcripts" / "memos"
MASTER_SRC = PROJECT_ROOT / "transcripts" / "master"
THESIS_DIR = PROJECT_ROOT / "micro_thesis"

# Artifact kinds — kept in sync with the CHECK constraint on output_artifacts.kind.
KIND_MEMO = "memo"
KIND_TRACKER = "thesis_tracker"
KIND_MASTER = "master_pdf"
KIND_TICKER_INDEX = "ticker_index"
KIND_PORTFOLIO_INDEX = "portfolio_index"


@dataclass(frozen=True)
class TickerArtifacts:
    ticker: str
    name: str
    list_type: str
    memo_src: Path | None
    memo_dest: Path | None
    tracker_src: Path | None
    tracker_dest: Path | None
    master_src: Path | None
    master_dest: Path | None


# ---------------------------------------------------------------------------
# Source discovery — find the latest of each kind for a ticker
# ---------------------------------------------------------------------------


def _latest_memo(ticker: str) -> Path | None:
    if not MEMOS_SRC.is_dir():
        return None
    candidates = sorted(MEMOS_SRC.glob(f"{ticker}_memo_*.html"))
    return candidates[-1] if candidates else None


def _latest_tracker(ticker: str) -> Path | None:
    if not THESIS_DIR.is_dir():
        return None
    candidates = sorted(THESIS_DIR.glob(f"thesis-tracker-{ticker}-*.md"))
    return candidates[-1] if candidates else None


def _master_pdf(ticker: str) -> Path | None:
    p = MASTER_SRC / f"{ticker}_Master_Transcripts.pdf"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Per-ticker consolidation
# ---------------------------------------------------------------------------


def _copy_if_changed(src: Path, dest: Path) -> Path:
    """Copy src → dest only if dest is missing or older than src."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (
        not dest.exists()
        or dest.stat().st_mtime < src.stat().st_mtime
        or dest.stat().st_size != src.stat().st_size
    ):
        shutil.copy2(src, dest)
    return dest


def _date_from_name(p: Path) -> str:
    """Extract YYYY-MM-DD from a source filename (memo or tracker), falling
    back to the file's mtime so the dated copy in output/ always has a date."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
    if m:
        return m.group(1)
    return _dt.date.fromtimestamp(p.stat().st_mtime).isoformat()


def consolidate_ticker(entry: PortfolioEntry) -> TickerArtifacts:
    ticker = entry.ticker
    folder = OUTPUTS_DIR / ticker
    folder.mkdir(parents=True, exist_ok=True)

    memo_src = _latest_memo(ticker)
    tracker_src = _latest_tracker(ticker)
    master_src = _master_pdf(ticker)

    memo_dest: Path | None = None
    tracker_dest: Path | None = None
    master_dest: Path | None = None

    # Output filenames follow the existing `<DATE>_<kind>.<ext>` convention
    # (matches the pre-existing `<DATE>_report.html` files already in
    # output/research/<TICKER>/). Master PDF gets a stable name since
    # we only ever keep one canonical version.
    if memo_src is not None:
        dest_name = f"{_date_from_name(memo_src)}_memo.html"
        memo_dest = _copy_if_changed(memo_src, folder / dest_name)
        db.register_output_artifact(ticker, KIND_MEMO, str(memo_dest.relative_to(PROJECT_ROOT)))

    if tracker_src is not None:
        dest_name = f"{_date_from_name(tracker_src)}_tracker.md"
        tracker_dest = _copy_if_changed(tracker_src, folder / dest_name)
        db.register_output_artifact(ticker, KIND_TRACKER, str(tracker_dest.relative_to(PROJECT_ROOT)))

    if master_src is not None:
        master_dest = _copy_if_changed(master_src, folder / "master_transcripts.pdf")
        db.register_output_artifact(ticker, KIND_MASTER, str(master_dest.relative_to(PROJECT_ROOT)))

    arts = TickerArtifacts(
        ticker=ticker, name=entry.name, list_type=entry.list_type,
        memo_src=memo_src, memo_dest=memo_dest,
        tracker_src=tracker_src, tracker_dest=tracker_dest,
        master_src=master_src, master_dest=master_dest,
    )
    _write_ticker_index(arts)
    return arts


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_TICKER_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{ticker} — outputs</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
            max-width: 720px; margin: 2em auto; padding: 0 1.5em; color: #1a1a1a; }}
    h1 {{ border-bottom: 2px solid #ccc; padding-bottom: .3em; }}
    .meta {{ color: #888; font-size: .9em; margin-bottom: 1.5em; }}
    .pill {{ display: inline-block; padding: .15em .55em; border-radius: 4px;
             background: #eef; color: #225; font-size: .8em; font-weight: 600; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
    th, td {{ border: 1px solid #ddd; padding: .5em .75em; text-align: left; }}
    th {{ background: #f7f7f8; }}
    .none {{ color: #aaa; font-style: italic; }}
    a {{ color: #04509b; }}
  </style>
</head>
<body>
  <p><a href="../index.html">&larr; research dashboard</a></p>
  <h1>{ticker} <span class="pill">{list_type}</span></h1>
  <p class="meta">{name} &mdash; outputs refreshed {refreshed_at}</p>
  <table>
    <tr><th>Artifact</th><th>Latest file</th><th>Source</th></tr>
    <tr><td>HTML investor memo</td><td>{memo_link}</td><td>{memo_src}</td></tr>
    <tr><td>Thesis tracker (markdown)</td><td>{tracker_link}</td><td>{tracker_src}</td></tr>
    <tr><td>Master transcripts (PDF)</td><td>{master_link}</td><td>{master_src}</td></tr>
  </table>
</body>
</html>
"""


def _link_or_none(filename: str | None) -> str:
    if not filename:
        return '<span class="none">none on file</span>'
    return f'<a href="{filename}">{filename}</a>'


def _src_or_none(path: Path | None) -> str:
    if path is None:
        return '<span class="none">&mdash;</span>'
    try:
        rel = path.relative_to(PROJECT_ROOT)
        return f'<code>{rel.as_posix()}</code>'
    except ValueError:
        return f'<code>{path}</code>'


def _write_ticker_index(arts: TickerArtifacts) -> None:
    html = _TICKER_INDEX_HTML.format(
        ticker=arts.ticker,
        name=arts.name,
        list_type=arts.list_type,
        refreshed_at=_dt.datetime.now().isoformat(timespec="seconds"),
        memo_link=_link_or_none(arts.memo_dest.name if arts.memo_dest else None),
        tracker_link=_link_or_none(arts.tracker_dest.name if arts.tracker_dest else None),
        master_link=_link_or_none(arts.master_dest.name if arts.master_dest else None),
        memo_src=_src_or_none(arts.memo_src),
        tracker_src=_src_or_none(arts.tracker_src),
        master_src=_src_or_none(arts.master_src),
    )
    out = OUTPUTS_DIR / arts.ticker / "index.html"
    out.write_text(html, encoding="utf-8")
    db.register_output_artifact(arts.ticker, KIND_TICKER_INDEX, str(out.relative_to(PROJECT_ROOT)))


_PORTFOLIO_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>earnings-summary outputs</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
            max-width: 1100px; margin: 2em auto; padding: 0 1.5em; color: #1a1a1a; }}
    h1 {{ border-bottom: 2px solid #ccc; padding-bottom: .3em; }}
    h2 {{ margin-top: 2em; border-bottom: 1px solid #e5e5e5; padding-bottom: .2em; }}
    .meta {{ color: #888; font-size: .9em; margin-bottom: 1.5em; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .94em; }}
    th, td {{ border: 1px solid #ddd; padding: .45em .65em; text-align: left; }}
    th {{ background: #f7f7f8; }}
    td.center {{ text-align: center; }}
    .y {{ color: #1a6f1a; font-weight: 600; }}
    .n {{ color: #aaa; font-style: italic; }}
    a {{ color: #04509b; }}
  </style>
</head>
<body>
  <h1>Earnings-summary research dashboard</h1>
  <p class="meta">Refreshed {refreshed_at} &middot; {n_portfolio} portfolio names &middot; {n_watchlist} watchlist names &middot; <code>output/research/</code></p>

  <h2>Portfolio</h2>
  {portfolio_table}

  <h2>Watchlist</h2>
  {watchlist_table}
</body>
</html>
"""


def _table_for(arts_list: list[TickerArtifacts]) -> str:
    rows = ['<tr><th>Ticker</th><th>Name</th><th>Memo</th><th>Tracker</th><th>Master PDF</th><th></th></tr>']
    for a in arts_list:
        memo_cell = f'<a href="{a.ticker}/{a.memo_dest.name}">html</a>' if a.memo_dest else '<span class="n">&mdash;</span>'
        tracker_cell = f'<a href="{a.ticker}/{a.tracker_dest.name}">md</a>' if a.tracker_dest else '<span class="n">&mdash;</span>'
        master_cell = f'<a href="{a.ticker}/master_transcripts.pdf">pdf</a>' if a.master_dest else '<span class="n">&mdash;</span>'
        landing = f'<a href="{a.ticker}/index.html">open</a>'
        rows.append(
            f'<tr><td><strong>{a.ticker}</strong></td><td>{a.name}</td>'
            f'<td class="center">{memo_cell}</td><td class="center">{tracker_cell}</td>'
            f'<td class="center">{master_cell}</td><td class="center">{landing}</td></tr>'
        )
    return f'<table>\n  ' + '\n  '.join(rows) + '\n</table>'


def write_portfolio_index(arts_list: list[TickerArtifacts]) -> Path:
    portfolio = [a for a in arts_list if a.list_type == "portfolio"]
    watchlist = [a for a in arts_list if a.list_type == "watchlist"]
    html = _PORTFOLIO_INDEX_HTML.format(
        refreshed_at=_dt.datetime.now().isoformat(timespec="seconds"),
        n_portfolio=len(portfolio), n_watchlist=len(watchlist),
        portfolio_table=_table_for(portfolio),
        watchlist_table=_table_for(watchlist),
    )
    out = OUTPUTS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    db.register_output_artifact("__PORTFOLIO__", KIND_PORTFOLIO_INDEX, str(out.relative_to(PROJECT_ROOT)))
    return out


# ---------------------------------------------------------------------------
# Public entry: consolidate everything
# ---------------------------------------------------------------------------


def consolidate_all(include_watchlist: bool = True) -> list[TickerArtifacts]:
    db.init_db()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    portfolio = get_portfolio(include_watchlist=include_watchlist)
    arts = [consolidate_ticker(e) for e in portfolio]
    write_portfolio_index(arts)
    return arts
