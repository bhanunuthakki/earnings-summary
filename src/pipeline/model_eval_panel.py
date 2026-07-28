"""Optimizer panel — the model-downgrade eval loop's dashboard surface (PR4).

``directives/model_eval_loop.md`` built the loop over three PRs (a cost-ranked
``model_ladder``, a standing ``model_eval_verdicts`` ledger + weekly sweep, and
an auto-switch ``model_pin_overrides`` table read by ``cli._model_for``). PR4 is
the surface that was planned but never built: until now the verdicts were only
queryable by hand. This panel reads the three tables the loop writes —
``model_eval_verdicts`` + ``model_pin_overrides`` + ``llm_calls`` — and answers
the four questions the optimizer can't otherwise show:

* **Anonymous-purpose alarm** — the call-health tripwire that would have caught
  the SayDo bypass (a ~$150/mo ``purpose=NULL`` line that ran ~5 weeks before
  #722). Any ``purpose IS NULL`` or unregistered-purpose ``llm_calls`` line with
  30d cost > $1 surfaces as a red flag instead of a silent 21%-of-spend leak.
* **Active overrides + realized savings** — every active ``model_pin_overrides``
  row with an estimate of what the downgrade saves: the incumbent-vs-override
  price delta applied to the purpose's real 30d production token volume
  (``model_ladder.estimated_call_usd``), rolled up to "≈$X/mo saved by N
  downgrades".
* **Verdict history** — per (purpose, candidate) the recent sweep verdicts as
  chips. ``CANDIDATE_ERRORED`` (the #723 infra verdict) is rendered as a RED
  INFRA FLAG, deliberately NOT on the quality-pill scale — a Gemini candidate
  erroring 60-100% of its runs is an infrastructure failure, not "the candidate
  lost on quality", and must not read as one.
* **Per-purpose 30d cost** — the ``llm_calls`` spend per purpose, split into
  production traffic vs the eval machinery itself (``scope`` in
  ``model_eval``/``backend_judge``/``eval``), so a purpose's real cost line and
  the cost of measuring it are legible separately.

Pure DB reads (the loop only ever WRITES through the sweep cron +
``apply_model_switches``); a missing table degrades to an explainer, matching
the evals panel it sits beside in the System → Provenance console.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import cast

from llm.eval_scopes import EVAL_SCOPES
from llm.model_ladder import estimated_call_usd, model_rank
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The eval-machinery scopes — traffic these tables account for that is NOT real
# production spend. Unified onto the canonical llm.eval_scopes.EVAL_SCOPES
# (meta_eval_governance.md §10.2 — this panel's local copy predated it); sorted
# for stable SQL placeholder ordering. A NULL scope is production.
_EVAL_SCOPES: tuple[str, ...] = tuple(sorted(EVAL_SCOPES))

WINDOW_DAYS = 30
# The anonymous-purpose alarm floor: a NULL/unregistered purpose only alarms
# once it has cost real money (the SayDo bypass was ~$150/mo; $1/30d is the
# noise floor below which an ad-hoc one-off call isn't worth a red flag).
ANON_COST_FLOOR_USD = 1.0
_HISTORY_PER_CANDIDATE = 6
_COST_MAX_ROWS = 60

# The #723 infra verdict — checked FIRST in decide_switch, excluded from the
# switch/keep streaks. Rendered as an infra flag here, never a quality pill.
CANDIDATE_ERRORED = "CANDIDATE_ERRORED"

# Quality verdict → (short label, kit pill tone). SWITCH_DOWN is the win (a
# cheaper model held), HOLD is the mixed/disagree amber, KEEP/INSUFFICIENT are
# neutral (untoned) pills. CANDIDATE_ERRORED is deliberately absent — it is not
# a quality outcome and takes the infra-flag path.
_VERDICT_TONE: dict[str, tuple[str, str]] = {
    "SWITCH_DOWN": ("switch ↓", "ok"),
    "HOLD": ("hold", "warn"),
    "KEEP_INCUMBENT": ("keep", ""),
    "INSUFFICIENT_DATA": ("insufficient", ""),
}
# The mini-history letter per verdict (most-recent-first strip in the chip title).
_VERDICT_LETTER: dict[str, str] = {
    "SWITCH_DOWN": "S",
    "KEEP_INCUMBENT": "K",
    "HOLD": "H",
    "INSUFFICIENT_DATA": "I",
    CANDIDATE_ERRORED: "E",
}


@dataclass(slots=True)
class PurposeCostRow:
    """Per-purpose 30d ``llm_calls`` spend, split production vs eval-machinery."""

    purpose: str
    prod_cost_usd: float
    eval_cost_usd: float
    prod_calls: int
    eval_calls: int

    @property
    def total_cost_usd(self) -> float:
        return self.prod_cost_usd + self.eval_cost_usd

    @property
    def total_calls(self) -> int:
        return self.prod_calls + self.eval_calls


@dataclass(slots=True)
class VerdictRow:
    """One ``model_eval_verdicts`` row (a single sweep's CandidateVerdict)."""

    verdict: str
    recorded_at: str
    parity_rate: float | None
    judge_agreement: float | None
    n_cases: int | None


@dataclass(slots=True)
class CandidateHistory:
    """Rolling verdict history for one (purpose, candidate) pair."""

    purpose: str
    candidate: str
    incumbent: str
    rows: list[VerdictRow] = field(default_factory=list[VerdictRow])

    @property
    def latest(self) -> VerdictRow:
        return self.rows[0]

    @property
    def errored(self) -> bool:
        """Latest verdict is the #723 infra verdict — an infra flag, not a
        quality result."""
        return self.latest.verdict == CANDIDATE_ERRORED


@dataclass(slots=True)
class OverrideRow:
    """One active ``model_pin_overrides`` row + its realized-savings estimate."""

    purpose: str
    model: str  # the override (cheaper) model now serving the purpose
    incumbent: str | None  # the code-pin it displaced (from the latest verdict)
    set_by: str
    set_at: str
    prod_input_tokens: int
    prod_output_tokens: int

    @property
    def monthly_savings_usd(self) -> float | None:
        """Incumbent-vs-override price delta over the purpose's real 30d
        production token volume. ``None`` when either model is off the ladder
        (cost unknown, not zero) or there's no production volume to price."""
        if self.incumbent is None:
            return None
        if model_rank(self.incumbent) is None or model_rank(self.model) is None:
            return None
        if self.prod_input_tokens <= 0 and self.prod_output_tokens <= 0:
            return None
        incumbent_cost = estimated_call_usd(
            self.incumbent, self.prod_input_tokens, self.prod_output_tokens
        )
        override_cost = estimated_call_usd(
            self.model, self.prod_input_tokens, self.prod_output_tokens
        )
        return incumbent_cost - override_cost


@dataclass(slots=True)
class AnonCostRow:
    """A ``purpose IS NULL`` or unregistered-purpose 30d spend line — the
    anonymous-purpose alarm's evidence."""

    purpose: str | None  # None == the NULL-purpose group
    cost_usd: float
    calls: int

    @property
    def is_null(self) -> bool:
        return self.purpose is None


# Freshness thresholds (meta_eval_governance.md PR6): a steering loop that
# stops steering must be a red flag, not a silent decay.
NOMINATION_STALE_DAYS = 45
SWEEP_STALE_DAYS = 14


@dataclass(slots=True)
class NominationRow:
    """One pending optimizer nomination (the sweep's steering feed)."""

    purpose: str
    kind: str
    priority: int
    risk_tier: str
    source: str
    candidates: list[str] = field(default_factory=list[str])
    rationale: str = ""
    expires_at: str | None = None


@dataclass(slots=True)
class SteeringHealth:
    """The loop's freshness posture + meta-machinery cost."""

    newest_nomination_at: str | None
    newest_verdict_at: str | None
    nomination_stale: bool
    sweep_stale: bool
    meta_cost_30d_usd: float
    meta_calls_30d: int
    active_candidate_models: int
    insufficient_frame_purposes: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class ExperimentRow:
    """One prompt-A/B experiment + its promotion posture (§4/§10 Q1)."""

    experiment_id: str
    purpose: str
    status: str
    hypothesis: str
    n_edits: int
    latest_recommendation: str | None
    runs: int
    override_active: bool


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _window_start(window_days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=window_days)).replace(tzinfo=None).isoformat()


def _eval_scope_sql() -> str:
    """The ``scope IN (?,?,?)`` placeholder list for the eval-machinery scopes."""
    return ",".join("?" * len(_EVAL_SCOPES))


def load_purpose_costs(
    conn: sqlite3.Connection, *, window_days: int = WINDOW_DAYS
) -> list[PurposeCostRow]:
    """Per-purpose 30d spend split production vs eval-machinery (a NULL scope is
    production). Highest total cost first — the fat lines lead."""
    if not _has_table(conn, "llm_calls"):
        return []
    since = _window_start(window_days)
    scopes = _eval_scope_sql()
    rows = conn.execute(
        f"""
        SELECT purpose,
               COALESCE(SUM(CASE WHEN scope IN ({scopes})
                    THEN cost_estimate_usd ELSE 0 END), 0.0) AS eval_cost,
               COALESCE(SUM(CASE WHEN scope IN ({scopes})
                    THEN 0 ELSE cost_estimate_usd END), 0.0) AS prod_cost,
               SUM(CASE WHEN scope IN ({scopes}) THEN 1 ELSE 0 END) AS eval_calls,
               SUM(CASE WHEN scope IN ({scopes}) THEN 0 ELSE 1 END) AS prod_calls
        FROM llm_calls
        WHERE purpose IS NOT NULL AND called_at >= ?
        GROUP BY purpose
        """,
        (*_EVAL_SCOPES, *_EVAL_SCOPES, *_EVAL_SCOPES, *_EVAL_SCOPES, since),
    ).fetchall()
    out = [
        PurposeCostRow(
            purpose=str(r["purpose"]),
            prod_cost_usd=float(r["prod_cost"] or 0.0),
            eval_cost_usd=float(r["eval_cost"] or 0.0),
            prod_calls=int(r["prod_calls"] or 0),
            eval_calls=int(r["eval_calls"] or 0),
        )
        for r in rows
    ]
    out.sort(key=lambda c: (-c.total_cost_usd, c.purpose))
    return out[:_COST_MAX_ROWS]


def load_candidate_histories(conn: sqlite3.Connection) -> list[CandidateHistory]:
    """Rolling verdict history per (purpose, candidate), most-recent verdict
    first within each pair; pairs ordered by purpose then candidate."""
    if not _has_table(conn, "model_eval_verdicts"):
        return []
    rows = conn.execute(
        """
        SELECT purpose, candidate, incumbent, verdict, recorded_at,
               parity_rate, judge_agreement, n_cases
        FROM model_eval_verdicts
        ORDER BY purpose, candidate, recorded_at DESC, id DESC
        """
    ).fetchall()
    grouped: dict[tuple[str, str], CandidateHistory] = {}
    for r in rows:
        key = (str(r["purpose"]), str(r["candidate"]))
        hist = grouped.get(key)
        if hist is None:
            hist = CandidateHistory(purpose=key[0], candidate=key[1], incumbent=str(r["incumbent"]))
            grouped[key] = hist
        if len(hist.rows) < _HISTORY_PER_CANDIDATE:
            hist.rows.append(
                VerdictRow(
                    verdict=str(r["verdict"]),
                    recorded_at=str(r["recorded_at"])[:16].replace("T", " "),
                    parity_rate=(float(r["parity_rate"]) if r["parity_rate"] is not None else None),
                    judge_agreement=(
                        float(r["judge_agreement"]) if r["judge_agreement"] is not None else None
                    ),
                    n_cases=int(r["n_cases"]) if r["n_cases"] is not None else None,
                )
            )
    return list(grouped.values())


def load_active_overrides(
    conn: sqlite3.Connection, *, window_days: int = WINDOW_DAYS
) -> list[OverrideRow]:
    """Active model-pin overrides + the 30d production token volume for each
    purpose (for the realized-savings estimate). The incumbent is read from the
    most recent verdict for (purpose, candidate) — the same source
    ``apply_model_switches`` uses on revert."""
    if not _has_table(conn, "model_pin_overrides"):
        return []
    since = _window_start(window_days)
    scopes = _eval_scope_sql()
    has_verdicts = _has_table(conn, "model_eval_verdicts")
    has_calls = _has_table(conn, "llm_calls")
    rows = conn.execute(
        """
        SELECT purpose, model, set_by, set_at
        FROM model_pin_overrides
        WHERE active = 1
        ORDER BY set_at DESC, id DESC
        """
    ).fetchall()
    out: list[OverrideRow] = []
    for r in rows:
        purpose, model = str(r["purpose"]), str(r["model"])
        incumbent: str | None = None
        if has_verdicts:
            inc = conn.execute(
                """
                SELECT incumbent FROM model_eval_verdicts
                WHERE purpose = ? AND candidate = ?
                ORDER BY recorded_at DESC, id DESC LIMIT 1
                """,
                (purpose, model),
            ).fetchone()
            incumbent = str(inc["incumbent"]) if inc else None
        in_tok, out_tok = 0, 0
        if has_calls:
            tok = conn.execute(
                f"""
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM llm_calls
                WHERE purpose = ? AND called_at >= ?
                  AND (scope IS NULL OR scope NOT IN ({scopes}))
                """,
                (purpose, since, *_EVAL_SCOPES),
            ).fetchone()
            in_tok, out_tok = int(tok[0] or 0), int(tok[1] or 0)
        out.append(
            OverrideRow(
                purpose=purpose,
                model=model,
                incumbent=incumbent,
                set_by=str(r["set_by"]),
                set_at=str(r["set_at"])[:16].replace("T", " "),
                prod_input_tokens=in_tok,
                prod_output_tokens=out_tok,
            )
        )
    return out


def _known_purposes() -> frozenset[str]:
    """The declared purpose universe for the anonymous-purpose alarm — the same
    set ``evals.coverage`` treats as known: every code-pinned + prompt-version-
    registered purpose plus the eval machinery's own (judge/grader) purposes. A
    purpose outside this set (and not a dynamic ``lens:*``) is "unregistered"."""
    from evals.coverage import META_PURPOSES
    from llm.cli import LLM_MODELS
    from llm.prompt_versions import registered_purposes

    return frozenset(set(LLM_MODELS) | set(registered_purposes()) | set(META_PURPOSES))


def load_anon_costs(
    conn: sqlite3.Connection,
    *,
    window_days: int = WINDOW_DAYS,
    min_cost_usd: float = ANON_COST_FLOOR_USD,
) -> list[AnonCostRow]:
    """The anonymous-purpose alarm: 30d ``llm_calls`` lines that are either
    ``purpose IS NULL`` or an unregistered purpose, with cost above the floor.
    Dynamic ``lens:*`` purposes are a known family (one shared generator) and
    never alarm. Costliest first."""
    if not _has_table(conn, "llm_calls"):
        return []
    since = _window_start(window_days)
    rows = conn.execute(
        """
        SELECT purpose, COALESCE(SUM(cost_estimate_usd), 0.0) AS cost, COUNT(*) AS n
        FROM llm_calls
        WHERE called_at >= ?
        GROUP BY purpose
        """,
        (since,),
    ).fetchall()
    known = _known_purposes()
    out: list[AnonCostRow] = []
    for r in rows:
        purpose = r["purpose"]
        cost = float(r["cost"] or 0.0)
        if cost <= min_cost_usd:
            continue
        if purpose is None:
            out.append(AnonCostRow(purpose=None, cost_usd=cost, calls=int(r["n"] or 0)))
            continue
        p = str(purpose)
        if p.startswith("lens:") or p in known:
            continue
        out.append(AnonCostRow(purpose=p, cost_usd=cost, calls=int(r["n"] or 0)))
    out.sort(key=lambda a: -a.cost_usd)
    return out


def load_pending_nominations(conn: sqlite3.Connection) -> list[NominationRow]:
    """The sweep's pending steering feed (priority order)."""
    if not _has_table(conn, "optimizer_nominations"):
        return []
    rows = conn.execute(
        """
        SELECT purpose, kind, priority, risk_tier, source, candidates_json,
               rationale, expires_at
        FROM optimizer_nominations
        WHERE status = 'pending'
        ORDER BY priority, id
        """
    ).fetchall()
    out: list[NominationRow] = []
    for r in rows:
        try:
            cands_obj: object = json.loads(str(r["candidates_json"] or "[]"))
        except json.JSONDecodeError:
            cands_obj = []
        cands = (
            [c for c in cast("list[object]", cands_obj) if isinstance(c, str)]
            if isinstance(cands_obj, list)
            else []
        )
        out.append(
            NominationRow(
                purpose=str(r["purpose"]),
                kind=str(r["kind"]),
                priority=int(r["priority"]),
                risk_tier=str(r["risk_tier"]),
                source=str(r["source"]),
                candidates=cands,
                rationale=str(r["rationale"] or ""),
                expires_at=str(r["expires_at"]) if r["expires_at"] is not None else None,
            )
        )
    return out


def load_steering_health(
    conn: sqlite3.Connection, *, window_days: int = WINDOW_DAYS
) -> SteeringHealth:
    """Freshness + meta-cost rollup: the loop watching itself (PR6)."""
    newest_nom: str | None = None
    if _has_table(conn, "optimizer_nominations"):
        row = conn.execute("SELECT MAX(created_at) FROM optimizer_nominations").fetchone()
        newest_nom = str(row[0]) if row and row[0] is not None else None
    newest_verdict: str | None = None
    insufficient: list[str] = []
    if _has_table(conn, "model_eval_verdicts"):
        row = conn.execute("SELECT MAX(recorded_at) FROM model_eval_verdicts").fetchone()
        newest_verdict = str(row[0]) if row and row[0] is not None else None
        # Purposes whose NEWEST verdict is the INSUFFICIENT_FRAME honesty label —
        # they need harvest, not judgement.
        rows = conn.execute(
            """
            SELECT purpose, verdict FROM model_eval_verdicts m
            WHERE recorded_at = (
                SELECT MAX(recorded_at) FROM model_eval_verdicts
                WHERE purpose = m.purpose
            )
            GROUP BY purpose
            """
        ).fetchall()
        insufficient = sorted(
            {str(r["purpose"]) for r in rows if str(r["verdict"]) == "INSUFFICIENT_FRAME"}
        )
    meta_cost = 0.0
    meta_calls = 0
    if _has_table(conn, "llm_calls"):
        scopes = _eval_scope_sql()
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(cost_estimate_usd), 0.0), COUNT(*)
            FROM llm_calls
            WHERE called_at >= ? AND scope IN ({scopes})
            """,
            (_window_start(window_days), *_EVAL_SCOPES),
        ).fetchone()
        meta_cost = float(row[0] or 0.0)
        meta_calls = int(row[1] or 0)
    candidates = 0
    if _has_table(conn, "candidate_models"):
        row = conn.execute(
            "SELECT COUNT(*) FROM candidate_models WHERE status = 'active'"
        ).fetchone()
        candidates = int(row[0] or 0)

    def _stale(stamp: str | None, days: int) -> bool:
        if stamp is None:
            return True
        try:
            age = datetime.now(UTC).replace(tzinfo=None) - datetime.fromisoformat(stamp)
        except ValueError:
            return True
        return age > timedelta(days=days)

    return SteeringHealth(
        newest_nomination_at=newest_nom,
        newest_verdict_at=newest_verdict,
        nomination_stale=_stale(newest_nom, NOMINATION_STALE_DAYS),
        sweep_stale=_stale(newest_verdict, SWEEP_STALE_DAYS),
        meta_cost_30d_usd=meta_cost,
        meta_calls_30d=meta_calls,
        active_candidate_models=candidates,
        insufficient_frame_purposes=insufficient,
    )


def load_experiments(conn: sqlite3.Connection, *, limit: int = 12) -> list[ExperimentRow]:
    """Prompt-A/B experiments + whether each drives an ACTIVE prompt override."""
    if not _has_table(conn, "prompt_experiments"):
        return []
    has_verdicts = _has_table(conn, "prompt_ab_verdicts")
    has_overrides = _has_table(conn, "prompt_pin_overrides")
    rows = conn.execute(
        "SELECT experiment_id, purpose, status, hypothesis, edits_json "
        "FROM prompt_experiments ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[ExperimentRow] = []
    for r in rows:
        experiment_id = str(r["experiment_id"])
        latest: str | None = None
        runs = 0
        if has_verdicts:
            v = conn.execute(
                "SELECT recommendation, COUNT(*) OVER () FROM prompt_ab_verdicts "
                "WHERE experiment_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
            if v is not None:
                latest = str(v[0])
                runs = int(v[1] or 0)
        override_active = False
        if has_overrides:
            o = conn.execute(
                "SELECT 1 FROM prompt_pin_overrides WHERE experiment_id = ? AND active = 1",
                (experiment_id,),
            ).fetchone()
            override_active = o is not None
        try:
            edits_obj: object = json.loads(str(r["edits_json"] or "[]"))
        except json.JSONDecodeError:
            edits_obj = []
        out.append(
            ExperimentRow(
                experiment_id=experiment_id,
                purpose=str(r["purpose"]),
                status=str(r["status"]),
                hypothesis=str(r["hypothesis"] or ""),
                n_edits=len(cast("list[object]", edits_obj)) if isinstance(edits_obj, list) else 0,
                latest_recommendation=latest,
                runs=runs,
                override_active=override_active,
            )
        )
    return out


def render_model_eval_panel(db_path: Path) -> str:
    """The Optimizer panel fragment (System → Provenance console). Pure DB reads
    — the loop's writes happen only through the sweep cron + apply_model_switches."""
    costs: list[PurposeCostRow] = []
    histories: list[CandidateHistory] = []
    overrides: list[OverrideRow] = []
    anon: list[AnonCostRow] = []
    nominations: list[NominationRow] = []
    health: SteeringHealth | None = None
    experiments: list[ExperimentRow] = []
    if db_path.exists():
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            costs = load_purpose_costs(conn)
            histories = load_candidate_histories(conn)
            overrides = load_active_overrides(conn)
            anon = load_anon_costs(conn)
            nominations = load_pending_nominations(conn)
            health = load_steering_health(conn)
            experiments = load_experiments(conn)
        finally:
            conn.close()
    return compose_model_eval_page(
        anon,
        overrides,
        histories,
        costs,
        nominations=nominations,
        health=health,
        experiments=experiments,
    )


def compose_model_eval_page(
    anon: list[AnonCostRow],
    overrides: list[OverrideRow],
    histories: list[CandidateHistory],
    costs: list[PurposeCostRow],
    *,
    nominations: list[NominationRow] | None = None,
    health: SteeringHealth | None = None,
    experiments: list[ExperimentRow] | None = None,
) -> str:
    """Pure page assembly (testable without a DB)."""
    return "".join(
        [
            _PANEL_CSS,
            _alarm_section(anon),
            _steering_section(health, nominations or []),
            _experiments_section(experiments or []),
            _overrides_section(overrides),
            _verdicts_section(histories),
            _costs_section(costs),
        ]
    )


def _usd(amount: float, *, places: int = 2) -> str:
    return f"${amount:.{places}f}"


def _alarm_section(anon: list[AnonCostRow]) -> str:
    head = (
        '<section class="panel"><h2>Anonymous-purpose alarm</h2>'
        '<p class="sub">Any <code>purpose=NULL</code> or unregistered-purpose LLM spend over '
        f"{_usd(ANON_COST_FLOOR_USD, places=0)}/30d — the tripwire for an ungoverned call. "
        "A <code>purpose=NULL</code> bypass ran ~21% of monthly cost for ~5 weeks before it "
        "was noticed (the SayDo leak); this makes that leak a red flag, not a surprise.</p>"
    )
    if not anon:
        return (
            f"{head}"
            '<div class="k-well k-well-ok me-alarm">'
            '<span class="k-prov-sev"><span class="k-dot k-dot-ok"></span>'
            '<span class="k-label">clear</span></span>'
            "<span>No anonymous or unregistered LLM spend over "
            f"{_usd(ANON_COST_FLOOR_USD, places=0)} in the last {WINDOW_DAYS} days.</span>"
            "</div></section>"
        )
    chips = "".join(
        '<span class="k-chip k-chip-bad me-alarm-chip" '
        f'title="{escape(row.purpose) if row.purpose else "purpose is NULL"} — '
        f'{row.calls} calls">'
        f"{'purpose=NULL' if row.is_null else escape(row.purpose or '')}"
        f"<span class='me-alarm-cost'>{_usd(row.cost_usd)}</span></span>"
        for row in anon
    )
    total = sum(a.cost_usd for a in anon)
    n = len(anon)
    return (
        f"{head}"
        '<div class="k-well k-well-bad me-alarm">'
        '<span class="k-prov-sev"><span class="k-dot k-dot-bad"></span>'
        '<span class="k-label">alarm</span></span>'
        f"<span>{n} anonymous/unregistered line{'s' if n != 1 else ''} · "
        f"{_usd(total)}/30d ungoverned</span></div>"
        f'<div class="me-alarm-chips">{chips}</div>'
        "</section>"
    )


def _steering_section(health: SteeringHealth | None, nominations: list[NominationRow]) -> str:
    """The self-steering loop's posture: freshness, meta-cost, pending feed."""
    head = (
        '<section class="panel"><h2>Optimizer steering</h2>'
        '<p class="sub">The monthly nominator + frontier research feed the sweep '
        "(meta_eval_governance.md §1/§10.1). A steering loop that stops steering is a "
        "red flag, not a silent decay.</p>"
    )
    if health is None:
        return f'{head}<p class="muted">No DB — steering state unavailable.</p></section>'
    chips: list[str] = []
    if health.nomination_stale:
        chips.append(
            f'<span class="k-chip k-chip-bad">no nomination run in {NOMINATION_STALE_DAYS}d</span>'
        )
    if health.sweep_stale:
        chips.append(
            f'<span class="k-chip k-chip-bad">no sweep verdict in {SWEEP_STALE_DAYS}d</span>'
        )
    if not chips:
        chips.append('<span class="k-pill k-pill-ok">fresh</span>')
    for purpose in health.insufficient_frame_purposes:
        chips.append(
            f'<span class="k-chip me-frame-chip" title="latest verdict INSUFFICIENT_FRAME — '
            f'needs harvest, not judgement">{escape(purpose)}: thin frame</span>'
        )
    facts = (
        f"meta-machinery {_usd(health.meta_cost_30d_usd)}/30d · {health.meta_calls_30d} calls · "
        f"{health.active_candidate_models} discovered candidate model(s) · "
        f"newest nomination {escape((health.newest_nomination_at or 'never')[:16])} · "
        f"newest verdict {escape((health.newest_verdict_at or 'never')[:16])}"
    )
    if not nominations:
        body = (
            '<p class="muted">No pending nominations — the sweep runs on discovery. '
            "The weekly step 0 re-nominates monthly or when the frontier changes.</p>"
        )
    else:
        rows_html = "".join(
            "<tr>"
            f'<td class="num">{n.priority}</td>'
            f'<td class="me-purpose">{escape(n.purpose)}</td>'
            f"<td><span class='k-chip'>{escape(n.kind)}</span> "
            f"<span class='k-chip{' k-chip-bad' if n.risk_tier == 'risky' else ''}'>"
            f"{escape(n.risk_tier)}</span></td>"
            f'<td class="me-loc">{escape(", ".join(n.candidates) or "—")}</td>'
            f'<td class="muted" title="{escape(n.rationale)}">'
            f"{escape(n.rationale[:80])}{'…' if len(n.rationale) > 80 else ''}</td>"
            f'<td class="me-loc muted">{escape(n.source)}</td>'
            "</tr>"
            for n in nominations
        )
        body = (
            '<table class="p-table"><thead><tr><th class="num">#</th><th>Purpose</th>'
            "<th>Kind · tier</th><th>Candidates</th><th>Why</th><th>Source</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table>"
        )
    return (
        f"{head}"
        f'<div class="me-alarm-chips">{"".join(chips)}</div>'
        f'<p class="me-rollup muted">{facts}</p>'
        f"{body}</section>"
    )


def _experiments_section(experiments: list[ExperimentRow]) -> str:
    """Prompt-A/B experiments + the Q1 auto-apply posture."""
    head = (
        '<section class="panel"><h2>Prompt experiments (A/B)</h2>'
        '<p class="sub">Deterministic edit-splice variants judged brand-blind against the '
        "baseline (§4). A promoted experiment drives an ACTIVE <code>prompt_pin_overrides</code> "
        "row applied to production traffic (owner decision Q1) — reversible, auto-demoted on a "
        "KEEP_BASELINE run; <code>reason_json.edits</code> is the git-reconciliation diff.</p>"
    )
    if not experiments:
        return (
            f'{head}<p class="muted">No experiments yet — propose one with '
            "<code>execution/run_prompt_ab.py --propose</code> or wait for a "
            "<code>prompt_experiment</code> nomination.</p></section>"
        )
    live_pill = '<span class="k-pill k-pill-ok">override live</span>'
    rows_html = "".join(
        "<tr>"
        f'<td class="me-loc">{escape(e.experiment_id[:8])}</td>'
        f'<td class="me-purpose">{escape(e.purpose)}</td>'
        f"<td><span class='k-chip'>{escape(e.status)}</span>"
        f"{live_pill if e.override_active else ''}"
        "</td>"
        f'<td>{escape(e.latest_recommendation or "—")}<span class="muted"> · {e.runs} run(s)'
        f" · {e.n_edits} edit(s)</span></td>"
        f'<td class="muted" title="{escape(e.hypothesis)}">'
        f"{escape(e.hypothesis[:70])}{'…' if len(e.hypothesis) > 70 else ''}</td>"
        "</tr>"
        for e in experiments
    )
    return (
        f"{head}"
        '<table class="p-table"><thead><tr><th>Id</th><th>Purpose</th><th>Status</th>'
        "<th>Latest verdict</th><th>Hypothesis</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table></section>"
    )


def _overrides_section(overrides: list[OverrideRow]) -> str:
    head = (
        '<section class="panel"><h2>Active model-pin overrides</h2>'
        '<p class="sub">Purposes the loop switched down to a cheaper model (reversible, '
        "DB-backed — read by <code>cli._model_for</code> before the code pin). Realized "
        "savings = the incumbent-vs-override price delta applied to the purpose's real 30d "
        "production token volume.</p>"
    )
    if not overrides:
        return (
            f"{head}"
            '<p class="muted">No active overrides — every purpose is on its code pin. '
            "The weekly sweep writes one here when a cheaper model clears the switch bar "
            "(<code>execution/apply_model_switches.py</code>).</p></section>"
        )
    priced = [o.monthly_savings_usd for o in overrides if o.monthly_savings_usd is not None]
    rollup_total = sum(priced)
    rollup = (
        f'<p class="me-rollup">≈ <strong>{_usd(rollup_total)}/mo</strong> saved by '
        f"{len(overrides)} downgrade{'s' if len(overrides) != 1 else ''}"
        f"{'' if len(priced) == len(overrides) else f' ({len(overrides) - len(priced)} unpriced)'}"
        ".</p>"
    )
    rows_html: list[str] = []
    for o in overrides:
        savings = o.monthly_savings_usd
        savings_cell = (
            f'<span class="me-savings">{_usd(savings)}/mo</span>'
            if savings is not None
            else '<span class="muted">—</span>'
        )
        incumbent = escape(o.incumbent) if o.incumbent else '<span class="muted">unknown</span>'
        set_by_tone = "k-chip-accent" if o.set_by.startswith("auto:") else ""
        rows_html.append(
            "<tr>"
            f'<td class="me-purpose">{escape(o.purpose)}</td>'
            f'<td class="me-loc">{incumbent} <span class="muted">→</span> '
            f"{escape(o.model)}</td>"
            f"<td>{savings_cell}</td>"
            f'<td class="num me-loc muted">{o.prod_input_tokens + o.prod_output_tokens:,}</td>'
            f'<td><span class="k-chip {set_by_tone}">{escape(o.set_by)}</span></td>'
            f'<td class="me-loc muted">{escape(o.set_at)}</td>'
            "</tr>"
        )
    return (
        f"{head}{rollup}"
        '<table class="p-table"><thead><tr>'
        "<th>Purpose</th><th>Incumbent → override</th><th>Est. savings</th>"
        '<th class="num">30d prod tokens</th><th>Set by</th><th>Since</th>'
        "</tr></thead><tbody>"
        f"{''.join(rows_html)}</tbody></table></section>"
    )


def _verdict_marker(row: VerdictRow) -> str:
    """One verdict as a status marker. Quality verdicts ride the kit's filled
    ``.k-pill`` (tone by outcome); CANDIDATE_ERRORED is an OUTLINE red infra
    chip — deliberately a different shape from the quality scale, so a candidate
    that erred operationally never reads as one that lost on quality (#723)."""
    if row.verdict == CANDIDATE_ERRORED:
        return '<span class="k-chip k-chip-bad me-infra">⚠ infra err</span>'
    label, tone = _VERDICT_TONE.get(row.verdict, (row.verdict.lower(), ""))
    tone_cls = f" k-pill-{tone}" if tone else ""
    return f'<span class="k-pill{tone_cls}">{escape(label)}</span>'


def _candidate_chip(hist: CandidateHistory) -> str:
    latest = hist.latest
    parts: list[str] = []
    if latest.parity_rate is not None:
        parts.append(f"parity {latest.parity_rate * 100:.0f}%")
    if latest.judge_agreement is not None:
        parts.append(f"agreement {latest.judge_agreement:.2f}")
    if latest.n_cases is not None:
        parts.append(f"n={latest.n_cases}")
    parts.append(f"latest {latest.recorded_at}")
    history_letters = " ".join(_VERDICT_LETTER.get(r.verdict, "?") for r in hist.rows)
    parts.append(f"history {history_letters}")
    title = escape(" · ".join(parts))
    n = len(hist.rows)
    count = f'<span class="muted">&times;{n}</span>' if n > 1 else ""
    return (
        f'<span class="me-cand" title="{title}">'
        f'<span class="me-loc">{escape(hist.candidate)}</span>'
        f"{_verdict_marker(latest)}{count}</span>"
    )


def _verdicts_section(histories: list[CandidateHistory]) -> str:
    head = (
        '<section class="panel"><h2>Downgrade verdicts</h2>'
        '<p class="sub">The brand-blind pairwise judge\'s recent verdict per (purpose, '
        "candidate) from the weekly sweep. <code>switch ↓</code> = a cheaper model held "
        "parity; <code>keep</code> = incumbent won; <code>hold</code> = judges split. "
        "<code>⚠ infra err</code> is an <em>infrastructure</em> flag (the candidate errored "
        "on ≥ 50% of cases) — not a quality result.</p>"
    )
    if not histories:
        return (
            f"{head}"
            '<p class="muted">No sweep verdicts recorded yet — the weekly '
            "<code>run_weekly_model_eval.py</code> rung populates "
            "<code>model_eval_verdicts</code>.</p></section>"
        )
    by_purpose: dict[str, list[CandidateHistory]] = {}
    for h in histories:
        by_purpose.setdefault(h.purpose, []).append(h)
    rows: list[str] = []
    for purpose in sorted(by_purpose):
        cands = sorted(by_purpose[purpose], key=lambda h: h.candidate)
        incumbent = cands[0].incumbent
        chips = "".join(_candidate_chip(h) for h in cands)
        rows.append(
            f'<tr><td class="me-purpose">{escape(purpose)}'
            f'<span class="muted me-vs"> vs {escape(incumbent)}</span></td>'
            f"<td>{chips}</td></tr>"
        )
    return f'{head}<table class="me-verdicts"><tbody>{"".join(rows)}</tbody></table></section>'


def _costs_section(costs: list[PurposeCostRow]) -> str:
    head = (
        '<section class="panel"><h2>Per-purpose cost (30d)</h2>'
        '<p class="sub">Spend per purpose over the llm_calls ledger, split into production '
        "traffic and the eval machinery that measures it (the model-eval sweep, the pairwise "
        "judge, the golden/rubric harness). The fat production lines are the optimizer's "
        "candidates for a downgrade sweep.</p>"
    )
    if not costs:
        return f'{head}<p class="muted">No LLM calls in the window.</p></section>'
    rows = "".join(
        "<tr>"
        f'<td class="me-purpose">{escape(c.purpose)}</td>'
        f'<td class="num">{_usd(c.prod_cost_usd)}'
        f"<span class='muted'> · {c.prod_calls}</span></td>"
        f'<td class="num muted">{_usd(c.eval_cost_usd)}'
        f"<span class='muted'> · {c.eval_calls}</span></td>"
        f'<td class="num me-total">{_usd(c.total_cost_usd)}</td>'
        "</tr>"
        for c in costs
    )
    total_prod = sum(c.prod_cost_usd for c in costs)
    total_eval = sum(c.eval_cost_usd for c in costs)
    return (
        f"{head}"
        '<table class="p-table"><thead><tr>'
        '<th>Purpose</th><th class="num">Prod $ · calls</th>'
        '<th class="num">Eval $ · calls</th><th class="num">Total</th>'
        "</tr></thead><tbody>"
        f"{rows}"
        '<tr class="me-cost-total"><td class="me-purpose">all purposes</td>'
        f'<td class="num">{_usd(total_prod)}</td>'
        f'<td class="num muted">{_usd(total_eval)}</td>'
        f'<td class="num me-total">{_usd(total_prod + total_eval)}</td></tr>'
        "</tbody></table></section>"
    )


# Token-clean local CSS. All status color rides the kit (.k-pill / .k-chip /
# .k-well from controls.py); this carries only layout + the mono locator / sans
# label distinctions + a couple of table tweaks, on the type/space/color tokens.
_PANEL_CSS = """<style>
.me-alarm { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.me-alarm-chips { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.me-alarm-chip { gap: var(--sp-2); }
.me-alarm-cost { font-family: var(--mono); }
.me-rollup { font-size: var(--fs-body); margin: 0 0 var(--sp-3); }
.me-savings { font-family: var(--mono); color: var(--ok); font-weight: 600; }
/* Purpose names are sans labels (NOT tickers): emphasis without mono. */
.me-purpose { font-weight: 600; }
.me-vs { font-weight: 400; }
/* Genuine mono locators: model ids, timestamps, token counts. */
.me-loc { font-family: var(--mono); }
.me-total { font-weight: 600; }
.me-cost-total > td { border-top: 2px solid var(--border); }
/* Verdict grid: purpose in col 1, candidate chips in col 2. */
.me-verdicts { width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
.me-verdicts td { padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border);
  text-align: left; vertical-align: top; }
.me-cand { display: inline-flex; align-items: center; gap: var(--sp-2);
  margin: 0 var(--sp-3) var(--sp-1) 0; }
/* The infra flag: an outline red chip, deliberately NOT a filled quality pill. */
.me-infra { cursor: help; }
/* Thin-frame honesty chips (INSUFFICIENT_FRAME): informational, hover for why. */
.me-frame-chip { cursor: help; }
</style>"""
