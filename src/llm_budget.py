"""Per-purpose LLM budget enforcement.

The llm_calls ledger (migration 0034) records what we spent. The
llm_budgets / llm_budget_alerts tables (migration 0052) record what we're
willing to spend. This module is the glue:

  * `current_month_spend(purpose)` — sum cost_estimate_usd in llm_calls
    where called_at >= start-of-month. The number any UI / pre-call check
    needs to know "are we over budget?".
  * `check_budget(purpose)` — returns BudgetCheck telling the caller
    whether to allow / warn / block the next call.
  * `record_alert(purpose, threshold_pct, current_spend)` — idempotent
    write to llm_budget_alerts so the same threshold crossing in the same
    month writes one row, not one per LLM call.

Every helper degrades gracefully when the DB or tables are missing — the
LLM call must NEVER fail because the budget enforcer couldn't read its
own bookkeeping. A budget check on a fresh repo (no migrations run yet)
returns "allowed" and logs at DEBUG.

The enforcer is wired into `src/llm_client._call_claude` so every LLM
call passes through it pre-flight. CLI tools that need to bypass (one-off
forced refresh) can pass `force_budget_bypass=True` to `call_llm`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)

# Special key in llm_budgets for purposes not explicitly listed. Seeded by
# migration 0052. If a caller asks about a purpose with no row of its own,
# the enforcer reads this row instead.
DEFAULT_BUDGET_KEY = "__default__"

# Decimal precision: cost_estimate_usd is stored as FLOAT in llm_calls (the
# Phase 0 ledger uses sqlite REAL) but we convert to Decimal at the boundary
# so accumulation stays cent-accurate. 4 decimal places matches the schema's
# Numeric(10,4) on spend_at_alert_usd.
_DECIMAL_QUANT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Result of a pre-call budget enforcement check.

    `allowed=False, hard_block=True` → caller MUST raise LLMBudgetExceeded.
    `allowed=False, hard_block=False` → soft cap, caller logs + proceeds.
    `warn=True` → 80% threshold crossed, caller logs + records alert.
    `reason` is a short human-readable string for log messages — None when
    the call is fully allowed with no warning.

    `on_exceed` is the per-purpose cap-exceeded MODE (migration 0066) and is the
    authoritative behavior knob: `'block'` → `hard_block=True` (raise / propagate),
    `'skip'` → forgo the call and mark the section forgone-due-to-budget (handled
    pre-flight via `should_skip_for_budget`), `'warn'` → proceed past the cap.
    `hard_block` is derived from it (`on_exceed == 'block'`) for back-compat.
    """

    allowed: bool
    warn: bool
    current_spend: Decimal
    cap: Decimal
    headroom_pct: float  # 1.0 = no spend, 0.0 = at cap, negative = over cap
    hard_block: bool
    on_exceed: str = "warn"
    reason: str | None = None


def _resolve_db_path(override: Path | str | None) -> Path | None:
    """Same resolution pattern as llm_call_ledger: explicit override wins,
    then db.DB_PATH if importable, else None (caller has no DB context)."""
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(DB_PATH)
    except ImportError:
        return None


def _month_start_iso(now: datetime | None = None) -> tuple[str, str]:
    """Return (start_of_current_month_iso, 'YYYY-MM') for the given clock
    or now (UTC). Helper for the spend window and the alert key."""
    n = now or datetime.now(UTC)
    start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), f"{n.year:04d}-{n.month:02d}"


def current_month_spend(
    purpose: str, *, db_path: Path | str | None = None, now: datetime | None = None
) -> Decimal:
    """Sum cost_estimate_usd in llm_calls for `purpose` since start of
    current month. Returns Decimal('0') when the DB / table is missing,
    when there are no rows yet, or on any read error — the budget check
    fails open so a broken ledger doesn't block LLM calls.

    `now` is optional for tests that need a deterministic month boundary.
    """
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return Decimal("0")
    start_iso, _ = _month_start_iso(now)
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(cost_estimate_usd), 0.0) AS total
                FROM llm_calls
                WHERE purpose = ? AND called_at >= ?
                """,
                (purpose, start_iso),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # Missing table, locked DB, schema mismatch — all best-effort.
        log.debug(
            {
                "event": "current_month_spend_read_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return Decimal("0")
    if row is None or row[0] is None:
        return Decimal("0")
    return Decimal(str(row[0])).quantize(_DECIMAL_QUANT)


@dataclass(frozen=True, slots=True)
class _BudgetRow:
    """In-memory shape of a single llm_budgets row. Used internally; the
    public type is BudgetCheck, which combines this with current spend."""

    purpose: str
    cap: Decimal
    warn_threshold_pct: float
    hard_block: bool
    on_exceed: str  # 'skip' | 'block' | 'warn' (authoritative; migration 0066)


def _has_on_exceed(conn: sqlite3.Connection) -> bool:
    """True when llm_budgets carries the `on_exceed` column (migration 0066).

    Pre-0066 DBs (and the hand-rolled test fixtures) lack it; callers derive the
    mode from the legacy `hard_block` bool instead so enforcement is unchanged.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_budgets)").fetchall()}
    except sqlite3.Error:
        return False
    return "on_exceed" in cols


def _row_on_exceed(row: sqlite3.Row, *, has_mode: bool, hard_block: bool) -> str:
    """Read `on_exceed` from a budget row, deriving from `hard_block` when the
    column is absent (pre-0066) or carries an unexpected value."""
    if has_mode:
        val = row["on_exceed"]
        if isinstance(val, str) and val in ("skip", "block", "warn"):
            return val
    return "block" if hard_block else "warn"


def _load_budget(
    purpose: str, *, db_path: Path | str | None = None
) -> _BudgetRow | None:
    """Look up the budget row for `purpose`, falling back to '__default__'.
    Returns None when neither row exists (no migrations run, or seed
    missing). Read failures return None — the enforcer fails open."""
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            has_mode = _has_on_exceed(conn)
            if has_mode:
                sql = (
                    "SELECT purpose, monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
                    "FROM llm_budgets WHERE purpose = ?"
                )
            else:
                sql = (
                    "SELECT purpose, monthly_cap_usd, warn_threshold_pct, hard_block "
                    "FROM llm_budgets WHERE purpose = ?"
                )
            row = conn.execute(sql, (purpose,)).fetchone()
            if row is None:
                row = conn.execute(sql, (DEFAULT_BUDGET_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "load_budget_read_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if row is None:
        return None
    hard_block = bool(row["hard_block"])
    return _BudgetRow(
        purpose=str(row["purpose"]),
        cap=Decimal(str(row["monthly_cap_usd"])).quantize(_DECIMAL_QUANT),
        warn_threshold_pct=float(row["warn_threshold_pct"]),
        hard_block=hard_block,
        on_exceed=_row_on_exceed(row, has_mode=has_mode, hard_block=hard_block),
    )


def _headroom_pct(spend: Decimal, cap: Decimal) -> float:
    """Fraction of cap remaining. 1.0 = no spend, 0.0 = at cap, negative
    = over cap. Returns 1.0 when cap is zero (no enforcement)."""
    if cap <= 0:
        return 1.0
    return float((cap - spend) / cap)


def check_budget(
    purpose: str | None,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> BudgetCheck:
    """Pre-call budget enforcement.

    Returns BudgetCheck — caller decides what to do based on the flags.
    The enforcer is permissive on failure:

      * `purpose=None` → allowed, no warn (legacy callers not yet wired
        through purpose-aware call_llm).
      * No budget row found → allowed, no warn (fresh repo, migration
        not run yet, or seed missing).
      * DB read error → allowed, no warn (logged at DEBUG).

    Only an actual cap crossing returns `allowed=False`.
    """
    if purpose is None:
        return BudgetCheck(
            allowed=True,
            warn=False,
            current_spend=Decimal("0"),
            cap=Decimal("0"),
            headroom_pct=1.0,
            hard_block=False,
            on_exceed="warn",
            reason=None,
        )
    budget = _load_budget(purpose, db_path=db_path)
    if budget is None:
        return BudgetCheck(
            allowed=True,
            warn=False,
            current_spend=Decimal("0"),
            cap=Decimal("0"),
            headroom_pct=1.0,
            hard_block=False,
            on_exceed="warn",
            reason=None,
        )
    spend = current_month_spend(purpose, db_path=db_path, now=now)
    headroom = _headroom_pct(spend, budget.cap)
    # `on_exceed` is authoritative (migration 0066); `hard_block` is derived so
    # the pre-call gate (which raises on it) propagates iff the mode is 'block'.
    hard_block = budget.on_exceed == "block"
    if headroom <= 0:
        # At or over cap.
        return BudgetCheck(
            allowed=False,
            warn=False,
            current_spend=spend,
            cap=budget.cap,
            headroom_pct=headroom,
            hard_block=hard_block,
            on_exceed=budget.on_exceed,
            reason=(
                f"{purpose}: monthly spend ${spend} >= cap ${budget.cap}"
                f" (on_exceed={budget.on_exceed})"
            ),
        )
    warn_at = Decimal(str(budget.warn_threshold_pct)) * budget.cap
    if spend >= warn_at:
        return BudgetCheck(
            allowed=True,
            warn=True,
            current_spend=spend,
            cap=budget.cap,
            headroom_pct=headroom,
            hard_block=hard_block,
            on_exceed=budget.on_exceed,
            reason=(
                f"{purpose}: spend ${spend} >= warn threshold"
                f" {budget.warn_threshold_pct:.0%} of ${budget.cap}"
            ),
        )
    return BudgetCheck(
        allowed=True,
        warn=False,
        current_spend=spend,
        cap=budget.cap,
        headroom_pct=headroom,
        hard_block=hard_block,
        on_exceed=budget.on_exceed,
        reason=None,
    )


_VALID_MODES: frozenset[str] = frozenset({"skip", "block", "warn"})


def budget_mode(purpose: str, *, db_path: Path | str | None = None) -> str:
    """Cap-exceeded MODE for `purpose` ('skip' | 'block' | 'warn').

    Falls back to '__default__' then to 'warn' (non-enforcing) when no row
    exists. Authoritative source is the `on_exceed` column (migration 0066),
    derived from the legacy `hard_block` bool on pre-0066 DBs.
    """
    budget = _load_budget(purpose, db_path=db_path)
    return budget.on_exceed if budget is not None else "warn"


def should_skip_for_budget(
    purpose: str,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    bypass: bool = False,
) -> BudgetCheck | None:
    """Pre-flight gate for the `skip` mode — the heart of "forgone due to budget".

    Returns the failing BudgetCheck (carrying cap / spend / headroom) when
    `purpose` is configured `on_exceed='skip'` AND is at/over its monthly cap —
    i.e. the section should FORGO the LLM call (no spend) and render a
    forgone-due-to-budget banner. Returns None to PROCEED: under cap, or a
    non-skip mode ('block' raises at the call gate, 'warn' overspends), or
    `bypass=True` (per-run override). Best-effort — never raises.
    """
    if bypass:
        return None
    try:
        check = check_budget(purpose, db_path=db_path, now=now)
    except Exception as exc:  # the budget gate must never break the build
        log.debug(
            {
                "event": "should_skip_for_budget_failed_open",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
    if check.on_exceed == "skip" and not check.allowed:
        return check
    return None


def record_alert(
    purpose: str,
    threshold_pct: float,
    current_spend: Decimal,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Write one row to llm_budget_alerts, idempotent on
    (purpose, month, threshold_pct). Returns True if a new row was written,
    False if the alert was already recorded for this month or the DB is
    unavailable.

    The dedupe is what keeps the WARNING log from spamming every LLM call
    after burn first crosses 80% — we record once, the dashboard reads it,
    the operator decides what to do.
    """
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    _, month = _month_start_iso(now)
    alerted_at = (now or datetime.now(UTC)).isoformat()
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            cur = conn.execute(
                """
                INSERT INTO llm_budget_alerts
                    (purpose, month, threshold_pct, alerted_at, spend_at_alert_usd)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(purpose, month, threshold_pct) DO NOTHING
                """,
                (
                    purpose,
                    month,
                    threshold_pct,
                    alerted_at,
                    str(current_spend),
                ),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "record_alert_write_failed",
                "purpose": purpose,
                "threshold_pct": threshold_pct,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False


def list_budgets(
    *, db_path: Path | str | None = None, now: datetime | None = None
) -> list[dict[str, object]]:
    """Return all llm_budgets rows annotated with the current month's spend
    and headroom%. Used by the manage_llm_budget CLI and the dashboard panel.
    Returns [] when the DB / table is missing."""
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            has_mode = _has_on_exceed(conn)
            if has_mode:
                rows = conn.execute(
                    """
                    SELECT purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                           on_exceed, created_at, updated_at, notes
                    FROM llm_budgets
                    ORDER BY purpose
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                           created_at, updated_at, notes
                    FROM llm_budgets
                    ORDER BY purpose
                    """
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "list_budgets_read_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return []
    out: list[dict[str, object]] = []
    for r in rows:
        purpose = str(r["purpose"])
        cap = Decimal(str(r["monthly_cap_usd"])).quantize(_DECIMAL_QUANT)
        spend = current_month_spend(purpose, db_path=db_path, now=now)
        hard_block = bool(r["hard_block"])
        out.append(
            {
                "purpose": purpose,
                "monthly_cap_usd": cap,
                "warn_threshold_pct": float(r["warn_threshold_pct"]),
                "hard_block": hard_block,
                "on_exceed": _row_on_exceed(r, has_mode=has_mode, hard_block=hard_block),
                "current_spend_usd": spend,
                "headroom_pct": _headroom_pct(spend, cap),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
                "notes": r["notes"],
            }
        )
    return out


def set_cap(
    purpose: str,
    new_cap_usd: Decimal | float,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Update monthly_cap_usd for `purpose`. Returns True on update,
    False if the row didn't exist or the DB is unavailable. Used by the
    manage_llm_budget CLI."""
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    cap_str = str(Decimal(str(new_cap_usd)).quantize(Decimal("0.01")))
    updated_at = (now or datetime.now(UTC)).isoformat()
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                UPDATE llm_budgets
                SET monthly_cap_usd = ?, updated_at = ?
                WHERE purpose = ?
                """,
                (cap_str, updated_at, purpose),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "set_cap_write_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False


def set_mode(
    purpose: str,
    mode: str,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Set the cap-exceeded `on_exceed` mode for `purpose` ('skip'|'block'|'warn').

    Also syncs the legacy `hard_block` bool (= mode == 'block') so back-compat
    readers (list_budgets, month_report) stay consistent. Returns True on
    update, False if the row didn't exist / DB unavailable. Raises ValueError on
    an invalid mode. Used by the manage_llm_budget CLI + the dashboard panel.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"invalid on_exceed mode {mode!r}; expected one of {sorted(_VALID_MODES)}"
        )
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    updated_at = (now or datetime.now(UTC)).isoformat()
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cur = conn.execute(
                "UPDATE llm_budgets SET on_exceed = ?, hard_block = ?, updated_at = ? "
                "WHERE purpose = ?",
                (mode, 1 if mode == "block" else 0, updated_at, purpose),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "set_mode_write_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False


def month_report(
    month: str | None = None, *, db_path: Path | str | None = None
) -> list[dict[str, object]]:
    """Per-purpose spend report for the given month (YYYY-MM) or the current
    month if None. Joins budgets ⨝ aggregated spend so the report shows
    capped + uncapped purposes side by side. Returns [] when the DB is
    missing."""
    path = _resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return []
    if month is None:
        now = datetime.now(UTC)
        month = f"{now.year:04d}-{now.month:02d}"
    # Range: [month-01T00:00, next-month-01T00:00)
    year_str, month_str = month.split("-")
    year, mnum = int(year_str), int(month_str)
    if mnum == 12:
        next_year, next_mnum = year + 1, 1
    else:
        next_year, next_mnum = year, mnum + 1
    start_iso = f"{year:04d}-{mnum:02d}-01T00:00:00+00:00"
    end_iso = f"{next_year:04d}-{next_mnum:02d}-01T00:00:00+00:00"
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    COALESCE(b.purpose, c.purpose) AS purpose,
                    b.monthly_cap_usd AS cap,
                    b.hard_block AS hard_block,
                    COALESCE(SUM(c.cost_estimate_usd), 0.0) AS spend,
                    COUNT(c.id) AS calls
                FROM llm_budgets b
                LEFT JOIN llm_calls c
                  ON c.purpose = b.purpose
                  AND c.called_at >= ? AND c.called_at < ?
                GROUP BY b.purpose

                UNION ALL

                -- Purposes seen in llm_calls that have no budget row.
                SELECT c.purpose AS purpose,
                       NULL AS cap, NULL AS hard_block,
                       SUM(c.cost_estimate_usd) AS spend,
                       COUNT(c.id) AS calls
                FROM llm_calls c
                LEFT JOIN llm_budgets b ON b.purpose = c.purpose
                WHERE c.called_at >= ? AND c.called_at < ?
                  AND b.purpose IS NULL
                  AND c.purpose IS NOT NULL
                GROUP BY c.purpose

                ORDER BY spend DESC
                """,
                (start_iso, end_iso, start_iso, end_iso),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "month_report_read_failed",
                "month": month,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return []
    out: list[dict[str, object]] = []
    for r in rows:
        spend = Decimal(str(r["spend"] or 0)).quantize(_DECIMAL_QUANT)
        cap = (
            Decimal(str(r["cap"])).quantize(_DECIMAL_QUANT)
            if r["cap"] is not None
            else None
        )
        headroom = _headroom_pct(spend, cap) if cap is not None else None
        out.append(
            {
                "purpose": str(r["purpose"]),
                "spend_usd": spend,
                "calls": int(r["calls"] or 0),
                "cap_usd": cap,
                "headroom_pct": headroom,
                "hard_block": bool(r["hard_block"]) if r["hard_block"] is not None else None,
            }
        )
    return out
