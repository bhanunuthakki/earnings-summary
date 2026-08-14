"""Per-ticker "analyses that ran" log for the command-center drill-down.

Summarizes, for one ticker, the state of every analysis the pipeline runs:
thesis evaluation, time-series signals, the trigger/alert framework, queued
actions, Say-Do commitments, DCF valuation, LLM calls (with recent cost), and
brief renders.

Read-only. Each table is probed for existence first (pre-migration DBs render
fine) and each block is wrapped so column drift degrades a single row rather
than the whole page — same defensive posture as pipeline.analytical_dashboard.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from compute.thesis_evaluation_episodes import episode_history_source


@dataclass(slots=True)
class AnalysisRow:
    analysis: str
    last_run: str | None  # ISO timestamp of most recent run, None if never
    summary: str  # human-readable one-liner

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class AlertRow:
    trigger_kind: str
    fired_at: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class LlmCallRow:
    purpose: str | None
    model: str | None
    cost_usd: float | None
    called_at: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class AnalysisLog:
    rows: list[AnalysisRow] = field(default_factory=lambda: list[AnalysisRow]())
    recent_alerts: list[AlertRow] = field(default_factory=lambda: list[AlertRow]())
    recent_llm_calls: list[LlmCallRow] = field(default_factory=lambda: list[LlmCallRow]())
    llm_cost_30d_usd: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "recent_alerts": [a.to_dict() for a in self.recent_alerts],
            "recent_llm_calls": [c.to_dict() for c in self.recent_llm_calls],
            "llm_cost_30d_usd": self.llm_cost_30d_usd,
        }


def build_analysis_log(conn: sqlite3.Connection, ticker: str) -> AnalysisLog:
    t = ticker.upper()
    log = AnalysisLog()
    for builder in (
        _thesis_eval_row,
        _signals_row,
        _alerts_row,
        _queued_actions_row,
        _saydo_row,
        _dcf_row,
        _llm_calls_row,
        _brief_render_row,
    ):
        try:
            builder(conn, t, log)
        except sqlite3.Error:
            # Column/schema drift on one table degrades that row, not the page.
            continue
    return log


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _thesis_eval_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "thesis_evaluations"):
        return
    source = episode_history_source(conn)
    latest = conn.execute(
        f"SELECT overall_status,{source.latest_checked_column} AS evaluated_at "
        f"FROM {source.relation} WHERE UPPER(ticker)=? "
        f"ORDER BY {source.latest_checked_column} DESC LIMIT 1",  # nosec B608 -- trusted closed relation
        (t,),
    ).fetchone()
    n = conn.execute(
        f"SELECT COUNT(*) FROM {source.relation} WHERE UPPER(ticker)=?",  # nosec B608 -- trusted closed relation
        (t,),
    ).fetchone()[0]
    if latest is None:
        log.rows.append(AnalysisRow("Thesis evaluation", None, "never run"))
        return
    log.rows.append(
        AnalysisRow(
            "Thesis evaluation",
            str(latest["evaluated_at"]),
            f"{latest['overall_status']} · {n} evaluation(s)",
        )
    )


def _signals_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "timeseries_signals"):
        return
    counts = {
        str(r["severity"]): int(r["n"])
        for r in conn.execute(
            "SELECT severity, COUNT(*) n FROM timeseries_signals "
            "WHERE UPPER(ticker)=? GROUP BY severity",
            (t,),
        ).fetchall()
    }
    last = conn.execute(
        "SELECT MAX(computed_at) FROM timeseries_signals WHERE UPPER(ticker)=?", (t,)
    ).fetchone()[0]
    if not counts:
        log.rows.append(AnalysisRow("Time-series signals", None, "never run"))
        return
    summary = " · ".join(
        f"{counts.get(sev, 0)} {sev}" for sev in ("red", "yellow", "green") if counts.get(sev)
    )
    log.rows.append(
        AnalysisRow("Time-series signals", str(last) if last else None, summary or "0 active")
    )


def _alerts_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "alerts"):
        return
    by_status = {
        str(r["status"]): int(r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM alerts WHERE UPPER(ticker)=? GROUP BY status",
            (t,),
        ).fetchall()
    }
    recent = conn.execute(
        "SELECT trigger_kind, fired_at, status FROM alerts "
        "WHERE UPPER(ticker)=? ORDER BY fired_at DESC LIMIT 5",
        (t,),
    ).fetchall()
    log.recent_alerts = [
        AlertRow(str(r["trigger_kind"]), str(r["fired_at"]), str(r["status"])) for r in recent
    ]
    last = recent[0]["fired_at"] if recent else None
    if not by_status:
        log.rows.append(AnalysisRow("Trigger alerts", None, "none fired"))
        return
    summary = " · ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
    log.rows.append(AnalysisRow("Trigger alerts", str(last) if last else None, summary))


def _queued_actions_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not (_has(conn, "queued_actions") and _has(conn, "alerts")):
        return
    # queued_actions has no ticker — join to alerts via alert_id.
    pending = conn.execute(
        "SELECT COUNT(*) FROM queued_actions q JOIN alerts a ON a.id = q.alert_id "
        "WHERE UPPER(a.ticker)=? AND q.status='pending'",
        (t,),
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM queued_actions q JOIN alerts a ON a.id = q.alert_id "
        "WHERE UPPER(a.ticker)=?",
        (t,),
    ).fetchone()[0]
    if not total:
        return
    log.rows.append(AnalysisRow("Queued actions", None, f"{pending} pending · {total} total"))


def _saydo_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "management_commitments"):
        return
    row = conn.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) evaluated, "
        "MAX(evaluated_at) last_eval "
        "FROM management_commitments WHERE UPPER(ticker)=?",
        (t,),
    ).fetchone()
    total = int(row["total"] or 0)
    if not total:
        log.rows.append(AnalysisRow("Say-Do commitments", None, "none extracted"))
        return
    evaluated = int(row["evaluated"] or 0)
    log.rows.append(
        AnalysisRow(
            "Say-Do commitments",
            str(row["last_eval"]) if row["last_eval"] else None,
            f"{total} tracked · {evaluated} evaluated",
        )
    )


def _dcf_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "dcf_runs"):
        return
    row = conn.execute(
        "SELECT valuation_date, over_under_pct FROM dcf_runs "
        "WHERE UPPER(ticker)=? AND (segment_name IS NULL OR segment_name='') "
        "ORDER BY valuation_date DESC LIMIT 1",
        (t,),
    ).fetchone()
    if row is None:
        log.rows.append(AnalysisRow("DCF valuation", None, "never run"))
        return
    ou = row["over_under_pct"]
    summary = f"over/under {float(ou) * 100:+.0f}%" if ou is not None else "run (no over/under)"
    log.rows.append(AnalysisRow("DCF valuation", str(row["valuation_date"]), summary))


def _llm_calls_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "llm_calls"):
        return
    recent = conn.execute(
        "SELECT purpose, model, cost_estimate_usd, called_at FROM llm_calls "
        "WHERE UPPER(ticker)=? ORDER BY called_at DESC LIMIT 8",
        (t,),
    ).fetchall()
    log.recent_llm_calls = [
        LlmCallRow(
            r["purpose"],
            r["model"],
            float(r["cost_estimate_usd"]) if r["cost_estimate_usd"] is not None else None,
            str(r["called_at"]) if r["called_at"] else None,
        )
        for r in recent
    ]
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_estimate_usd), 0.0) FROM llm_calls "
        "WHERE UPPER(ticker)=? AND called_at >= ?",
        (t, cutoff),
    ).fetchone()[0]
    log.llm_cost_30d_usd = float(cost or 0.0)
    if not recent:
        log.rows.append(AnalysisRow("LLM calls", None, "none recorded"))
        return
    log.rows.append(
        AnalysisRow(
            "LLM calls",
            str(recent[0]["called_at"]) if recent[0]["called_at"] else None,
            f"${log.llm_cost_30d_usd:,.2f} (30d) · {len(recent)} recent",
        )
    )


def _brief_render_row(conn: sqlite3.Connection, t: str, log: AnalysisLog) -> None:
    if not _has(conn, "brief_provenance_log"):
        return
    row = conn.execute(
        "SELECT generated_at, trigger FROM brief_provenance_log "
        "WHERE UPPER(ticker)=? ORDER BY generated_at DESC LIMIT 1",
        (t,),
    ).fetchone()
    if row is None:
        log.rows.append(AnalysisRow("Brief render", None, "never rendered"))
        return
    log.rows.append(
        AnalysisRow("Brief render", str(row["generated_at"]), f"trigger: {row['trigger']}")
    )
