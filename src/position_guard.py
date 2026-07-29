"""Naked-position gate (Monthly Red Team Phase 1, guard 7 —
``directives/monthly_red_team.md``).

Every held name above a rounding-error weight needs THREE things on file
before it counts as a covered position, not a naked bet:

* **(a) downside trigger encoded** — a downside exit rule the platform can
  actually enforce: either the holdings JSON carries non-empty
  ``break_rules`` / ``business_model_rules`` / ``break_rules_soft``, or the
  ticker's ``position_sizing_intent`` history carries an explicit
  below-current-price rung paired with a cut/trim/exit action. FLKR before
  its 2026-07-10 two-sided exit ladder is the canonical FAIL this check
  exists to catch: a real, sized position with zero machine-enforceable
  downside plan.
* **(b) realistic bear persisted** — ``bear_lint``'s classification for the
  name is ``ok`` or ``shallow``; ``missing`` or ``not_a_bear`` fail (guard 2
  of this same program).
* **(c) thesis freshness** — ``v_thesis_status.thesis_updated_at`` is on file
  and within :data:`THESIS_FRESHNESS_DAYS`; no thesis (or a stale one) fails.

All three reads are local disk/DB, read-only, and tolerant of a missing
substrate (empty findings, never a crash) — the same degrade discipline as
``bear_lint`` and ``portfolio_tail_stress``. The DB reads use their own
read-only ``sqlite3`` connection (``mode=ro``), never the write-capable
``user_state`` accessors, so this module can never mutate the Personal CIO
DB even by accident.

Real prod shapes (2026-07-11, verified read-only against ``data/portfolio.db``)
that shaped the conservative narrative parse in check (a):

* FLKR's sizing intent narrative (encoded 2026-07-10, after the adversarial
  review): "...DOWNSIDE (new): close <$57.50 (-20% from cycle high) -> cut to
  3.5% ... close <$50 ... -> exit the momentum position entirely..." — an
  explicit below-price rung immediately followed by a cut/exit action. PASS.
* RBRK's narrative: "...re-add discretion only below fair value ~$66.50." —
  a below-price trigger, but the action is RE-ADD (a buy-the-dip condition,
  the opposite of downside protection), not a cut/trim/exit. The narrative
  parse correctly does NOT pass this alone; RBRK instead passes via its
  holdings JSON's 3 universal + 7 business-model break rules.
* BKNG's narrative carries a funding rationale and a staged-entry note but no
  price rung at all; it too passes via its holdings JSON break rules (3 + 3),
  not the sizing intent.

Bull-side symmetry (Monthly Red Team PR9, ``directives/monthly_red_team.md``
Phase 1 last row): the three checks above are all downside-only — the program
attacked longs only. A fourth check, ``add_trigger``, is ADVISORY rather than
a violation: it asks whether a HIGH-CONVICTION held name (``position_entries
.entry_conviction = 'high'`` among currently-held, non-excluded names — today
MELI and NU) has an encoded upside pre-commitment on file, not just an exit
plan. Deliberately asymmetric severity: missing downside protection is a
standing violation (blocks the monthly close); missing upside protection is a
nudge (never counted in ``PositionGuardRow.passed``, ``failed_checks``, or the
``NAKED POSITIONS`` pill) — a high-conviction name with no add-rung is a
missed-opportunity risk, not a portfolio-blowup risk, and the two should never
compete for the same urgency budget.

**The add-rung intent contract.** An add-rung is a BUY pre-commitment: a
below-current-price trigger level paired with a sizing action and
(implicitly or explicitly) a thesis-intact condition — the mirror image of
the downside exit rung above. It is detected the same conservative way, from
``position_sizing_intent``:

* **explicit kind** — a row whose ``intent_kind`` is one of
  :data:`_ADD_INTENT_KINDS` (``add_rung`` is the canonical value a future
  sizing UI should write; ``buy_rung`` / ``add_trigger`` are accepted
  synonyms) passes outright, regardless of narrative content.
* **narrative parse** — a below-current-price rung (``<$X`` / ``below $X``,
  the same :data:`_PRICE_RUNG_RE`) within :data:`_PROXIMITY_CHARS` of an ADD
  action verb (add/buy/accumulate/scale — :data:`_ADD_ACTION_RE`), mirroring
  :func:`_narrative_encodes_downside`'s price-rung + action-verb requirement
  so a bare "below $X" (which could just as easily be a stop) never
  false-positive passes.

No prod row uses an explicit add-shaped ``intent_kind`` yet, and only DRAFT
narrative-based rows exist for MELI/NU as of this PR's data pass (Phase 2
step 3) — this contract is intentionally forward-looking, same posture as
:data:`_DOWNSIDE_INTENT_KINDS` above.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bear_lint import (
    STATUS_MISSING,
    STATUS_NOT_A_BEAR,
    STATUS_OK,
    STATUS_SHALLOW,
    BearLintFinding,
    build_bear_lint,
)
from portfolio_montecarlo import CASH_LIKE_TICKERS
from portfolio_weights import read_materialized_weights
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

__all__ = [
    "CHECK_ADD",
    "CHECK_BEAR",
    "CHECK_DOWNSIDE",
    "CHECK_THESIS",
    "EXCLUDED_TICKERS",
    "INDEX_ETF_TICKERS",
    "MIN_WEIGHT_PCT",
    "THESIS_FRESHNESS_DAYS",
    "IntentRow",
    "PositionGuardCheck",
    "PositionGuardReport",
    "PositionGuardRow",
    "build_position_guard",
    "evaluate_add_trigger",
    "evaluate_downside_trigger",
    "fetch_intent_rows",
]

# Broad-market index trackers carry no single-name thesis/bear/exit-ladder to
# encode — the naked-position gate is about single-name conviction bets, not
# the index sleeve. Kept separate from CASH_LIKE_TICKERS (a different reason
# for exclusion) but unioned into one exclusion set below.
INDEX_ETF_TICKERS: frozenset[str] = frozenset({"VTI", "SPY", "VOO"})
EXCLUDED_TICKERS: frozenset[str] = CASH_LIKE_TICKERS | INDEX_ETF_TICKERS

# A name below this weight is a rounding error, not a naked bet worth gating.
MIN_WEIGHT_PCT = 0.5

# v_thesis_status.thesis_updated_at older than this is stale (Phase 1 guard 7
# contract — directives/monthly_red_team.md).
THESIS_FRESHNESS_DAYS = 90

CHECK_DOWNSIDE = "downside_trigger"
CHECK_BEAR = "realistic_bear"
CHECK_THESIS = "thesis_freshness"
# ADVISORY only (module docstring "Bull-side symmetry") — deliberately never
# added to _PG_CHECK_ORDER-equivalent violation lists; see PositionGuardRow
# .add_trigger / .passed / .failed_checks below.
CHECK_ADD = "add_trigger"

# Forward-looking: no prod row uses an explicit downside-shaped intent_kind
# yet (every row on file is target_weight_pct with the downside encoded in
# free-text narrative) — but a future sizing UI that writes a dedicated kind
# should pass here without needing the narrative parse at all.
_DOWNSIDE_INTENT_KINDS: frozenset[str] = frozenset(
    {"downside_exit_price", "stop_loss_price", "exit_ladder", "downside_trigger"}
)

# Mirror of _DOWNSIDE_INTENT_KINDS for the advisory add-rung check (module
# docstring "The add-rung intent contract") — same forward-looking posture,
# same "explicit kind always wins" precedence.
_ADD_INTENT_KINDS: frozenset[str] = frozenset({"add_rung", "buy_rung", "add_trigger"})

# Conservative narrative parse for check (a): a below-current-price RUNG
# (``<$X`` / ``below $X``) within _PROXIMITY_CHARS of a downside ACTION verb
# (cut/trim/exit/sell/reduce). Requiring BOTH, close together, is what keeps
# RBRK's "re-add discretion only below fair value ~$66.50" (a below-price
# trigger whose action is a BUY, not downside protection) from false-positive
# passing — a bare "below $X" or a bare "cut to N%" alone proves nothing.
_PRICE_RUNG_RE = re.compile(r"(?:<=|<|≤|\bbelow\b)\s*\$\s*[\d][\d,]*(?:\.\d+)?", re.IGNORECASE)
_DOWNSIDE_ACTION_RE = re.compile(r"\b(cut|trim|exit|sell|reduce)\b", re.IGNORECASE)
# The mirror-image action verb set for the advisory add-rung narrative parse
# — same price-rung regex, a BUY-shaped action instead of a downside one.
_ADD_ACTION_RE = re.compile(r"\b(add|buy|accumulate|scale)\b", re.IGNORECASE)
_PROXIMITY_CHARS = 80
_EXCERPT_RADIUS = 45


@dataclass(slots=True, frozen=True)
class IntentRow:
    """The two ``position_sizing_intent`` columns this gate reads. Public (not
    module-private) because ``redteam.lenses`` reuses :func:`fetch_intent_rows`
    + :func:`evaluate_downside_trigger` / :func:`evaluate_add_trigger` to
    derive its evidence-pack flags from the exact same detection logic rather
    than re-deriving it."""

    intent_kind: str
    narrative: str | None


@dataclass(slots=True, frozen=True)
class PositionGuardCheck:
    """One name's read on one of the gate's checks (three violation-grade,
    one advisory — see :data:`CHECK_ADD`)."""

    passed: bool
    reason: str


@dataclass(slots=True, frozen=True)
class PositionGuardRow:
    """One held name's full naked-position read: the three violation-grade
    checks, plus the advisory ``add_trigger`` check (``None`` when the name
    isn't a high-conviction held position — the check doesn't apply, which is
    NOT the same as failing it)."""

    ticker: str
    weight_pct: float
    downside_trigger: PositionGuardCheck
    realistic_bear: PositionGuardCheck
    thesis_fresh: PositionGuardCheck
    # ADVISORY (Bull-side symmetry, PR9) — intentionally excluded from
    # `passed` / `failed_checks` below; see the module docstring.
    add_trigger: PositionGuardCheck | None = None

    @property
    def passed(self) -> bool:
        """Covered — every VIOLATION-grade check on file. A naked position
        fails at least one. ``add_trigger`` never participates — it's
        advisory, not a violation (module docstring "Bull-side symmetry")."""
        return (
            self.downside_trigger.passed and self.realistic_bear.passed and self.thesis_fresh.passed
        )

    @property
    def failed_checks(self) -> tuple[str, ...]:
        """Which check key(s) failed, in the fixed (a)/(b)/(c) order."""
        out: list[str] = []
        if not self.downside_trigger.passed:
            out.append(CHECK_DOWNSIDE)
        if not self.realistic_bear.passed:
            out.append(CHECK_BEAR)
        if not self.thesis_fresh.passed:
            out.append(CHECK_THESIS)
        return tuple(out)


@dataclass(slots=True)
class PositionGuardReport:
    """The book-wide naked-position gate: one row per held (weighted, non-
    excluded) name, violations-first."""

    rows: list[PositionGuardRow] = field(default_factory=list[PositionGuardRow])

    @property
    def violations(self) -> list[PositionGuardRow]:
        """Rows that fail at least one check — the standing-chip list."""
        return [r for r in self.rows if not r.passed]


def _narrative_encodes_rung(narrative: str, action_re: re.Pattern[str]) -> tuple[bool, str | None]:
    """``(matched, excerpt)`` for a below-current-price RUNG within
    :data:`_PROXIMITY_CHARS` of an action verb matching ``action_re``. Shared
    by both directions of the gate: :func:`_narrative_encodes_downside` calls
    this with :data:`_DOWNSIDE_ACTION_RE`, :func:`_narrative_encodes_add` with
    :data:`_ADD_ACTION_RE` — see the module docstring's RBRK/FLKR examples for
    why a price rung AND a nearby action verb are both required, not either
    alone."""
    rungs = [m.start() for m in _PRICE_RUNG_RE.finditer(narrative)]
    if not rungs:
        return False, None
    actions = [m.start() for m in action_re.finditer(narrative)]
    if not actions:
        return False, None
    for r in rungs:
        for a in actions:
            if abs(r - a) <= _PROXIMITY_CHARS:
                lo = max(0, min(r, a) - _EXCERPT_RADIUS)
                hi = min(len(narrative), max(r, a) + _EXCERPT_RADIUS)
                excerpt = narrative[lo:hi].strip()
                return True, excerpt
    return False, None


def _narrative_encodes_downside(narrative: str) -> tuple[bool, str | None]:
    return _narrative_encodes_rung(narrative, _DOWNSIDE_ACTION_RE)


def _narrative_encodes_add(narrative: str) -> tuple[bool, str | None]:
    """The add-rung mirror of :func:`_narrative_encodes_downside` — a
    below-current-price rung near a BUY-shaped action verb (add/buy/
    accumulate/scale), not a cut/trim/exit one."""
    return _narrative_encodes_rung(narrative, _ADD_ACTION_RE)


def _holdings_rule_counts(repo_root: Path, ticker: str) -> tuple[bool, str]:
    """``(has_any_rule, "N universal + M business-model + K soft")`` from
    ``micro_thesis/holdings/<TICKER>.json``. Pure disk read, tolerant of a
    missing file / malformed JSON / non-list rule arrays — never raises."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False, "no holdings JSON on file"
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return False, "holdings JSON unparsable"
    if not isinstance(payload, dict):
        return False, "holdings JSON malformed"
    d = cast("dict[str, object]", payload)
    n_hard = _rule_array_len(d.get("break_rules"))
    n_biz = _rule_array_len(d.get("business_model_rules"))
    n_soft = _rule_array_len(d.get("break_rules_soft"))
    counts = f"{n_hard} universal + {n_biz} business-model + {n_soft} soft break rule(s)"
    return (n_hard + n_biz + n_soft) > 0, counts


def _rule_array_len(raw: object) -> int:
    """0 for anything that isn't a JSON array (missing key, malformed field)."""
    return len(cast("list[object]", raw)) if isinstance(raw, list) else 0


def evaluate_downside_trigger(
    intents: Sequence[IntentRow], *, repo_root: Path, ticker: str
) -> PositionGuardCheck:
    """Check (a) — public so ``redteam.lenses`` can derive its
    ``has_downside_rung`` evidence-pack flag from this exact logic instead of
    re-deriving it (task: "reuse position_guard's detection logic")."""
    has_rules, counts = _holdings_rule_counts(repo_root, ticker)
    if has_rules:
        return PositionGuardCheck(True, f"holdings JSON: {counts}")
    for intent in intents:
        if intent.intent_kind in _DOWNSIDE_INTENT_KINDS:
            return PositionGuardCheck(
                True, f"sizing intent kind={intent.intent_kind} encodes downside"
            )
    for intent in intents:
        if not intent.narrative:
            continue
        matched, excerpt = _narrative_encodes_downside(intent.narrative)
        if matched:
            return PositionGuardCheck(
                True, f'sizing-intent narrative encodes a downside rung: "...{excerpt}..."'
            )
    return PositionGuardCheck(
        False,
        "no downside exit rule on file — no sizing-intent price rung + action, "
        f"and holdings JSON carries none ({counts})",
    )


def evaluate_add_trigger(intents: Sequence[IntentRow]) -> PositionGuardCheck:
    """The ADVISORY check (module docstring "Bull-side symmetry" / "The
    add-rung intent contract") — public for the same reuse reason as
    :func:`evaluate_downside_trigger`. Unlike the downside check, there is no
    holdings-JSON fallback: no "add rules" concept exists in the holdings
    schema, only ``position_sizing_intent``."""
    for intent in intents:
        if intent.intent_kind in _ADD_INTENT_KINDS:
            return PositionGuardCheck(
                True, f"sizing intent kind={intent.intent_kind} encodes an add-rung"
            )
    for intent in intents:
        if not intent.narrative:
            continue
        matched, excerpt = _narrative_encodes_add(intent.narrative)
        if matched:
            return PositionGuardCheck(
                True, f'sizing-intent narrative encodes an add-rung: "...{excerpt}..."'
            )
    return PositionGuardCheck(
        False,
        "no add-rung on file — high-conviction position with no encoded buy pre-commitment",
    )


_BEAR_FAIL_LABEL: dict[str, str] = {STATUS_NOT_A_BEAR: "NOT A BEAR", STATUS_MISSING: "MISSING"}


def _check_bear(finding: BearLintFinding | None) -> PositionGuardCheck:
    if finding is None:
        return PositionGuardCheck(False, "no bear-lint read on file")
    if finding.status in (STATUS_OK, STATUS_SHALLOW):
        return PositionGuardCheck(True, f"bear-lint {finding.status}: {finding.reason}")
    label = _BEAR_FAIL_LABEL.get(finding.status, finding.status.upper())
    return PositionGuardCheck(False, f"bear-lint {label}: {finding.reason}")


def _check_thesis(updated_at: datetime | None, now: datetime) -> PositionGuardCheck:
    if updated_at is None:
        return PositionGuardCheck(False, "no thesis on file (v_thesis_status: no timestamp)")
    age_days = (now - updated_at).days
    if age_days > THESIS_FRESHNESS_DAYS:
        return PositionGuardCheck(
            False, f"thesis last updated {age_days}d ago (> {THESIS_FRESHNESS_DAYS}d)"
        )
    return PositionGuardCheck(True, f"thesis updated {age_days}d ago")


def _parse_dt_or_none(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _read_sizing_intents(db_path: Path, tickers: Sequence[str]) -> dict[str, list[IntentRow]]:
    """``TICKER -> every position_sizing_intent row`` (all history, not just
    the latest — a downside rung encoded at any point is still on file for
    the platform to enforce). ``{}`` on a missing DB/table, mirroring
    ``bear_lint``'s degrade discipline."""
    want = sorted({t.upper() for t in tickers})
    if not want:
        return {}
    conn = _ro_conn(db_path)
    if conn is None:
        return {}
    try:
        placeholders = ",".join("?" for _ in want)
        rows = conn.execute(
            "SELECT ticker, intent_kind, narrative FROM position_sizing_intent "
            f"WHERE ticker IN ({placeholders})",
            want,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, list[IntentRow]] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        out.setdefault(t, []).append(
            IntentRow(
                intent_kind=str(r["intent_kind"]),
                narrative=(str(r["narrative"]) if r["narrative"] is not None else None),
            )
        )
    return out


def fetch_intent_rows(conn: sqlite3.Connection, ticker: str) -> list[IntentRow]:
    """One ticker's ``position_sizing_intent`` history via an ALREADY-OPEN
    connection (as opposed to :func:`_read_sizing_intents`, which opens its
    own read-only connection for a batch of tickers) — the read-through path
    ``redteam.lenses.build_name_evidence_pack`` uses so its
    ``has_downside_rung`` / ``has_add_rung`` evidence flags share this
    module's exact query, not a re-derived one. ``[]`` on any read error, the
    same tolerant posture as every other read helper in this module."""
    try:
        rows = conn.execute(
            "SELECT intent_kind, narrative FROM position_sizing_intent WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        IntentRow(
            intent_kind=str(r["intent_kind"]),
            narrative=(str(r["narrative"]) if r["narrative"] is not None else None),
        )
        for r in rows
    ]


def _read_high_conviction(db_path: Path, tickers: Sequence[str]) -> frozenset[str]:
    """Tickers among ``tickers`` with an OPEN ``position_entries`` row whose
    ``entry_conviction = 'high'`` — the advisory ``add_trigger`` check's
    scope (module docstring "today MELI and NU"). ``frozenset()`` on a
    missing DB/table (pre-0088 schema), matching this module's degrade
    discipline everywhere else."""
    want = sorted({t.upper() for t in tickers})
    if not want:
        return frozenset()
    conn = _ro_conn(db_path)
    if conn is None:
        return frozenset()
    try:
        placeholders = ",".join("?" for _ in want)
        rows = conn.execute(
            "SELECT ticker FROM position_entries WHERE ticker IN "
            f"({placeholders}) AND exit_date IS NULL AND entry_conviction = 'high'",
            want,
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    finally:
        conn.close()
    return frozenset(str(r["ticker"]).upper() for r in rows)


def _read_thesis_updated_at(db_path: Path, tickers: Sequence[str]) -> dict[str, datetime | None]:
    """``TICKER -> thesis_updated_at`` from ``v_thesis_status``. ``{}`` on a
    DB that predates the view (pre-0143) or is missing entirely."""
    want = sorted({t.upper() for t in tickers})
    if not want:
        return {}
    conn = _ro_conn(db_path)
    if conn is None:
        return {}
    try:
        placeholders = ",".join("?" for _ in want)
        rows = conn.execute(
            f"SELECT ticker, thesis_updated_at FROM v_thesis_status WHERE ticker IN ({placeholders})",
            want,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(r["ticker"]).upper(): _parse_dt_or_none(r["thesis_updated_at"]) for r in rows}


def build_position_guard(
    db_path: Path, repo_root: Path | None = None, *, now: datetime | None = None
) -> PositionGuardReport:
    """The book-wide naked-position gate over every held (weighted, non-
    excluded) name. ``repo_root`` defaults to ``db_path``'s grandparent (the
    repo root holding ``data/portfolio.db`` and ``micro_thesis/``). Pure
    disk/DB reads (read-only connections only) — no network, no LLM, renders
    with the tracker down."""
    root = repo_root or db_path.parent.parent
    weights = read_materialized_weights(root)
    positive = {
        t.upper(): w
        for t, w in weights.items()
        if w * 100.0 > MIN_WEIGHT_PCT and t.upper() not in EXCLUDED_TICKERS
    }
    if not positive:
        return PositionGuardReport(rows=[])
    now_dt = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
    tickers = list(positive)
    bear_by_ticker = {f.ticker: f for f in build_bear_lint(db_path, root).findings}
    intents_by_ticker = _read_sizing_intents(db_path, tickers)
    thesis_updated_by_ticker = _read_thesis_updated_at(db_path, tickers)
    high_conviction = _read_high_conviction(db_path, tickers)

    rows: list[PositionGuardRow] = []
    for t in sorted(positive):
        ticker_intents = intents_by_ticker.get(t, [])
        rows.append(
            PositionGuardRow(
                ticker=t,
                weight_pct=positive[t] * 100.0,
                downside_trigger=evaluate_downside_trigger(
                    ticker_intents, repo_root=root, ticker=t
                ),
                realistic_bear=_check_bear(bear_by_ticker.get(t)),
                thesis_fresh=_check_thesis(thesis_updated_by_ticker.get(t), now_dt),
                add_trigger=(
                    evaluate_add_trigger(ticker_intents) if t in high_conviction else None
                ),
            )
        )
    # Violations first (largest weight first within each group) — the same
    # "attention list" ordering as bear_lint.
    rows.sort(key=lambda r: (r.passed, -r.weight_pct, r.ticker))
    return PositionGuardReport(rows=rows)
