"""Shared helpers for section builders.

Time-series helpers (quarterly labels, TTM-based CAGR) are deliberately
written for the 16-quarter underlying / 12-quarter display contract: the
caller pulls 16, the helpers consume up to 16, and the display layer slices
to the last 12.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from report.models import BudgetSkip, GrowthMetrics, MissingReason

DISPLAY_QUARTERS = 12
UNDERLYING_QUARTERS = 16  # 12 display + 4 prior for TTM-1Y CAGR baseline
CAGR_3Y_BASELINE = 16  # need TTM ending at Q-12 → window Q-12..Q-15

QUARTERLY_PERIOD_TYPES = ("Q1", "Q2", "Q3", "Q4", "quarterly")
ANNUAL_PERIOD_TYPES = ("FY", "annual")


def calendar_quarter_key(period_end: str | object) -> tuple[int, int]:
    """Map a period_end to (calendar_year, calendar_quarter ∈ 1..4)."""
    s = str(period_end)[:10]
    year = int(s[:4])
    month = int(s[5:7])
    return (year, (month - 1) // 3 + 1)


def quarter_label(period_end: str | date) -> str:
    """Convert a period_end to a 'YYYY Qx' label using calendar quarters."""
    if isinstance(period_end, str):
        s = period_end[:10]
        y, m, _ = s.split("-")
        year = int(y)
        month = int(m)
    else:
        year = period_end.year
        month = period_end.month
    q = (month - 1) // 3 + 1
    return f"{year} Q{q}"


def compute_growth(values: list[float | None]) -> GrowthMetrics:
    """Compute QoQ, YoY, 1Y-TTM CAGR, 3Y-TTM CAGR from a chronological series.

    `values` is ordered oldest → newest. Returns Nones for any metric whose
    inputs are missing or non-positive (CAGR requires positive endpoints).
    """
    if not values:
        return GrowthMetrics()
    n = len(values)
    last = values[-1]
    prev = values[-2] if n >= 2 else None
    yoy_base = values[-5] if n >= 5 else None

    qoq = _safe_growth(last, prev)
    yoy = _safe_growth(last, yoy_base)
    cagr_1y = _ttm_cagr(values, end_offset=0, base_offset=4, years=1)
    cagr_3y = _ttm_cagr(values, end_offset=0, base_offset=12, years=3)
    return GrowthMetrics(qoq=qoq, yoy=yoy, cagr_1y_ttm=cagr_1y, cagr_3y_ttm=cagr_3y)


def _safe_growth(curr: float | None, base: float | None) -> float | None:
    if curr is None or base is None or base == 0:
        return None
    return curr / base - 1.0


def _ttm_cagr(values: list[float | None], end_offset: int, base_offset: int, years: int) -> float | None:
    """TTM CAGR. end_offset/base_offset are quarters back from the most recent.

    end_offset=0 → TTM(Q0..Q-3); base_offset=4 → TTM(Q-4..Q-7).
    """
    end_window = _ttm(values, end_offset)
    base_window = _ttm(values, base_offset)
    if end_window is None or base_window is None or base_window <= 0 or end_window <= 0:
        return None
    return (end_window / base_window) ** (1 / years) - 1.0


def _ttm(values: list[float | None], offset: int) -> float | None:
    """Sum of 4 consecutive quarters ending at len-1-offset (newest=offset 0)."""
    end_idx = len(values) - 1 - offset
    if end_idx < 3:
        return None
    window = values[end_idx - 3 : end_idx + 1]
    if any(v is None for v in window):
        return None
    return sum(v for v in window if v is not None)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def open_repo_db(repo_root: Path) -> sqlite3.Connection | None:
    """Open the portfolio DB if present; return None to let the caller stub the section."""
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def open_portfolio_tracker_db(repo_root: Path) -> sqlite3.Connection | None:
    """Open the companion portfolio-tracker project's SQLite read-only.

    Both projects sit side-by-side under
    ~/.gemini/antigravity/scratch/. This finds portfolio-tracker's DB
    at `../portfolio-tracker/portfolio.db` relative to earnings-summary's
    repo root. Returns None when the companion isn't installed so the
    Portfolio Position section can stub itself cleanly.

    Opened in URI mode with `?mode=ro` so we never accidentally write
    to the other project's database.
    """
    db_path = repo_root.parent / "portfolio-tracker" / "portfolio.db"
    if not db_path.exists():
        return None
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    )
    return cursor.fetchone() is not None


def missing(stage: str, fix_command: str, detail: str | None = None) -> MissingReason:
    return MissingReason(stage=stage, fix_command=fix_command, detail=detail)


def budget_gate(
    purpose: str,
    section_label: str,
    repo_root: Path,
    *,
    bypass: bool = False,
) -> BudgetSkip | None:
    """Pre-flight LLM-budget gate for a section.

    Returns a ``BudgetSkip`` (to attach to the section AND roll up into
    ``ReportSpec.forgone_due_to_budget``) when ``purpose`` is configured
    ``on_exceed='skip'`` and at/over its monthly cap — i.e. the section should
    FORGO its LLM call (no spend) and render a "forgone due to budget" banner.
    Returns None to proceed (under cap, a non-skip mode such as 'block'/'warn',
    or ``bypass=True``). Best-effort: any failure reading the budget returns
    None so the gate can never break a build.
    """
    try:
        from llm_budget import should_skip_for_budget
    except ImportError:
        return None
    check = should_skip_for_budget(
        purpose, db_path=repo_root / "data" / "portfolio.db", bypass=bypass
    )
    if check is None:
        return None
    return BudgetSkip(
        section=section_label,
        purpose=purpose,
        cap_usd=float(check.cap),
        spend_usd=float(check.current_spend),
        headroom_pct=check.headroom_pct,
    )


def budget_skip_missing(ticker: str, purpose: str, skip: BudgetSkip) -> MissingReason:
    """Standard loud banner for an LLM section forgone to stay under budget —
    names the cap + month-to-date spend and points at the override path."""
    return MissingReason(
        stage=f"BUDGET({purpose})",
        fix_command=(
            f"python execution/manage_llm_budget.py --set {purpose} --cap <higher>"
            "  # or raise / override in the dashboard, then rebuild"
        ),
        detail=(
            f"Forgone to stay under budget — {purpose} is at/over its "
            f"${skip.cap_usd:g} monthly cap (${skip.spend_usd:g} spent this month, "
            f"ticker {ticker.upper()}). Raise the cap or override, then rebuild."
        ),
    )
