"""Research cockpit — the landing screen (master build P1.2).

One dense row per holding answering "which holding needs my attention today?"
without leaving the Overview tab:

* **Thesis health** — verdict badge from the latest ``thesis_evaluations`` row
  (breached/warned rule names in the hover), plus tier-1 KPI deltas (latest vs
  prior quarter, toned by any matching break-rule status).
* **Valuation** — price & day move from the FMP profile cache on disk, the DCF
  fair-value gap recomputed from the latest ``dcf_runs`` row's own price + fair
  value (the stored ``over_under_pct`` mixes two writer conventions, and a
  fresher external price would silently mix currencies), and the PEG ratio from
  the §Valuation cache where present.
* **Events** — next earnings date from the FMP earnings-calendar cache (the
  read-only twin of ``sources.earnings_calendar``: a render path must not write
  ``source_calls`` telemetry rows), unreviewed (pending) alerts, documents
  fetched since the last report build, and open report comments.

Evaluation-list names get a thinner row variant (no KPI chips, tighter type).
The old per-column ops-freshness tables shrink to one staleness dot per row
with the detail in its hover title.

Built directly over ``data/portfolio.db`` + the on-disk FMP caches. Every
enrichment query degrades to empty on a missing table/column
(``sqlite3.OperationalError``) so minimal hand-rolled test schemas and partial
DBs render a valid — just sparser — cockpit. Pure reads, no mutations, no
network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import cast

from pipeline.dashboard_status import DashboardRow, build_dashboard_rows
from report.renderers.numfmt import fmt_date, fmt_pct, fmt_pp, fmt_reltime

# Worst-wins ordering for thesis verdicts and rule statuses; doubles as the
# attention sort key (breach floats to the top of the cockpit).
_STATUS_RANK: dict[str, int] = {"breach": 0, "broken": 0, "warn": 1, "watch": 1, "unresolved": 2}
_STATUS_TONE: dict[str, str] = {
    "breach": "bad",
    "broken": "bad",
    "warn": "warn",
    "watch": "warn",
    "ok": "ok",
    "intact": "ok",
}

# Staleness-dot thresholds (days). The FMP cycle is daily and builds are
# weekly-ish, so a few quiet days is normal; beyond these the cron is broken.
_FMP_WARN_DAYS, _FMP_BAD_DAYS = 3.0, 14.0
_BUILD_WARN_DAYS, _BUILD_BAD_DAYS = 10.0, 30.0

_MAX_KPI_CHIPS = 3


@dataclass(frozen=True)
class KpiDelta:
    """One tier-1 KPI's move between its two most recent facts."""

    name: str
    unit: str
    latest_value: float
    prior_value: float
    latest_period: str  # ISO date
    prior_period: str  # ISO date
    tone: str  # "bad" | "warn" | "ok" | "neutral" (from a matching break rule)

    @property
    def is_pp(self) -> bool:
        return "percent" in self.unit.lower() or self.unit.strip() in {"%", "pp"}

    @property
    def magnitude(self) -> float:
        """Sort key: pp move for percent KPIs, relative % move otherwise."""
        if self.is_pp:
            return abs(self.latest_value - self.prior_value)
        if self.prior_value == 0:
            return 0.0
        return abs((self.latest_value - self.prior_value) / abs(self.prior_value)) * 100.0

    @property
    def delta_display(self) -> str:
        if self.is_pp:
            return fmt_pp(self.latest_value - self.prior_value)
        if self.prior_value == 0:
            return "n/a"
        rel = (self.latest_value - self.prior_value) / abs(self.prior_value) * 100.0
        return fmt_pct(rel, signed=True)


@dataclass(frozen=True)
class CockpitRow:
    """Everything one holding's cockpit row renders."""

    base: DashboardRow
    name: str | None = None
    # Valuation
    price: float | None = None
    day_move_pct: float | None = None  # percent points (FMP changePercentage)
    price_asof: str | None = None  # ISO datetime (profile-cache mtime)
    fv_gap_pct: float | None = None  # live_price/npv_per_share - 1, in % (+ = above FV)
    fair_value: float | None = None  # dcf_runs.npv_per_share
    dcf_price: float | None = None  # price the gap was computed against
    dcf_date: str | None = None  # dcf_runs.valuation_date
    peg_ratio: float | None = None
    # Events
    next_earnings: str | None = None  # ISO date
    pending_alerts: int = 0
    new_docs: int = 0
    # Thesis detail
    kpi_deltas: list[KpiDelta] = field(default_factory=list["KpiDelta"])
    rule_summary: str | None = None  # breached/warned rule names for the badge hover
    evaluated_at: str | None = None

    @property
    def attention_key(self) -> tuple[int, int, int, str]:
        status = (self.base.breach_status or "").lower()
        return (
            _STATUS_RANK.get(status, 3),
            -self.pending_alerts,
            -self.new_docs,
            self.base.ticker,
        )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_cockpit_rows(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, list[CockpitRow]]:
    """Assemble ``{"portfolio": [...], "evaluation": [...]}``, attention-sorted
    (breach first, then pending alerts, then new docs, then ticker).

    Layers batch enrichment queries + per-ticker disk-cache reads over the
    existing :func:`build_dashboard_rows` (which stays the ``/api/dashboard``
    contract).
    """
    ref = now or datetime.now(UTC)
    base_rows = build_dashboard_rows(conn, repo_root)
    tickers = [r.ticker for rows in base_rows.values() for r in rows]
    portfolio_tickers = {r.ticker for r in base_rows.get("portfolio", [])}

    names = _company_names(conn, tickers)
    alerts = _pending_alert_counts(conn)
    dcf = _latest_dcf_runs(conn)
    docs = _new_doc_counts(conn)
    evals = _latest_evaluations(conn)
    kpi_facts = _tier1_kpi_deltas(conn, portfolio_tickers)

    out: dict[str, list[CockpitRow]] = {}
    for list_type, rows in base_rows.items():
        built: list[CockpitRow] = []
        for row in rows:
            t = row.ticker
            price, day_move, price_asof = _profile_quote(repo_root, t)
            rule_summary, evaluated_at, rule_tones = evals.get(t, (None, None, {}))
            deltas = _toned(kpi_facts.get(t, []), rule_tones) if t in portfolio_tickers else []
            fv_gap, fair_value, dcf_price, dcf_date = dcf.get(t, (None, None, None, None))
            built.append(
                CockpitRow(
                    base=row,
                    name=names.get(t),
                    price=price,
                    day_move_pct=day_move,
                    price_asof=price_asof,
                    fv_gap_pct=fv_gap,
                    fair_value=fair_value,
                    dcf_price=dcf_price,
                    dcf_date=dcf_date,
                    peg_ratio=_peg_ratio(repo_root, t),
                    next_earnings=_next_earnings(repo_root, t, ref),
                    pending_alerts=alerts.get(t, 0),
                    new_docs=docs.get(t, 0),
                    kpi_deltas=deltas,
                    rule_summary=rule_summary,
                    evaluated_at=evaluated_at,
                )
            )
        built.sort(key=lambda r: r.attention_key)
        out[list_type] = built
    return out


def _safe_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    """Run an enrichment query; a missing table/column degrades to no rows
    (hand-rolled test schemas + partial DBs render a sparser cockpit)."""
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def _company_names(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    rows = _safe_rows(
        conn,
        f"SELECT ticker, name FROM tracked_companies WHERE ticker IN ({marks})",
        tuple(tickers),
    )
    return {str(r["ticker"]): str(r["name"]) for r in rows if r["name"]}


def _pending_alert_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = _safe_rows(
        conn, "SELECT ticker, COUNT(*) AS n FROM alerts WHERE status='pending' GROUP BY ticker"
    )
    return {str(r["ticker"]): int(r["n"]) for r in rows}


def _latest_dcf_runs(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float | None, float | None, float | None, str | None]]:
    """ticker -> (fv_gap_pct, npv_per_share, live_price, valuation_date), latest
    run per ticker. The gap is RECOMPUTED as live_price / npv_per_share − 1
    rather than read from ``over_under_pct`` — the bank/holdco builders stored
    that column in a different convention (percent upside) than refresh_dcf's
    documented ratio, and the row's own two fields are convention-proof and
    currency-consistent with each other."""
    rows = _safe_rows(
        conn,
        "SELECT ticker, valuation_date, npv_per_share, live_price "
        "FROM dcf_runs ORDER BY ticker, created_at DESC, id DESC",
    )
    out: dict[str, tuple[float | None, float | None, float | None, str | None]] = {}
    for r in rows:
        t = str(r["ticker"])
        if t in out:
            continue
        fv = float(r["npv_per_share"]) if r["npv_per_share"] is not None else None
        px = float(r["live_price"]) if r["live_price"] is not None else None
        gap = (px / fv - 1.0) * 100.0 if fv is not None and fv > 0 and px is not None else None
        out[t] = (gap, fv, px, str(r["valuation_date"]) if r["valuation_date"] else None)
    return out


def _new_doc_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Documents fetched after the ticker's last report build. ``julianday``
    bridges the two timestamp spellings in play ('T' vs space separator)."""
    rows = _safe_rows(
        conn,
        "SELECT tc.ticker AS ticker, COUNT(d.id) AS n "
        "FROM tracked_companies tc JOIN documents d ON d.ticker = tc.ticker "
        "WHERE tc.last_built_at IS NOT NULL "
        "  AND julianday(d.fetched_at) > julianday(tc.last_built_at) "
        "GROUP BY tc.ticker",
    )
    return {str(r["ticker"]): int(r["n"]) for r in rows}


def _latest_evaluations(
    conn: sqlite3.Connection,
) -> dict[str, tuple[str | None, str | None, dict[str, str]]]:
    """ticker -> (rule summary for the badge hover, evaluated_at,
    kpi_name -> worst rule status) from the latest thesis evaluation."""
    rows = _safe_rows(
        conn,
        "SELECT ticker, evaluated_at, rule_evaluations_json "
        "FROM thesis_evaluations ORDER BY ticker, evaluated_at DESC",
    )
    out: dict[str, tuple[str | None, str | None, dict[str, str]]] = {}
    for r in rows:
        t = str(r["ticker"])
        if t in out:
            continue
        tones: dict[str, str] = {}
        flagged: dict[str, list[str]] = {"breach": [], "warn": []}
        try:
            rules = json.loads(str(r["rule_evaluations_json"] or "[]"))
        except json.JSONDecodeError:
            rules = []
        if isinstance(rules, list):
            for rule in cast("list[object]", rules):
                if not isinstance(rule, dict):
                    continue
                rec = cast("dict[str, object]", rule)
                kpi = rec.get("kpi_name")
                status = str(rec.get("status") or "").lower()
                if not isinstance(kpi, str) or not kpi:
                    continue
                prev = tones.get(kpi)
                if prev is None or _STATUS_RANK.get(status, 3) < _STATUS_RANK.get(prev, 3):
                    tones[kpi] = status
                if status in flagged and kpi not in flagged[status]:
                    flagged[status].append(kpi)
        parts = [f"{status}: {', '.join(kpis)}" for status, kpis in flagged.items() if kpis]
        summary = " · ".join(parts) if parts else None
        out[t] = (summary, str(r["evaluated_at"]) if r["evaluated_at"] else None, tones)
    return out


def _tier1_kpi_deltas(conn: sqlite3.Connection, tickers: set[str]) -> dict[str, list[KpiDelta]]:
    """Latest-vs-prior move per tier-1 KPI definition (portfolio names only).
    Superseded facts are excluded; tone is stamped later from the evaluation's
    rule statuses (the definitions carry no good-direction signal)."""
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    rows = _safe_rows(
        conn,
        "SELECT f.ticker AS ticker, d.id AS def_id, d.name AS name, f.unit AS unit, "
        "       f.period_end AS period_end, f.value AS value "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE d.threshold_tier = 'tier_1_break' AND f.ticker IN (" + marks + ") "
        "  AND f.id NOT IN (SELECT supersedes_id FROM kpi_facts "
        "                   WHERE supersedes_id IS NOT NULL) "
        "ORDER BY f.ticker, d.id, f.period_end DESC",
        tuple(sorted(tickers)),
    )
    series: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for r in rows:
        key = (str(r["ticker"]), int(r["def_id"]))
        bucket = series.setdefault(key, [])
        if len(bucket) < 2:
            bucket.append(r)
    out: dict[str, list[KpiDelta]] = {}
    for (ticker, _def_id), pair in series.items():
        if len(pair) < 2:
            continue
        latest, prior = pair
        try:
            latest_v, prior_v = float(latest["value"]), float(prior["value"])
        except (TypeError, ValueError):
            continue
        out.setdefault(ticker, []).append(
            KpiDelta(
                name=str(latest["name"]),
                unit=str(latest["unit"] or ""),
                latest_value=latest_v,
                prior_value=prior_v,
                latest_period=str(latest["period_end"])[:10],
                prior_period=str(prior["period_end"])[:10],
                tone="neutral",
            )
        )
    return out


def _toned(deltas: list[KpiDelta], rule_tones: dict[str, str]) -> list[KpiDelta]:
    """Largest movers first, capped, with each chip toned by any break rule that
    references the same KPI name in the latest evaluation."""
    toned = [
        KpiDelta(
            name=d.name,
            unit=d.unit,
            latest_value=d.latest_value,
            prior_value=d.prior_value,
            latest_period=d.latest_period,
            prior_period=d.prior_period,
            tone=_STATUS_TONE.get(rule_tones.get(d.name, ""), "neutral"),
        )
        for d in deltas
    ]
    toned.sort(key=lambda d: (-d.magnitude, d.name))
    return toned[:_MAX_KPI_CHIPS]


# --------------------------------------------------------------------------- #
# Disk caches (read-only; no source_calls logging on a render path)
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile_quote(repo_root: Path, ticker: str) -> tuple[float | None, float | None, str | None]:
    """(price, day-move %, as-of) from ``data/historical/fmp/<T>_profile.json``.
    As-of is the file mtime — the price is from the last FMP cycle, which the
    staleness dot already surfaces; a render path must not hit the network."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    payload = _read_json(path)
    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        return None, None, None
    raw_price = rec.get("price")
    price = float(raw_price) if isinstance(raw_price, (int, float)) and raw_price > 0 else None
    raw_move = rec.get("changePercentage", rec.get("changesPercentage"))
    move = float(raw_move) if isinstance(raw_move, (int, float)) else None
    asof = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    return price, move, asof


def _peg_ratio(repo_root: Path, ticker: str) -> float | None:
    """PEG from the §Valuation cache (``data/valuation_basis/<T>.json``). A
    tolerant field pluck, not ``compute.valuation_basis.load`` — the cockpit
    must survive cache-shape drift and must not import the LLM compute path."""
    payload = _read_json(repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json")
    if not isinstance(payload, dict):
        return None
    v = cast("dict[str, object]", payload).get("peg_ratio")
    return float(v) if isinstance(v, (int, float)) else None


def _next_earnings(repo_root: Path, ticker: str, now: datetime) -> str | None:
    """Earliest calendar date >= today from the FMP earnings-calendar cache.
    Read-only twin of ``sources.earnings_calendar._try_fmp_cache`` minus its
    ``source_calls`` logging (a dashboard render is not a sourcing decision)."""
    payload = _read_json(
        repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_earnings_calendar.json"
    )
    if not isinstance(payload, list):
        return None
    today = now.date().isoformat()
    upcoming: list[str] = []
    for item in cast("list[object]", payload):
        if not isinstance(item, dict):
            continue
        ds = cast("dict[str, object]", item).get("date")
        if isinstance(ds, str) and len(ds) >= 10 and ds[:10] >= today:
            upcoming.append(ds[:10])
    return min(upcoming) if upcoming else None


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #


def render_research_cockpit(
    rows_by_list: dict[str, list[CockpitRow]],
    *,
    now: datetime | None = None,
) -> str:
    """The cockpit fragment: a full-density Portfolio table, a thinner
    Evaluation table, and the cockpit's own ``<style>`` block (shared tokens
    via ``var(--ok)``/``--warn``/``--bad`` from the shell palette)."""
    ref = now or datetime.now(UTC)
    portfolio = rows_by_list.get("portfolio", [])
    evaluation = rows_by_list.get("evaluation", [])
    return "".join(
        [
            _COCKPIT_CSS,
            _render_list_section("Portfolio", portfolio, ref, thin=False),
            _render_list_section("Evaluation", evaluation, ref, thin=True),
        ]
    )


def _render_list_section(title: str, rows: list[CockpitRow], now: datetime, *, thin: bool) -> str:
    if not rows:
        body = f"<p class='empty'>No {escape(title.lower())} tickers.</p>"
    else:
        head = (
            "<thead><tr>"
            "<th>Ticker</th><th>Thesis</th>"
            + ("<th>Tier-1 moves</th>" if not thin else "")
            + "<th class='num'>Price</th><th class='num'>vs DCF FV</th>"
            "<th class='num'>PEG</th><th>Next ER</th><th>Inbox</th>"
            "<th class='dot-col' title='Ops freshness'>&#9679;</th>"
            "</tr></thead>"
        )
        body_rows = "".join(_render_row(r, now, thin=thin) for r in rows)
        cls = "cockpit-table cockpit-thin" if thin else "cockpit-table"
        body = f"<table class='{cls}'>{head}<tbody>{body_rows}</tbody></table>"
    return (
        f"<section class='list-section cockpit-section'>"
        f"<h2>{escape(title)} <span class='count'>({len(rows)})</span></h2>"
        f"{body}</section>"
    )


def _render_row(row: CockpitRow, now: datetime, *, thin: bool) -> str:
    t = escape(row.base.ticker)
    name_attr = f" title='{escape(row.name)}'" if row.name else ""
    cells = [
        f"<td class='ticker'{name_attr}><a href='/ticker/{t}'>{t}</a></td>",
        f"<td>{_verdict_badge(row, now)}</td>",
    ]
    if not thin:
        cells.append(f"<td class='kpi-moves'>{_kpi_chips(row.kpi_deltas)}</td>")
    cells.extend(
        [
            f"<td class='num'>{_price_cell(row)}</td>",
            f"<td class='num'>{_fv_gap_cell(row)}</td>",
            f"<td class='num'>{_peg_cell(row)}</td>",
            f"<td>{_earnings_cell(row, now)}</td>",
            f"<td>{_inbox_cell(row)}</td>",
            f"<td class='dot-col'>{_staleness_dot(row, now)}</td>",
        ]
    )
    return "<tr>" + "".join(cells) + "</tr>"


def _muted(text: str = "&mdash;") -> str:
    return f"<span class='muted'>{text}</span>"


def _verdict_badge(row: CockpitRow, now: datetime) -> str:
    status = row.base.breach_status
    if status is None:
        return _muted()
    tone = _STATUS_TONE.get(status.lower(), "muted")
    bits: list[str] = []
    if row.rule_summary:
        bits.append(row.rule_summary)
    if row.evaluated_at:
        bits.append(f"evaluated {fmt_reltime(row.evaluated_at, now=now)}")
    title = f" title='{escape(' — '.join(bits))}'" if bits else ""
    return f"<span class='cockpit-badge b-{tone}'{title}>{escape(status)}</span>"


def _kpi_chips(deltas: list[KpiDelta]) -> str:
    if not deltas:
        return _muted()
    chips: list[str] = []
    for d in deltas:
        short = d.name.split(" (")[0]
        if len(short) > 18:
            short = short[:17] + "…"
        title = (
            f"{d.name}: {d.prior_value:g} → {d.latest_value:g} {d.unit} "
            f"({d.prior_period} → {d.latest_period})"
        )
        chips.append(
            f"<span class='kpi-chip chip-{d.tone}' title='{escape(title)}'>"
            f"{escape(short)} <b>{escape(d.delta_display)}</b></span>"
        )
    return "".join(chips)


def _price_cell(row: CockpitRow) -> str:
    if row.price is None:
        return _muted()
    move = ""
    if row.day_move_pct is not None:
        tone = "pos" if row.day_move_pct >= 0 else "neg"
        move = f" <span class='{tone}'>{escape(fmt_pct(row.day_move_pct, signed=True))}</span>"
    title = (
        f" title='last FMP quote {escape(fmt_reltime(row.price_asof))}'" if row.price_asof else ""
    )
    return f"<span{title}>${row.price:,.2f}{move}</span>"


def _fv_gap_cell(row: CockpitRow) -> str:
    if row.fv_gap_pct is None:
        return _muted()
    # over_under > 0 — price ABOVE fair value (rich); < 0 — below (cheap).
    tone = "neg" if row.fv_gap_pct > 0 else "pos"
    bits: list[str] = []
    if row.fair_value is not None and row.dcf_price is not None:
        bits.append(f"DCF FV ${row.fair_value:,.2f} vs ${row.dcf_price:,.2f}")
    if row.dcf_date:
        bits.append(f"run {row.dcf_date}")
    title = f" title='{escape(' — '.join(bits))}'" if bits else ""
    return f"<span class='{tone}'{title}>{escape(fmt_pct(row.fv_gap_pct, signed=True))}</span>"


def _peg_cell(row: CockpitRow) -> str:
    if row.peg_ratio is None:
        return _muted()
    return f"{row.peg_ratio:.1f}"


def _earnings_cell(row: CockpitRow, now: datetime) -> str:
    if row.next_earnings is None:
        return _muted()
    days = (datetime.fromisoformat(row.next_earnings).date() - now.date()).days
    cls = " class='er-soon'" if days <= 7 else ""
    rel = "today" if days == 0 else f"in {days}d"
    return (
        f"<span{cls} title='{escape(rel)}'>"
        f"{escape(fmt_date(row.next_earnings, include_year=False))}</span>"
    )


def _inbox_cell(row: CockpitRow) -> str:
    pills: list[str] = []
    if row.pending_alerts:
        pills.append(
            f"<a class='pill pill-bad' href='/feed?ticker={escape(row.base.ticker)}"
            f"&status=pending' title='unreviewed alerts'>{row.pending_alerts} alert"
            f"{'s' if row.pending_alerts != 1 else ''}</a>"
        )
    if row.new_docs:
        pills.append(
            f"<span class='pill pill-accent' title='documents fetched since the last "
            f"report build'>{row.new_docs} new doc{'s' if row.new_docs != 1 else ''}</span>"
        )
    if row.base.open_comments_count:
        n = row.base.open_comments_count
        pills.append(
            f"<span class='pill pill-warn' title='open report comments'>"
            f"{n} comment{'s' if n != 1 else ''}</span>"
        )
    return "".join(pills) if pills else _muted()


def _staleness_dot(row: CockpitRow, now: datetime) -> str:
    """One dot summarising ops freshness; the per-column detail the old status
    tables carried lives in the hover title."""
    fmp_age = _age_days(row.base.fmp_last_pulled, now)
    build_age = _age_days(row.base.last_build_at, now)
    tone = "ok"
    if fmp_age is None or build_age is None:
        tone = "bad"
    else:
        if fmp_age > _FMP_WARN_DAYS or build_age > _BUILD_WARN_DAYS:
            tone = "warn"
        if fmp_age > _FMP_BAD_DAYS or build_age > _BUILD_BAD_DAYS:
            tone = "bad"
    detail = [
        f"FMP {fmt_reltime(row.base.fmp_last_pulled, now=now) if row.base.fmp_last_pulled else 'never'}",
        f"build {fmt_reltime(row.base.last_build_at, now=now) if row.base.last_build_at else 'never'}",
    ]
    transcript = row.base.last_transcript
    if transcript and transcript.period_end:
        qa = ""
        if transcript.has_qa_section is True:
            qa = " (Q&A)"
        elif transcript.has_qa_section is False:
            qa = " (no Q&A)"
        detail.append(f"transcript {transcript.period_end}{qa}")
    return f"<span class='stale-dot dot-{tone}' title='{escape(' · '.join(detail))}'>&#9679;</span>"


def _age_days(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ref = now if now.tzinfo else now.replace(tzinfo=UTC)
    return (ref - dt).total_seconds() / 86400.0


_COCKPIT_CSS = """
<style>
.cockpit-section h2 { display: flex; align-items: baseline; gap: 6px; }
.cockpit-table td { white-space: nowrap; }
.cockpit-table td.kpi-moves { white-space: normal; }
.cockpit-thin td, .cockpit-thin th { padding: 4px 10px; font-size: 12px; }
.cockpit-badge { display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; cursor: default; }
.cockpit-badge.b-ok { background: #14532d; color: var(--ok); }
.cockpit-badge.b-warn { background: #422006; color: var(--warn); }
.cockpit-badge.b-bad { background: #450a0a; color: var(--bad); }
.cockpit-badge.b-muted { background: #2a2c30; color: var(--muted); }
.kpi-chip { display: inline-block; margin: 1px 4px 1px 0; padding: 1px 7px; border-radius: 10px;
  font-size: 11px; font-family: var(--mono); background: var(--paper);
  border: 1px solid var(--border); color: var(--muted); cursor: default; }
.kpi-chip b { font-weight: 600; color: var(--fg); }
.kpi-chip.chip-bad { border-color: var(--bad); }
.kpi-chip.chip-bad b { color: var(--bad); }
.kpi-chip.chip-warn { border-color: var(--warn); }
.kpi-chip.chip-warn b { color: var(--warn); }
.kpi-chip.chip-ok b { color: var(--ok); }
.pill { display: inline-block; margin-right: 4px; padding: 1px 7px; border-radius: 10px;
  font-size: 11px; font-weight: 600; text-decoration: none; cursor: default; }
a.pill { cursor: pointer; }
.pill-bad { background: #450a0a; color: var(--bad); }
.pill-warn { background: #422006; color: var(--warn); }
.pill-accent { background: var(--accent-soft); color: var(--accent); }
.er-soon { color: var(--warn); font-weight: 600; }
.stale-dot { font-size: 10px; cursor: help; }
.dot-col { text-align: center; width: 28px; }
.dot-ok { color: var(--ok); }
.dot-warn { color: var(--warn); }
.dot-bad { color: var(--bad); }
td.pos, span.pos { color: var(--ok); }
td.neg, span.neg { color: var(--bad); }
</style>
""".strip()
