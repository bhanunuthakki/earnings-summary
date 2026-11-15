"""Typed view data for the earnings prep and post-earnings readout peeks.

The builders here own every read behind the two peeks. ``pipeline.peeks``
renders HTML from these models, and ``/api/view-data/earnings-prep`` and
``/api/view-data/earnings-readout`` serve the same models as JSON. One reader
means the page and an agent cannot disagree about what was on record.

Each section is best-effort, as in the peeks: a missing table or an
unreadable row makes that section ``None`` (or an empty list) and never fails
the view. A builder returns ``None`` only when the ticker is not a tracked,
non-archived name, or when an exact ``artifact_id`` does not resolve to a
current artifact of the right kind for that ticker.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from alerts import list_alerts
from pipeline.research_cockpit import next_earnings
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

VIEW_SCHEMA_VERSION = "1"

BRIEF_BODY_LIMIT = 20000
READOUT_BODY_LIMIT = 30000

RawValue = float | int | str | None


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactRefView(_View):
    """One persisted LLM artifact, with its receipt fields."""

    id: int
    purpose: str
    fiscal_period: str | None
    generated_at: datetime | None
    model: str | None
    prompt_version: str
    input_sha256: str
    source_doc_ids: list[int]
    content_md: str


class NewsEventView(_View):
    published: str
    headline: str
    url: str
    why: str


class WatchItemView(_View):
    kind: str
    body: str


class ValuationView(_View):
    """Latest consolidated DCF run. ``over_under_pct`` is a fraction (0.12 = 12%)."""

    live_price: float | None
    npv_per_share: float | None
    over_under_pct: float | None
    unreviewed: bool


class SurpriseSideView(_View):
    label: Literal["EPS", "Revenue"]
    estimate: RawValue
    actual: RawValue
    surprise_pct: float


class SurpriseView(_View):
    sides: list[SurpriseSideView]
    eps_beat_rate_pct: Decimal | None
    lookback_quarters: int | None


class KpiMoveView(_View):
    name: str
    unit: str
    latest_value: float
    prior_value: float
    prior_period: str
    delta_display: str
    tone: str
    tone_why: str


class CallToneView(_View):
    summary: str
    shifts: list[str]
    fired_on: str


class ReportedQuarterView(_View):
    document_id: int
    transcript_id: int
    fiscal_period_type: str
    period_end: str
    call_date: str | None


class ViewSource(_View):
    """One input the view read: a table, an artifact, or a source document."""

    kind: Literal["table", "artifact", "document"]
    ref: str


class EarningsPrepView(_View):
    schema_version: str = VIEW_SCHEMA_VERSION
    view: Literal["earnings_prep"] = "earnings_prep"
    ticker: str
    as_of: date
    list_type: str
    exact_artifact: bool
    next_er: str | None
    thesis_status: str | None
    brief: ArtifactRefView | None
    events: list[NewsEventView]
    watch_items: list[WatchItemView] | None
    valuation: ValuationView | None
    sources: list[ViewSource]


class EarningsReadoutView(_View):
    schema_version: str = VIEW_SCHEMA_VERSION
    view: Literal["earnings_readout"] = "earnings_readout"
    ticker: str
    as_of: date
    list_type: str
    exact_artifact: bool
    reported_on: str | None
    days_since_report: int | None
    thesis_status: str | None
    quarter: ReportedQuarterView | None
    readout: ArtifactRefView | None
    surprise: SurpriseView | None
    kpi_moves: list[KpiMoveView]
    call_tone: CallToneView | None
    watch_items: list[WatchItemView] | None
    valuation: ValuationView | None
    sources: list[ViewSource]


# ── shared readers ──────────────────────────────────────────────────────────


def _open(db_path: Path, conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection | None, bool]:
    if conn is not None:
        return conn, False
    try:
        return connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY), True
    except sqlite3.Error:
        return None, False


def _tracked_list_type(conn: sqlite3.Connection, t: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT list_type FROM tracked_companies WHERE ticker = ? AND archived_at IS NULL",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def _thesis_status(conn: sqlite3.Connection, t: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT breach_status FROM thesis_state WHERE ticker = ?", (t,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _artifact_ref(art: object) -> ArtifactRefView:
    from llm_artifact_store import Artifact

    a = cast(Artifact, art)
    return ArtifactRefView(
        id=a.id,
        purpose=a.purpose,
        fiscal_period=a.fiscal_period,
        generated_at=a.generated_at,
        model=a.model,
        prompt_version=a.prompt_version,
        input_sha256=a.input_sha256,
        source_doc_ids=list(a.source_doc_ids),
        content_md=a.content_md or "",
    )


def _exact_artifact(
    db_path: Path, artifact_id: int, t: str, purpose: str
) -> ArtifactRefView | None:
    """The exact artifact only when it is current, non-empty, and this ticker's."""
    try:
        from llm_artifact_store import read_artifact

        art = read_artifact(artifact_id, db_path=db_path)
    except Exception:
        return None
    if (
        art is None
        or art.ticker != t
        or art.scope != "ticker"
        or art.purpose != purpose
        or art.superseded_by_id is not None
        or not (art.content_md or "").strip()
    ):
        return None
    return _artifact_ref(art)


def _current_artifact(
    db_path: Path, t: str, purpose: str, fiscal_period: str
) -> ArtifactRefView | None:
    try:
        from llm_artifact_store import read_current

        art = read_current(ticker=t, purpose=purpose, fiscal_period=fiscal_period, db_path=db_path)
    except Exception:
        return None
    if art is None or not (art.content_md or "").strip():
        return None
    return _artifact_ref(art)


def news_events_since(
    db_path: Path,
    t: str,
    *,
    now: datetime,
    limit: int = 12,
    conn: sqlite3.Connection | None = None,
) -> list[NewsEventView]:
    """Material events noted for ``t`` since its last reported ER (fallback:
    120 days), newest first, one row per real-world event (latest per
    event_key — the write path already suppresses most duplicates; this
    read-side pass keeps the list one-per-event even across guard windows).
    Empty on a pre-0262 DB or any sqlite error — the section simply drops.
    """
    c, own = _open(db_path, conn)
    if c is None:
        return []
    try:
        since: str | None = None
        try:
            from expected_earnings import last_reported_by_ticker

            last = last_reported_by_ticker(c, now.date()).get(t)
            since = last.isoformat() if last else None
        except Exception:
            since = None
        if since is None:
            since = (now - timedelta(days=120)).date().isoformat()
        try:
            rows = c.execute(
                "SELECT published_at, headline, url, why_material, event_key "
                "FROM news_events WHERE ticker = ? AND published_at >= ? "
                "ORDER BY published_at DESC",
                (t, since),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        if own:
            c.close()
    out: list[NewsEventView] = []
    seen_keys: set[str] = set()
    for published_at, headline, url, why, event_key in rows:
        key = str(event_key or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        out.append(
            NewsEventView(
                published=str(published_at or "")[:10],
                headline=str(headline or ""),
                url=str(url or ""),
                why=str(why or ""),
            )
        )
        if len(out) >= limit:
            break
    return out


def _watch_items(db_path: Path, t: str) -> list[WatchItemView] | None:
    """Open watch items first, then questions (max 6). ``None`` = unreadable."""
    try:
        from user_state.notes import list_notes

        notes = list_notes(ticker=t, status="open", db_path=db_path)
    except Exception:
        return None
    kind_rank = {"watch": 0, "question": 1}
    kept = sorted((n for n in notes if n.kind in kind_rank), key=lambda n: kind_rank[n.kind])[:6]
    return [WatchItemView(kind=n.kind, body=n.body.strip()) for n in kept]


def _valuation(conn: sqlite3.Connection, t: str) -> ValuationView | None:
    try:
        row = conn.execute(
            "SELECT live_price, npv_per_share, over_under_pct, "
            "COALESCE(sanity_flag, '') FROM dcf_runs "
            "WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '') "
            "ORDER BY valuation_date DESC, rowid DESC LIMIT 1",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    live, fair, ou, sanity = row
    if live is None and fair is None and ou is None:
        return None
    return ValuationView(
        live_price=None if live is None else float(live),
        npv_per_share=None if fair is None else float(fair),
        over_under_pct=None if ou is None else float(ou),
        unreviewed=bool(sanity),
    )


# ── earnings prep ───────────────────────────────────────────────────────────


def _calendar_er(conn: sqlite3.Connection, t: str, now: datetime) -> str | None:
    """The upcoming ER date from the earnings calendar (``expected_earnings``)."""
    try:
        from expected_earnings import upcoming_by_ticker

        d = upcoming_by_ticker(conn, now.date()).get(t)
    except Exception:
        return None
    return d.isoformat() if d is not None else None


def build_earnings_prep_view(
    db_path: Path,
    repo_root: Path,
    ticker: str,
    *,
    now: datetime,
    artifact_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> EarningsPrepView | None:
    """The upcoming-ER prep data for one tracked name.

    The pre-earnings brief resolves exactly the way the stage-1c generator
    persisted it (keyed by the calendar ER date), so a brief for a past
    quarter can never masquerade as current. With ``artifact_id`` it resolves
    that one current brief, or the whole view is ``None``."""
    from earnings_brief import PURPOSE as BRIEF_PURPOSE

    t = (ticker or "").strip().upper()
    if not t:
        return None
    c, own = _open(db_path, conn)
    if c is None:
        return None
    try:
        list_type = _tracked_list_type(c, t)
        if list_type is None:
            return None
        calendar_er = _calendar_er(c, t, now)
        next_er = calendar_er
        if next_er is None:
            try:
                next_er = next_earnings(repo_root, t, now)
            except Exception:
                next_er = None
        thesis_status = _thesis_status(c, t)
        valuation = _valuation(c, t)
    finally:
        if own:
            c.close()

    if artifact_id is not None:
        brief = _exact_artifact(db_path, artifact_id, t, BRIEF_PURPOSE)
        if brief is None:
            return None
    else:
        brief = _current_artifact(db_path, t, BRIEF_PURPOSE, calendar_er) if calendar_er else None
        if brief is not None:
            brief = brief.model_copy(update={"fiscal_period": calendar_er})
    events = news_events_since(db_path, t, now=now, conn=None if own else c)
    watch = _watch_items(db_path, t)

    sources = [
        ViewSource(kind="table", ref="tracked_companies"),
        ViewSource(kind="table", ref="expected_earnings"),
        ViewSource(kind="table", ref="thesis_state"),
        ViewSource(kind="table", ref="news_events"),
        ViewSource(kind="table", ref="analyst_notes"),
        ViewSource(kind="table", ref="dcf_runs"),
    ]
    if brief is not None:
        sources.append(ViewSource(kind="artifact", ref=f"llm_artifacts:{brief.id}"))
    return EarningsPrepView(
        ticker=t,
        as_of=now.date(),
        list_type=list_type,
        exact_artifact=artifact_id is not None,
        next_er=next_er,
        thesis_status=thesis_status,
        brief=brief,
        events=events,
        watch_items=watch,
        valuation=valuation,
        sources=sources,
    )


# ── post-earnings readout ───────────────────────────────────────────────────


def _reported_on(conn: sqlite3.Connection, t: str, now: datetime) -> str | None:
    """Prefer an actual report event (``earnings_surprises.release_date``),
    falling back to the earnings calendar's kept-as-history past rows."""
    try:
        row = conn.execute(
            "SELECT MAX(release_date) FROM earnings_surprises WHERE ticker = ?", (t,)
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])[:10]
    except sqlite3.Error:
        pass
    try:
        from expected_earnings import last_reported_by_ticker

        d = last_reported_by_ticker(conn, now.date()).get(t)
    except Exception:
        return None
    return d.isoformat() if d is not None else None


def _surprise(conn: sqlite3.Connection, t: str) -> SurpriseView | None:
    """Latest quarter's beat/miss vs street plus the 8-quarter base rate."""
    try:
        row = conn.execute(
            "SELECT eps_estimate, eps_actual, eps_surprise_pct, "
            "revenue_estimate, revenue_actual, revenue_surprise_pct "
            "FROM earnings_surprises WHERE ticker = ? "
            "ORDER BY release_date DESC LIMIT 1",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None

    def _side(label: Literal["EPS", "Revenue"], est: RawValue, act: RawValue, pct: RawValue):
        if act is None or pct is None:
            return None
        try:
            p = float(str(pct))
        except ValueError:
            return None
        return SurpriseSideView(label=label, estimate=est, actual=act, surprise_pct=p)

    sides = [
        s
        for s in (_side("EPS", row[0], row[1], row[2]), _side("Revenue", row[3], row[4], row[5]))
        if s is not None
    ]
    if not sides:
        return None
    rate: Decimal | None = None
    quarters: int | None = None
    try:
        from compute.earnings_surprise import surprise_scorecard_for

        previous_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            sc = surprise_scorecard_for(conn, t, lookback_quarters=8)
        finally:
            conn.row_factory = previous_row_factory
        if sc.total_quarters and sc.eps.beat_rate_pct is not None:
            rate = sc.eps.beat_rate_pct
            quarters = sc.total_quarters
    except Exception:
        rate, quarters = None, None
    return SurpriseView(sides=sides, eps_beat_rate_pct=rate, lookback_quarters=quarters)


def _kpi_moves(conn: sqlite3.Connection, t: str, now: datetime) -> list[KpiMoveView]:
    """Tier-1 KPI moves, latest vs prior — the same deltas the cockpit chips
    render (one reader, so the peek cannot disagree with the cockpit)."""
    try:
        from pipeline.research_cockpit import tier1_kpi_deltas

        deltas = tier1_kpi_deltas(conn, {t}, as_of=now.date()).get(t, [])
    except Exception:
        return []
    deltas = sorted(deltas, key=lambda d: d.magnitude, reverse=True)[:8]
    return [
        KpiMoveView(
            name=d.name,
            unit=d.unit,
            latest_value=d.latest_value,
            prior_value=d.prior_value,
            prior_period=d.prior_period,
            delta_display=d.delta_display,
            tone=d.tone,
            tone_why=d.tone_why,
        )
        for d in deltas
    ]


def _call_tone(db_path: Path, t: str) -> CallToneView | None:
    """The latest ``earnings_tone`` alert's summary and shifts — the call-tone
    read the trigger already produced (never recomputed here)."""
    try:
        alerts = list_alerts(ticker=t, limit=50, db_path=db_path)
    except sqlite3.Error:
        return None
    tone = next((a for a in alerts if a.trigger_kind == "earnings_tone"), None)
    if tone is None or not tone.evidence_json:
        return None
    try:
        parsed = json.loads(tone.evidence_json)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    ev = cast("dict[str, object]", parsed)
    summary = str(ev.get("summary") or "").strip()
    if not summary:
        return None
    raw_shifts = ev.get("shifts")
    shifts: list[str] = []
    if isinstance(raw_shifts, list):
        shifts = [str(s) for s in cast("list[object]", raw_shifts)[:4] if str(s).strip()]
    return CallToneView(summary=summary, shifts=shifts, fired_on=str(tone.fired_at)[:10])


def build_earnings_readout_view(
    db_path: Path,
    repo_root: Path,
    ticker: str,
    *,
    now: datetime,
    artifact_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> EarningsReadoutView | None:
    """The just-reported quarter's data for one tracked name.

    With ``artifact_id`` it resolves that one current readout artifact and
    reads nothing else (the quarter-stable view), or the whole view is
    ``None``. Otherwise the persisted readout resolves for the latest selected
    reported quarter, next to the deterministic sections."""
    from earnings_readout import PURPOSE as READOUT_PURPOSE
    from earnings_readout import latest_reported_quarter

    del repo_root  # the readout reads only the database
    t = (ticker or "").strip().upper()
    if not t:
        return None
    c, own = _open(db_path, conn)
    if c is None:
        return None
    try:
        list_type = _tracked_list_type(c, t)
        if list_type is None:
            return None
        if artifact_id is not None:
            exact = _exact_artifact(db_path, artifact_id, t, READOUT_PURPOSE)
            if exact is None:
                return None
            return EarningsReadoutView(
                ticker=t,
                as_of=now.date(),
                list_type=list_type,
                exact_artifact=True,
                reported_on=None,
                days_since_report=None,
                thesis_status=None,
                quarter=None,
                readout=exact,
                surprise=None,
                kpi_moves=[],
                call_tone=None,
                watch_items=None,
                valuation=None,
                sources=[ViewSource(kind="artifact", ref=f"llm_artifacts:{exact.id}")],
            )
        quarter = latest_reported_quarter(db_path, t, today=now.date(), conn=c)
        reported_on = _reported_on(c, t, now)
        thesis_status = _thesis_status(c, t)
        surprise = _surprise(c, t)
        kpis = _kpi_moves(c, t, now)
        valuation = _valuation(c, t)
    finally:
        if own:
            c.close()

    days: int | None = None
    if reported_on:
        try:
            days = (now.date() - date.fromisoformat(reported_on)).days
        except ValueError:
            days = None
    call_tone = _call_tone(db_path, t)
    watch = _watch_items(db_path, t)
    readout = (
        _current_artifact(db_path, t, READOUT_PURPOSE, quarter.period_end)
        if quarter is not None
        else None
    )
    quarter_view = (
        None
        if quarter is None
        else ReportedQuarterView(
            document_id=quarter.document_id,
            transcript_id=quarter.transcript_id,
            fiscal_period_type=quarter.fiscal_period_type,
            period_end=quarter.period_end,
            call_date=quarter.call_date,
        )
    )
    sources = [
        ViewSource(kind="table", ref="tracked_companies"),
        ViewSource(kind="table", ref="earnings_surprises"),
        ViewSource(kind="table", ref="thesis_state"),
        ViewSource(kind="table", ref="kpi_definitions"),
        ViewSource(kind="table", ref="alerts"),
        ViewSource(kind="table", ref="analyst_notes"),
        ViewSource(kind="table", ref="dcf_runs"),
    ]
    if quarter_view is not None:
        sources.append(ViewSource(kind="document", ref=f"documents:{quarter_view.document_id}"))
    if readout is not None:
        sources.append(ViewSource(kind="artifact", ref=f"llm_artifacts:{readout.id}"))
    return EarningsReadoutView(
        ticker=t,
        as_of=now.date(),
        list_type=list_type,
        exact_artifact=False,
        reported_on=reported_on,
        days_since_report=days,
        thesis_status=thesis_status,
        quarter=quarter_view,
        readout=readout,
        surprise=surprise,
        kpi_moves=kpis,
        call_tone=call_tone,
        watch_items=watch,
        valuation=valuation,
        sources=sources,
    )
