"""§13 Executive Compensation — per-ticker analytical section.

Composes:
  1. Latest year's NEO comp packages (Phase 5b)
  2. Recent insider transactions ranked by conviction signal (Phase 5a)
  3. Beneficial-ownership snapshots (Phase 5b)
  4. LLM-driven alignment commentary: do management's comp-design metrics
     match the thesis tier-1 KPIs? (gated by enable_llm; cached via
     llm_artifacts.purpose='exec_comp_alignment')
  5. Anomaly flags (spring-loading, peer-group drift, hedging policy)

Read-only against the underlying tables. Renderers consume the typed
ExecCompSectionModel (defined in src/report/models.py).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from llm_client import is_hard_stop
from report.models import (
    ExecCompRowModel,
    ExecCompSectionModel,
    InsiderSignalRowModel,
    SectionStatus,
)
from report.sections._common import missing

log = logging.getLogger(__name__)


def build(
    ticker: str,
    repo_root: Path,
    *,
    enable_llm: bool = False,
    insider_window_days: int = 365,
) -> ExecCompSectionModel:
    """Build the §13 Executive Compensation section for one ticker."""
    ticker = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"

    packages = _load_packages(ticker, db_path)
    insider_signals = _load_insider_signals(ticker, db_path, insider_window_days)
    holdings_kpis = _load_thesis_kpis(ticker, repo_root)

    if not packages and not insider_signals:
        return ExecCompSectionModel(
            status=SectionStatus.MISSING_DATA,
            ticker=ticker,
            missing=missing(
                stage="INGEST(insider+proxy)",
                fix_command=(
                    f"python execution/backfill_insider_transactions.py --tickers {ticker} && "
                    f"python execution/extract_exec_comp.py --ticker {ticker}"
                ),
                detail="No Form 4 transactions backfilled and no DEF 14A package extracted.",
            ),
        )

    fiscal_year_latest = max((p["fiscal_year"] for p in packages), default=None)

    rows: list[ExecCompRowModel] = []
    for p in packages:
        if fiscal_year_latest is not None and p["fiscal_year"] != fiscal_year_latest:
            continue
        granted = _f(p.get("total_comp_granted"))
        realized = _f(p.get("total_comp_realized"))
        realized_vs = (
            (realized / granted - 1.0) if (granted and realized and granted != 0) else None
        )
        metrics, has_kpi_match = _summarize_metrics(
            p.get("performance_metrics_json"), holdings_kpis
        )
        rows.append(
            ExecCompRowModel(
                executive_name=p["executive_name"],
                role=p["role"],
                is_ceo=bool(p["is_ceo"]),
                fiscal_year=int(p["fiscal_year"]),
                currency=p["currency"],
                base_salary=_f(p.get("base_salary")),
                cash_bonus_actual=_f(p.get("cash_bonus_actual")),
                cash_bonus_target=_f(p.get("cash_bonus_target")),
                equity_grant_value=_f(p.get("equity_grant_value")),
                total_comp_granted=granted,
                total_comp_realized=realized,
                realized_vs_granted_pct=realized_vs,
                ceo_pay_ratio=_f(p.get("ceo_pay_ratio")),
                performance_metrics_summary=metrics,
                metrics_have_thesis_kpi=has_kpi_match,
            )
        )
    rows.sort(key=lambda r: (not r.is_ceo, -(r.total_comp_granted or 0)))

    flags = _detect_anomalies(rows, insider_signals, holdings_kpis)

    alignment_md: str | None = None
    if enable_llm and (rows or insider_signals):
        try:
            alignment_md = _generate_alignment_narrative(
                ticker=ticker,
                rows=rows,
                insider_signals=insider_signals,
                thesis_kpis=holdings_kpis,
                repo_root=repo_root,
            )
        except Exception as exc:  # noqa: BLE001 — defensive across LLM/cache
            # Hard stops (monthly budget cap, CLI not installed) must propagate
            # — degrading would silently mask an over-budget / unconfigured run.
            # Everything else is transient: drop the alignment narrative and let
            # the rest of §13 render from the deterministic comp/insider rows.
            if is_hard_stop(exc):
                raise
            log.warning(
                {
                    "event": "exec_comp_alignment_failed",
                    "ticker": ticker,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return ExecCompSectionModel(
        status=SectionStatus.OK if rows else SectionStatus.PARTIAL,
        ticker=ticker,
        fiscal_year_latest=fiscal_year_latest,
        packages=rows,
        insider_signals=insider_signals,
        alignment_narrative_md=alignment_md,
        anomaly_flags=flags,
    )


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def _load_packages(ticker: str, db_path: Path) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        # Verify table exists
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exec_comp_packages'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute(
            "SELECT * FROM exec_comp_packages WHERE ticker = ? ORDER BY fiscal_year DESC, is_ceo DESC",
            (ticker,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_insider_signals(
    ticker: str, db_path: Path, window_days: int
) -> list[InsiderSignalRowModel]:
    if not db_path.exists():
        return []
    # Late import so this section module can be imported without sys.path being set
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    try:
        from insider_transactions import conviction_signals  # type: ignore[import-not-found]
    except ImportError:
        return []

    signals = conviction_signals(
        ticker=ticker, window_days=window_days, db_path=db_path
    )
    return [
        InsiderSignalRowModel(
            insider_name=s.insider_name,
            role=s.insider_role,
            transaction_date=s.transaction_date.date().isoformat(),
            transaction_type=s.transaction_type,
            shares=s.shares,
            transaction_value=s.transaction_value,
            signal_strength=s.signal_strength,
            rationale=s.rationale,
        )
        for s in signals[:30]  # top 30 by signal
    ]


def _load_thesis_kpis(ticker: str, repo_root: Path) -> list[str]:
    """Tier-1 KPI names from holdings JSON — used to test alignment."""
    holdings = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not holdings.exists():
        return []
    try:
        data = cast("dict[str, object]", json.loads(holdings.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("tier_1_kpis")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def _summarize_metrics(
    metrics_raw: object, thesis_kpis: list[str]
) -> tuple[str | None, bool]:
    """Render metrics summary + flag whether ANY thesis tier-1 KPI is among
    the comp-design metrics (the alignment check)."""
    if not metrics_raw:
        return (None, False)
    try:
        decoded = json.loads(str(metrics_raw))
    except (json.JSONDecodeError, TypeError):
        return (None, False)
    if not isinstance(decoded, list):
        return (None, False)

    parts: list[str] = []
    has_match = False
    kpi_lower = {k.lower() for k in thesis_kpis}
    for entry in decoded:
        if not isinstance(entry, dict):
            continue
        metric = str(entry.get("metric") or "").strip()
        weight = entry.get("weight")
        if not metric:
            continue
        weight_str = f"{int(float(weight) * 100)}%" if isinstance(weight, (int, float)) else "?%"
        parts.append(f"{metric} ({weight_str})")
        m_lower = metric.lower()
        if any(k in m_lower or m_lower in k for k in kpi_lower):
            has_match = True
    if not parts:
        return (None, False)
    return (" · ".join(parts), has_match)


def _detect_anomalies(
    rows: list[ExecCompRowModel],
    insider_signals: list[InsiderSignalRowModel],
    thesis_kpis: list[str],
) -> list[str]:
    flags: list[str] = []

    # 1. Missing alignment: thesis tier-1 KPI not in any comp design
    has_match = any(r.metrics_have_thesis_kpi for r in rows)
    if rows and thesis_kpis and not has_match:
        flags.append(
            f"COMP MISALIGNMENT — no comp-design metric matches the {len(thesis_kpis)} "
            "tier-1 thesis KPI(s) tracked in micro_thesis. Management is not paid for "
            "what the analyst tracks."
        )

    # 2. CEO realized comp materially exceeds granted (spring-loading or
    # over-performance on equity)
    ceo = next((r for r in rows if r.is_ceo), None)
    if ceo and ceo.realized_vs_granted_pct is not None and ceo.realized_vs_granted_pct > 0.5:
        flags.append(
            f"CEO REALIZED PAY +{ceo.realized_vs_granted_pct * 100:.0f}% vs granted — "
            "investigate whether driven by share-price appreciation (good) vs "
            "spring-loaded grants (concerning)."
        )

    # 3. CEO pay ratio > 500x
    if ceo and ceo.ceo_pay_ratio is not None and ceo.ceo_pay_ratio > 500:
        flags.append(
            f"CEO pay ratio {ceo.ceo_pay_ratio:.0f}x — well above S&P 500 average (~300x)."
        )

    # 4. Cluster of CFO/CEO open-market sells in last 90d
    recent_executive_sells = [
        s
        for s in insider_signals
        if s.transaction_type == "open_market_sell"
        and s.role
        and any(t in (s.role or "").lower() for t in ("chief executive", "chief financial"))
    ]
    if len(recent_executive_sells) >= 2:
        flags.append(
            f"{len(recent_executive_sells)} CEO/CFO discretionary sells in last year — "
            "consult realized-pay context; not necessarily a signal but worth flagging."
        )

    # 5. Open-market buys by CEO/CFO (positive signal)
    exec_buys = [
        s
        for s in insider_signals
        if s.transaction_type == "open_market_buy"
        and s.role
        and any(t in (s.role or "").lower() for t in ("chief executive", "chief financial"))
    ]
    if exec_buys:
        total = sum(s.transaction_value or 0 for s in exec_buys)
        flags.append(
            f"POSITIVE SIGNAL — CEO/CFO open-market buys totaling ${total / 1e6:.1f}M "
            "in window. High-conviction insider event."
        )

    return flags


# ---------------------------------------------------------------------------
# LLM alignment narrative — cached via llm_artifacts
# ---------------------------------------------------------------------------


def _generate_alignment_narrative(
    *,
    ticker: str,
    rows: list[ExecCompRowModel],
    insider_signals: list[InsiderSignalRowModel],
    thesis_kpis: list[str],
    repo_root: Path,
) -> str | None:
    """Single Opus call that produces a concise alignment commentary.
    Cached in llm_artifacts under purpose='exec_comp_alignment'."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    try:
        from llm_artifact_store import UpsertRequest, read_current, upsert  # type: ignore[import-not-found]
        from llm_client import call_llm  # type: ignore[import-not-found]
    except ImportError:
        return None

    db_path = repo_root / "data" / "portfolio.db"

    # Build the cache key inputs deterministically
    cache_inputs: list[bytes | str] = [
        ticker,
        json.dumps(
            [
                {
                    "name": r.executive_name,
                    "role": r.role,
                    "granted": r.total_comp_granted,
                    "realized": r.total_comp_realized,
                    "metrics": r.performance_metrics_summary,
                    "kpi_match": r.metrics_have_thesis_kpi,
                }
                for r in rows
            ],
            sort_keys=True,
        ),
        json.dumps(thesis_kpis, sort_keys=True),
        json.dumps(
            [
                {
                    "name": s.insider_name,
                    "type": s.transaction_type,
                    "value": s.transaction_value,
                    "role": s.role,
                    "date": s.transaction_date,
                }
                for s in insider_signals[:10]
            ],
            sort_keys=True,
        ),
    ]
    # Cache-hit check
    existing = read_current(
        ticker=ticker,
        purpose="exec_comp_alignment",
        db_path=db_path,
    )
    if existing is not None and not existing.dirty:
        # Compare input_sha256
        from llm_artifact_store import compute_input_sha256  # type: ignore[import-not-found]

        new_sha = compute_input_sha256(prompt_version="v1", cache_inputs=cache_inputs)
        if new_sha == existing.input_sha256 and existing.content_md:
            return existing.content_md

    # Build the prompt
    prompt = _alignment_prompt(ticker, rows, insider_signals, thesis_kpis)
    narrative = call_llm(
        prompt,
        purpose="exec_comp_alignment",
        ticker=ticker,
        model="claude-opus-4-7",  # alignment judgment benefits from Opus
    )
    upsert(
        UpsertRequest(
            ticker=ticker,
            purpose="exec_comp_alignment",
            content_md=narrative,
            cache_inputs=cache_inputs,
            model="claude-opus-4-7",
        ),
        db_path=db_path,
    )
    return narrative


def _alignment_prompt(
    ticker: str,
    rows: list[ExecCompRowModel],
    insider_signals: list[InsiderSignalRowModel],
    thesis_kpis: list[str],
) -> str:
    # Late import to match the lazy-load pattern of _generate_alignment_narrative —
    # this module is loaded with src/ on sys.path but the explicit late binding
    # keeps any future src-path reordering from breaking import.
    from llm.style import NUMBER_FORMATTING_BLOCK  # type: ignore[import-not-found]

    pkg_lines: list[str] = []
    for r in rows:
        pkg_lines.append(
            f"- {r.executive_name} ({r.role or '?'}): "
            f"granted ${(r.total_comp_granted or 0) / 1e6:.1f}M / realized ${(r.total_comp_realized or 0) / 1e6:.1f}M. "
            f"Metrics: {r.performance_metrics_summary or '(none disclosed)'}. "
            f"Thesis-KPI match: {'YES' if r.metrics_have_thesis_kpi else 'NO'}."
        )
    insider_lines: list[str] = []
    for s in insider_signals[:10]:
        insider_lines.append(
            f"- {s.transaction_date} · {s.insider_name} ({s.role or '?'}): "
            f"{s.transaction_type} {s.shares:,.0f} sh @ ${s.transaction_value or 0:,.0f}, "
            f"signal={s.signal_strength:.2f}, {s.rationale}"
        )
    thesis_str = "\n  - " + "\n  - ".join(thesis_kpis) if thesis_kpis else " (no thesis KPIs configured)"

    return f"""You are writing a concise alignment analysis for {ticker} as part of a
buy-side equity research workspace. Your job is to evaluate whether management's
compensation design rewards what the analyst thinks matters.

Thesis tier-1 KPIs (what the analyst is tracking):{thesis_str}

Latest fiscal year NEO compensation:
{chr(10).join(pkg_lines) if pkg_lines else "(no comp data available)"}

Recent insider transactions (top {len(insider_signals[:10])} by conviction signal):
{chr(10).join(insider_lines) if insider_lines else "(no recent insider activity)"}

Write a 200-350 word analyst memo in markdown. Cover:

1. **Alignment verdict** (one sentence): does the comp design reward the
   thesis or work against it? Be specific — name the metric mismatch when
   there is one.

2. **Realized vs granted commentary**: when materially divergent, explain
   the likely drivers (share-price appreciation, performance achievement,
   spring-loaded grants).

3. **Insider conviction signal**: synthesize the recent activity. CEO/CFO
   open-market buys are high-signal; clustered events compound; 10b5-1 sells
   are noise. Be concrete about what the pattern means for the next 4Q.

4. **What to watch**: 1-2 specific things — a comp-design vote, a peer-group
   shift, a CEO realized-pay milestone — that would materially update this
   reading.

NO restating the data verbatim. NO bullet-padded vague observations. Take a
position; cite the specific number that drives it. Use the prose voice of a
senior buy-side analyst writing to themselves: opinion-bearing, terse,
non-consensus where the data supports it.

{NUMBER_FORMATTING_BLOCK}

Return ONLY the memo markdown — no preamble, no JSON wrapper."""


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
