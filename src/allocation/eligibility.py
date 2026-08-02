"""Decision-ready eligibility gate (PRD ``docs/design/personal_investment_partner_prd.md``
§7.3, P0.3). Pure deterministic checks — no LLM, no network, no writes.

The Incremental Dollar Recommendation (P0.4, out of scope here) may only prefer
a security this module marks ``eligible=True``. **The governed LLM can never
override ``eligible=False``** — every field on :class:`DecisionReadyAssessment`
is computed entirely by the eight checks below, before any LLM sees the name;
nothing downstream may flip a blocked name to eligible.

Eight checks (PRD §7.3), each a typed :class:`EligibilityCheck`:

1. **Price freshness** — the latest ``dcf_runs`` row's ``live_price_at``, within
   :data:`risk_reward.PRICE_STALE_DAYS`.
2. **Usable DCF** — same latest row: no blocking ``sanity_flag``, and both
   ``npv_per_share``/``live_price`` present. The gap is recomputed from those
   two fields, never read off ``over_under_pct`` (bank/holdco archetypes store
   that column in a different convention — same rule
   ``research_cockpit.latest_dcf_runs`` follows).
3. **KPI coverage** — ``kpi_facts`` distinct-definition coverage against
   ``kpi_definitions`` for the ticker.
4. **Candidate fit** — evaluation names only; portfolio names auto-pass
   (``portfolio_fit_status="held"``).
5. **Explicit directional hypothesis** — ``thesis_state``; a ``STUB:`` marker is
   a permitted, LABELED system-drafted hypothesis (not a prerequisite for a
   user-authored one); a missing thesis or a corruption-recovery stub blocks.
6. **>=1 disconfirmer** — holdings-JSON hard break rules, or (warning-only)
   qualitative-only disconfirmers (``thesis_breakers_qualitative``).
7. **Portfolio context** — the materialized weights cache's freshness.
8. **Source provenance/as-of** — every source required for this name's class
   (portfolio vs evaluation) carries an as-of date.

Cash is always eligible (:func:`cash_assessment`) — a synthetic row, not run
through the eight checks.

Degrade-don't-crash throughout: a missing DB, missing cache, or missing file
never raises — it fails the checks that depend on it (which is the correct
answer for an eligibility GATE: "unknown" must never read as "eligible").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from allocation.candidate_fit import CandidateFit
from compute.thesis_evaluator import HoldingsSpec, load_holdings_spec
from models.companies import ListType
from pipeline.queries import tracked_companies_for_user
from portfolio_risk_snapshot_store import read_latest_snapshot
from portfolio_weights import read_materialized_weights_as_of
from risk_reward import PRICE_STALE_DAYS
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

__all__ = [
    "CHECK_CANDIDATE_FIT",
    "CHECK_DIRECTIONAL_HYPOTHESIS",
    "CHECK_DISCONFIRMERS",
    "CHECK_KPI_COVERAGE",
    "CHECK_PORTFOLIO_CONTEXT",
    "CHECK_PRICE_FRESHNESS",
    "CHECK_SOURCE_PROVENANCE",
    "CHECK_USABLE_DCF",
    "KPI_COVERAGE_MIN",
    "WEIGHTS_CACHE_STALE_HOURS",
    "DecisionReadyAssessment",
    "EligibilityCheck",
    "assess_eligibility",
    "assess_universe",
    "cash_assessment",
]

CHECK_PRICE_FRESHNESS = "price_freshness"
CHECK_USABLE_DCF = "usable_dcf"
CHECK_KPI_COVERAGE = "kpi_coverage"
CHECK_CANDIDATE_FIT = "candidate_fit"
CHECK_DIRECTIONAL_HYPOTHESIS = "directional_hypothesis"
CHECK_DISCONFIRMERS = "disconfirmers"
CHECK_PORTFOLIO_CONTEXT = "portfolio_context"
CHECK_SOURCE_PROVENANCE = "source_provenance"

_ALL_CHECKS: tuple[str, ...] = (
    CHECK_PRICE_FRESHNESS,
    CHECK_USABLE_DCF,
    CHECK_KPI_COVERAGE,
    CHECK_CANDIDATE_FIT,
    CHECK_DIRECTIONAL_HYPOTHESIS,
    CHECK_DISCONFIRMERS,
    CHECK_PORTFOLIO_CONTEXT,
    CHECK_SOURCE_PROVENANCE,
)

# Check 3 rule (PRD §7.3): coverage >= this floor passes outright.
KPI_COVERAGE_MIN = 0.5

# Check 7 rule: the materialized weights cache is fresh enough to trust for
# the frontier's resulting-weight math up to this age; older is a WARNING,
# absent is BLOCKING.
WEIGHTS_CACHE_STALE_HOURS = 48.0

# Sources every name in this list_type requires an as-of for (check 8). "fit"
# is required for evaluation names only — check 4 auto-passes portfolio names
# without a fit read, so there is nothing to date there.
_REQUIRED_SOURCES_BY_CLASS: dict[str, tuple[str, ...]] = {
    ListType.PORTFOLIO.value: ("price", "dcf", "weights", "thesis"),
    ListType.EVALUATION.value: ("price", "dcf", "weights", "thesis", "fit"),
}

# Mirrors compute.thesis_evaluator._is_corruption_stub's status literal
# (thesis_evaluator.py:626-641) — duplicated rather than imported (that
# helper is module-private; repo convention is to duplicate a few lines of
# shared logic rather than promote a private helper to a shared import).
_STUB_REGENERATED_STATUS = "stub_regenerated_from_corruption"


@dataclass(slots=True, frozen=True)
class EligibilityCheck:
    """One name's read on one of the eight §7.3 checks."""

    passed: bool
    reason: str


@dataclass(slots=True, frozen=True)
class DecisionReadyAssessment:
    """One security's decision-ready eligibility read (PRD §7.3).

    ``eligible`` is the AND of every check's ``passed`` — computed entirely
    in this module, from disk/DB reads only. **No LLM may ever override
    ``eligible=False``**: P0.4's governed selection receives only names this
    gate already marked eligible, and nothing downstream can flip a blocked
    name back on. ``warning_reasons`` never affect ``eligible`` — they are
    context the P0.4 selection (or a human) should weigh, not a blocker.
    """

    ticker: str
    list_type: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    warning_reasons: tuple[str, ...]
    source_freshness: dict[str, str]
    hypothesis_origin: Literal["user_authored", "system_drafted", "missing"]
    portfolio_fit_status: str
    checks: dict[str, EligibilityCheck]
    input_sha: str


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _sha(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _parse_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _is_corruption_stub(raw_json: object) -> bool:
    """Local mirror of ``compute.thesis_evaluator._is_corruption_stub`` — see
    the module-level note above ``_STUB_REGENERATED_STATUS``."""
    if not isinstance(raw_json, str) or not raw_json:
        return False
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    status = cast("dict[str, object]", parsed).get("_status")
    return status == _STUB_REGENERATED_STATUS


# --------------------------------------------------------------------------- #
# Check 1 + 2 — dcf_runs (shared row read)
# --------------------------------------------------------------------------- #


def _dcf_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    except sqlite3.Error:
        return set()


def _latest_dcf_row(conn: sqlite3.Connection | None, ticker: str) -> sqlite3.Row | None:
    """The latest consolidated (unsegmented) dcf_runs row for ``ticker`` — the
    same ``COALESCE(is_latest, 1) = 1`` / ``COALESCE(segment_name, '') = ''``
    column-presence-guarded query template used by ``bear_lint`` and
    ``model_provenance.basis``. NOT shared with ``risk_reward._dcf_reward_legs``:
    that query has no ``is_latest``/``segment_name`` predicates at all (it reads
    every dcf_runs row ordered by ticker/created_at/id and the caller takes the
    first per ticker) — despite this docstring previously claiming otherwise."""
    if conn is None:
        return None
    cols = _dcf_columns(conn)
    if not cols:
        return None
    is_latest_pred = "COALESCE(is_latest, 1) = 1" if "is_latest" in cols else "1 = 1"
    seg_pred = "COALESCE(segment_name, '') = ''" if "segment_name" in cols else "1 = 1"
    sanity_sel = "sanity_flag" if "sanity_flag" in cols else "NULL AS sanity_flag"
    price_at_sel = "live_price_at" if "live_price_at" in cols else "NULL AS live_price_at"
    try:
        return conn.execute(
            f"SELECT ticker, valuation_date, npv_per_share, live_price, "
            f"{price_at_sel}, {sanity_sel}, created_at, id FROM dcf_runs "
            f"WHERE UPPER(ticker) = ? AND {is_latest_pred} AND {seg_pred} "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None


def _check_price_freshness(
    row: sqlite3.Row | None, *, now: datetime
) -> tuple[EligibilityCheck, str | None]:
    if row is None:
        return EligibilityCheck(False, "no dcf_runs row on file — price freshness unknown"), None
    raw_at = row["live_price_at"]
    if not raw_at:
        return EligibilityCheck(False, "live_price_at not recorded on the latest run"), None
    dt = _parse_dt(raw_at)
    if dt is None:
        return EligibilityCheck(False, f"live_price_at unparseable ({raw_at!r})"), str(raw_at)
    age_days = (now - dt).days
    asof = str(raw_at)
    if age_days > PRICE_STALE_DAYS:
        return (
            EligibilityCheck(
                False, f"price {age_days}d stale (> {PRICE_STALE_DAYS}d), as of {asof}"
            ),
            asof,
        )
    return (
        EligibilityCheck(True, f"price {age_days}d old (<= {PRICE_STALE_DAYS}d), as of {asof}"),
        asof,
    )


def _check_usable_dcf(row: sqlite3.Row | None) -> tuple[EligibilityCheck, str | None]:
    if row is None:
        return EligibilityCheck(False, "no dcf_runs row on file"), None
    val_date = row["valuation_date"] or "?"
    asof = f"{val_date} (run {row['id']})"
    sanity = row["sanity_flag"]
    if sanity:
        return (
            EligibilityCheck(
                False, f"sanity_flag={sanity!r} on the latest run — blocking (unreviewed valuation)"
            ),
            asof,
        )
    fv = row["npv_per_share"]
    px = row["live_price"]
    if fv is None or px is None:
        return EligibilityCheck(
            False, "npv_per_share or live_price missing on the latest run"
        ), asof
    fv_f, px_f = float(fv), float(px)
    gap = f" ({(px_f / fv_f - 1.0) * 100.0:+.1f}% price vs fair)" if fv_f != 0.0 else ""
    return (
        EligibilityCheck(True, f"usable DCF: fair ${fv_f:.2f} vs price ${px_f:.2f}{gap}"),
        asof,
    )


# --------------------------------------------------------------------------- #
# Check 3 — KPI coverage
# --------------------------------------------------------------------------- #


def _count(conn: sqlite3.Connection | None, sql: str, ticker: str) -> int:
    if conn is None:
        return 0
    try:
        row = conn.execute(sql, (ticker,)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row is not None and row[0] is not None else 0


def _check_kpi_coverage(
    conn: sqlite3.Connection | None, ticker: str, *, list_type: str
) -> tuple[EligibilityCheck, str | None]:
    n_def = _count(conn, "SELECT COUNT(*) FROM kpi_definitions WHERE UPPER(ticker) = ?", ticker)
    n_covered = _count(
        conn,
        "SELECT COUNT(DISTINCT kpi_definition_id) FROM kpi_facts WHERE UPPER(ticker) = ?",
        ticker,
    )
    is_evaluation = list_type == ListType.EVALUATION.value
    if n_def == 0:
        reason = "no KPI definitions on file"
        if is_evaluation:
            return EligibilityCheck(False, reason), None
        return EligibilityCheck(True, f"{reason} — warning for a held name"), reason
    coverage = n_covered / n_def
    detail = f"{n_covered}/{n_def} KPI definitions have facts ({coverage:.0%} coverage)"
    if coverage >= KPI_COVERAGE_MIN:
        return EligibilityCheck(True, detail), None
    floor_note = f"below the {KPI_COVERAGE_MIN:.0%} floor"
    if is_evaluation:
        return EligibilityCheck(False, f"{detail} — {floor_note}"), None
    warning = f"{detail} — {floor_note}"
    return EligibilityCheck(True, f"{warning}, warning for a held name"), warning


# --------------------------------------------------------------------------- #
# Check 4 — candidate fit
# --------------------------------------------------------------------------- #


def _check_candidate_fit(
    ticker: str,
    *,
    list_type: str,
    fit_cache: dict[str, CandidateFit],
    fit_meta: dict[str, object],
) -> tuple[EligibilityCheck, str, str | None]:
    if list_type == ListType.PORTFOLIO.value:
        return EligibilityCheck(True, "held name — candidate fit not required"), "held", None
    fit = fit_cache.get(ticker)
    if fit is None:
        return EligibilityCheck(False, "no candidate-fit result on file"), "missing", None
    computed_at = fit_meta.get("computed_at")
    asof = str(computed_at) if isinstance(computed_at, str) else None
    return EligibilityCheck(True, f"candidate fit {fit.fit:.2f} on file"), "scored", asof


# --------------------------------------------------------------------------- #
# Check 5 — explicit directional hypothesis
# --------------------------------------------------------------------------- #


def _check_directional_hypothesis(
    conn: sqlite3.Connection | None, ticker: str
) -> tuple[EligibilityCheck, Literal["user_authored", "system_drafted", "missing"], str | None]:
    row = None
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT thesis, raw_json, ingested_at FROM thesis_state WHERE UPPER(ticker) = ?",
                (ticker,),
            ).fetchone()
        except sqlite3.Error:
            row = None
    if row is None or not row["thesis"]:
        return (
            EligibilityCheck(False, "no thesis on file (thesis_state: no row)"),
            "missing",
            None,
        )
    asof = str(row["ingested_at"]) if row["ingested_at"] else None
    if _is_corruption_stub(row["raw_json"]):
        return (
            EligibilityCheck(
                False, "thesis_state carries the corruption-recovery stub, not a real thesis"
            ),
            "missing",
            asof,
        )
    thesis = str(row["thesis"])
    if "STUB:" in thesis:
        return (
            EligibilityCheck(True, "system-drafted thesis on file (STUB: marker)"),
            "system_drafted",
            asof,
        )
    return EligibilityCheck(True, "user-authored thesis on file"), "user_authored", asof


# --------------------------------------------------------------------------- #
# Check 6 — >=1 disconfirmer
# --------------------------------------------------------------------------- #


def _read_qualitative_breakers(repo_root: Path, ticker: str) -> list[str]:
    """``thesis_breakers_qualitative`` off the raw holdings JSON — a field
    ``HoldingsSpec`` (Pydantic-validated for ``break_rules``/
    ``business_model_rules``/``break_rules_soft`` only) does not carry."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    items = cast("dict[str, object]", payload).get("thesis_breakers_qualitative")
    if not isinstance(items, list):
        return []
    return [str(x) for x in cast("list[object]", items) if str(x).strip()]


def _check_disconfirmers(repo_root: Path, ticker: str) -> tuple[EligibilityCheck, str | None]:
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    spec: HoldingsSpec | None
    try:
        spec = load_holdings_spec(holdings_dir, ticker)
    except FileNotFoundError:
        spec = None
    except (ValueError, ValidationError) as exc:
        return EligibilityCheck(False, f"holdings JSON unparsable/invalid: {exc}"), None
    if spec is not None and (spec.break_rules or spec.business_model_rules):
        n = len(spec.break_rules) + len(spec.business_model_rules)
        return EligibilityCheck(True, f"{n} hard disconfirming break rule(s) on file"), None
    qualitative = _read_qualitative_breakers(repo_root, ticker)
    if qualitative:
        return (
            EligibilityCheck(True, f"{len(qualitative)} qualitative-only disconfirmer(s) on file"),
            "qualitative-only disconfirmers",
        )
    return (
        EligibilityCheck(
            False, "no disconfirming condition on file (no break rules, no qualitative breakers)"
        ),
        None,
    )


# --------------------------------------------------------------------------- #
# Check 7 — portfolio context
# --------------------------------------------------------------------------- #


def _check_portfolio_context(
    *, weights_as_of: str | None, now: datetime
) -> tuple[EligibilityCheck, str | None]:
    if weights_as_of is None:
        return EligibilityCheck(False, "no materialized weights cache on file"), None
    dt = _parse_dt(weights_as_of)
    if dt is None:
        return (
            EligibilityCheck(False, f"weights cache computed_at unparseable ({weights_as_of!r})"),
            None,
        )
    age_hours = (now - dt).total_seconds() / 3600.0
    if age_hours <= WEIGHTS_CACHE_STALE_HOURS:
        return (
            EligibilityCheck(
                True, f"weights cache {age_hours:.1f}h old (<= {WEIGHTS_CACHE_STALE_HOURS:g}h)"
            ),
            None,
        )
    warning = f"portfolio weights cache is {age_hours:.1f}h old (> {WEIGHTS_CACHE_STALE_HOURS:g}h)"
    return EligibilityCheck(True, warning), warning


# --------------------------------------------------------------------------- #
# Check 8 — source provenance / as-of
# --------------------------------------------------------------------------- #


def _check_source_provenance(
    source_freshness: dict[str, str], *, list_type: str
) -> EligibilityCheck:
    required = _REQUIRED_SOURCES_BY_CLASS.get(list_type, ())
    if not required:
        return EligibilityCheck(True, "no required sources for this name class")
    missing = [k for k in required if not source_freshness.get(k)]
    if missing:
        return EligibilityCheck(
            False, f"missing as-of for required source(s): {', '.join(missing)}"
        )
    return EligibilityCheck(True, f"source as-of on file for: {', '.join(required)}")


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def cash_assessment(*, now: datetime | None = None) -> DecisionReadyAssessment:
    """Cash — always eligible (PRD §7.3), a synthetic row never run through
    the eight checks (each is stamped a trivially-true pass)."""
    now_dt = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
    trivial = EligibilityCheck(True, "cash is always eligible (PRD §7.3)")
    checks = dict.fromkeys(_ALL_CHECKS, trivial)
    payload = {"ticker": "CASH", "cash": True, "as_of": now_dt.isoformat()}
    return DecisionReadyAssessment(
        ticker="CASH",
        list_type="cash",
        eligible=True,
        blocking_reasons=(),
        warning_reasons=(),
        source_freshness={},
        # Cash carries no hypothesis at all — "missing" is the accurate literal
        # here even though eligible=True; this is the one place that pairing
        # is intentional (cash bypasses the gate by PRD fiat, not by passing
        # check 5).
        hypothesis_origin="missing",
        portfolio_fit_status="cash",
        checks=checks,
        input_sha=_sha(payload),
    )


def assess_eligibility(
    db_path: Path, repo_root: Path, ticker: str, *, list_type: ListType | str
) -> DecisionReadyAssessment:
    """The eight-check §7.3 read for one security. Never raises: a missing
    DB/cache/file degrades the checks that depend on it to a blocking
    failure, never a crash and never a silent pass."""
    upper = ticker.upper()
    lt = list_type.value if isinstance(list_type, ListType) else str(list_type)
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = _ro_conn(db_path)
    # Lazy to break the package-init cycle:
    # candidate_fit_cache -> allocation.candidate_fit -> allocation.__init__
    # -> allocation.eligibility. The cache is only needed by this I/O entrypoint.
    from candidate_fit_cache import read_materialized_candidate_fit, read_materialized_fit_meta

    checks: dict[str, EligibilityCheck] = {}
    blocking: list[str] = []
    warnings: list[str] = []
    source_freshness: dict[str, str] = {}

    dcf_row = _latest_dcf_row(conn, upper)

    price_check, price_asof = _check_price_freshness(dcf_row, now=now)
    checks[CHECK_PRICE_FRESHNESS] = price_check
    if price_asof:
        source_freshness["price"] = price_asof
    if not price_check.passed:
        blocking.append(price_check.reason)

    dcf_check, dcf_asof = _check_usable_dcf(dcf_row)
    checks[CHECK_USABLE_DCF] = dcf_check
    if dcf_asof:
        source_freshness["dcf"] = dcf_asof
    if not dcf_check.passed:
        blocking.append(dcf_check.reason)

    kpi_check, kpi_warning = _check_kpi_coverage(conn, upper, list_type=lt)
    checks[CHECK_KPI_COVERAGE] = kpi_check
    if not kpi_check.passed:
        blocking.append(kpi_check.reason)
    elif kpi_warning:
        warnings.append(kpi_warning)

    fit_cache = read_materialized_candidate_fit(repo_root)
    fit_meta = read_materialized_fit_meta(repo_root)
    fit_check, portfolio_fit_status, fit_asof = _check_candidate_fit(
        upper, list_type=lt, fit_cache=fit_cache, fit_meta=fit_meta
    )
    checks[CHECK_CANDIDATE_FIT] = fit_check
    if fit_asof:
        source_freshness["fit"] = fit_asof
    if not fit_check.passed:
        blocking.append(fit_check.reason)

    hyp_check, hypothesis_origin, thesis_asof = _check_directional_hypothesis(conn, upper)
    checks[CHECK_DIRECTIONAL_HYPOTHESIS] = hyp_check
    if thesis_asof:
        source_freshness["thesis"] = thesis_asof
    if not hyp_check.passed:
        blocking.append(hyp_check.reason)

    disc_check, disc_warning = _check_disconfirmers(repo_root, upper)
    checks[CHECK_DISCONFIRMERS] = disc_check
    if not disc_check.passed:
        blocking.append(disc_check.reason)
    elif disc_warning:
        warnings.append(disc_warning)

    weights_as_of = read_materialized_weights_as_of(repo_root)
    ctx_check, ctx_warning = _check_portfolio_context(weights_as_of=weights_as_of, now=now)
    checks[CHECK_PORTFOLIO_CONTEXT] = ctx_check
    if weights_as_of:
        source_freshness["weights"] = weights_as_of
    if not ctx_check.passed:
        blocking.append(ctx_check.reason)
    elif ctx_warning:
        warnings.append(ctx_warning)

    snapshot = read_latest_snapshot(db_path=db_path)
    if snapshot is not None and snapshot.captured_at:
        source_freshness["risk_snapshot"] = snapshot.captured_at

    prov_check = _check_source_provenance(source_freshness, list_type=lt)
    checks[CHECK_SOURCE_PROVENANCE] = prov_check
    if not prov_check.passed:
        blocking.append(prov_check.reason)

    if conn is not None:
        conn.close()

    payload = {
        "ticker": upper,
        "list_type": lt,
        "source_freshness": source_freshness,
        "checks": {k: {"passed": v.passed, "reason": v.reason} for k, v in checks.items()},
    }
    return DecisionReadyAssessment(
        ticker=upper,
        list_type=lt,
        eligible=not blocking,
        blocking_reasons=tuple(blocking),
        warning_reasons=tuple(warnings),
        source_freshness=source_freshness,
        hypothesis_origin=hypothesis_origin,
        portfolio_fit_status=portfolio_fit_status,
        checks=checks,
        input_sha=_sha(payload),
    )


def assess_universe(db_path: Path, repo_root: Path) -> dict[str, DecisionReadyAssessment]:
    """``ticker -> DecisionReadyAssessment`` for every active (non-archived)
    portfolio + evaluation name (``pipeline.queries.tracked_companies_for_user``,
    the PRD §7.3 eligible universe before the gate). ``{}`` on a missing/
    unreadable DB — never a crash."""
    conn = _ro_conn(db_path)
    if conn is None:
        return {}
    try:
        portfolio = tracked_companies_for_user(conn, list_types=frozenset({ListType.PORTFOLIO}))
        evaluation = tracked_companies_for_user(conn, list_types=frozenset({ListType.EVALUATION}))
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, DecisionReadyAssessment] = {}
    for company in (*portfolio, *evaluation):
        out[company.ticker.upper()] = assess_eligibility(
            db_path, repo_root, company.ticker, list_type=company.list_type
        )
    return out
