"""Evals panel — the System theme's eval-scores surface (llm_evals_plan §2.6).

Four stacked views over the eval substrate (PR 1 #424 golden sets, PR 2 #435
rubric audits):

* **Run bar** — one "Run" button per evaluable purpose. Each POSTs
  ``/actions/run-eval`` (the jobs registry runs
  ``execution/run_llm_evals.py``) and streams the job's stdout via the
  standard ``/actions/stream/<job_id>`` SSE channel, then refetches the
  fragment so the fresh run appears. LLM spend stays deliberate — the panel
  itself is pure reads.
* **Latest runs** — per purpose: avg score, pass rate, n, prompt_version,
  mode, when, and the run's real cost/latency joined from ``llm_calls`` via
  ``run_id``.
* **Score by prompt version** — the A/B strip over
  ``prompt_calibration_scores`` (``summarize_by_prompt_version``), so "is v2
  actually better than v1?" is answered where the scores land.
* **Failed cases** — drill-down drawer per failed case of each latest run:
  the question/artifact, expected vs actual, and the judge's rationale (the
  evidence-drawer pattern).

Plus the §5.3 observability fold-in: **Call health** — per-purpose
error/fallback rates + cost over the last 30 days of ``llm_calls``, so a
silently-degrading purpose surfaces as a number instead of a vibe.

Missing tables (pre-0083 repo) degrade to an explainer + the run bar.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from llm.calibration import VersionSummary, summarize_by_prompt_version
from ui import living_grid as lg
from ui.controls import prov_case, prov_drawer

# Purposes the run bar offers — mirrors execution/run_llm_evals.py PURPOSES
# (asserted in tests so the two can't drift).
RUNNABLE_PURPOSES: tuple[str, ...] = (
    "viewspec_compile",
    "transcript_metadata",
    "intake_classifier",
    "news_structuring",
    "decision_conditions_extract",
    "ask_pack_router",
    "ask_evidence_followup",
    "ask_claim_grounding",
    "injection_canaries",
    "provenance_caution",
    "peer_selection",
    "key_metrics",
    "scenario_prior",
    "bear_case",
    "transcript_summary",
    "advisor_next_dollar",
    # Incremental Dollar Recommendation (P0.4a) — in sync with
    # run_llm_evals.AUDIT_PURPOSES + rubric_judge.AUDIT_SPECS.
    "incremental_dollar_recommendation",
    # Investment Decision Card (P1.1) — in sync with run_llm_evals.AUDIT_PURPOSES
    # + rubric_judge.AUDIT_SPECS.
    "investment_decision_card",
    # Senior Partner Brief (P2.2) — in sync with run_llm_evals.AUDIT_PURPOSES
    # + rubric_judge.AUDIT_SPECS.
    "senior_partner_brief",
    "ask_advisory_answer",
    # Calibration coach scorecard audit (close_the_loops L8) — keeps this run
    # bar in sync with run_llm_evals.AUDIT_PURPOSES.
    "calibration_coach",
    # Pre-existing omissions vs run_llm_evals.PURPOSES — the CLI runner gained
    # these audit purposes but the run bar wasn't updated (test_llm_evals_ask_loop
    # ::test_runner_purpose_lists_stay_in_sync was already red on main); folded in
    # here so the registries match.
    "earnings_themes_split",
    "qa_topics",
    # The Ledger Phase-1 research-loop gate (mode-A golden classifier) — kept in
    # sync with run_llm_evals.GOLDEN_PURPOSES (test_runner_purpose_lists_stay_in_sync).
    "wondering_detect",
    # The Ledger intent tap (mode-A golden classifier) — supersedes wondering_detect
    # on the live tap; kept in sync with run_llm_evals.GOLDEN_PURPOSES.
    "capture_intent",
    # The Ledger reply-box router + Triage second-pass route suggestion (#884,
    # mode-A golden classifiers) — kept in sync with run_llm_evals.GOLDEN_PURPOSES.
    "ledger_reply_intent",
    "triage_route_suggest",
    # Position-review verdict audit (mode-B rubric) — in sync with
    # run_llm_evals.AUDIT_PURPOSES + rubric_judge.AUDIT_SPECS.
    "position_review",
    # 10-Q segment quarterly period-axis disambiguation Stage B fallback
    # (mode-A golden classifier) — kept in sync with
    # run_llm_evals.GOLDEN_PURPOSES.
    "segment_10q_period_disambiguate",
    # Behavioral-rules distiller audit (tenet-2 Phase 4, mode-B rubric) — in
    # sync with run_llm_evals.AUDIT_PURPOSES + rubric_judge.AUDIT_SPECS.
    "behavior_distill",
    # Sector-benchmark-ETF proposal (comparable_sets_bottoms_up.md §4, Phase 3,
    # mode-A golden classifier) — kept in sync with run_llm_evals.GOLDEN_PURPOSES.
    "sector_benchmark_proposal",
    # The capture->answer primary gate (capture.triage, B3, mode-A golden
    # classifier) — kept in sync with run_llm_evals.GOLDEN_PURPOSES.
    "capture_triage",
    # Decision Draft parse (P2.1, mode-A golden classifier) — kept in sync
    # with run_llm_evals.GOLDEN_PURPOSES.
    "decision_draft_parse",
)

CALL_HEALTH_WINDOW_DAYS = 30
_FAILED_CASES_PER_RUN = 8
_CALL_HEALTH_MAX_ROWS = 40
# Cap each evidence blob in the failed-case drawer — these are raw model/golden
# JSON dumps that can run long; the drawer is a peek, not a viewer.
_EVIDENCE_CHARS = 1200


@dataclass(slots=True)
class LatestRunRow:
    """The most recent eval_runs row for one purpose + its llm_calls join."""

    run_db_id: int
    purpose: str
    mode: str
    run_id: str
    prompt_version: str
    model: str
    judge_model: str | None
    n_cases: int
    n_pass: int
    avg_score: float | None
    started_at: str
    git_sha: str | None
    cost_usd: float
    call_count: int


@dataclass(slots=True)
class FailedCaseRow:
    """One failed case from a latest run — the drawer's evidence."""

    purpose: str
    case_id: str
    question: str
    score: float
    failure_stage: str | None
    judge_rationale: str | None
    expected_json: str | None
    actual_json: str | None


@dataclass(slots=True)
class CallHealthRow:
    """Per-purpose llm_calls health over the window (§5.3 rollup)."""

    purpose: str
    calls: int
    errors: int
    fallbacks: int
    cost_usd: float
    avg_elapsed_ms: int | None

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.fallbacks / self.calls if self.calls else 0.0


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def load_latest_runs(conn: sqlite3.Connection) -> list[LatestRunRow]:
    """Latest run per purpose (by id — insertion order is run order), with
    cost/latency joined back from llm_calls on run_id."""
    if not _has_table(conn, "eval_runs"):
        return []
    rows = conn.execute(
        """
        SELECT r.id, r.purpose, r.mode, r.run_id, r.prompt_version, r.model,
               r.judge_model, r.n_cases, r.n_pass, r.avg_score, r.started_at, r.git_sha
        FROM eval_runs r
        WHERE r.id = (SELECT MAX(id) FROM eval_runs WHERE purpose = r.purpose)
        ORDER BY r.purpose
        """
    ).fetchall()
    has_calls = _has_table(conn, "llm_calls")
    out: list[LatestRunRow] = []
    for r in rows:
        cost, n_calls = 0.0, 0
        if has_calls:
            joined = conn.execute(
                "SELECT COALESCE(SUM(cost_estimate_usd), 0.0), COUNT(*)"
                " FROM llm_calls WHERE run_id = ?",
                (r["run_id"],),
            ).fetchone()
            cost, n_calls = float(joined[0] or 0.0), int(joined[1] or 0)
        out.append(
            LatestRunRow(
                run_db_id=int(r["id"]),
                purpose=str(r["purpose"]),
                mode=str(r["mode"]),
                run_id=str(r["run_id"]),
                prompt_version=str(r["prompt_version"]),
                model=str(r["model"]),
                judge_model=r["judge_model"],
                n_cases=int(r["n_cases"]),
                n_pass=int(r["n_pass"]),
                avg_score=float(r["avg_score"]) if r["avg_score"] is not None else None,
                started_at=str(r["started_at"])[:16].replace("T", " "),
                git_sha=r["git_sha"],
                cost_usd=cost,
                call_count=n_calls,
            )
        )
    return out


def load_failed_cases(
    conn: sqlite3.Connection, runs: list[LatestRunRow]
) -> dict[str, list[FailedCaseRow]]:
    """Failed cases of each latest run, worst score first, capped per run."""
    if not runs or not _has_table(conn, "eval_case_results"):
        return {}
    out: dict[str, list[FailedCaseRow]] = {}
    for run in runs:
        rows = conn.execute(
            """
            SELECT case_id, question, score, failure_stage, judge_rationale,
                   expected_json, actual_json
            FROM eval_case_results
            WHERE eval_run_id = ? AND passed = 0
            ORDER BY score ASC, id ASC
            LIMIT ?
            """,
            (run.run_db_id, _FAILED_CASES_PER_RUN),
        ).fetchall()
        if rows:
            out[run.purpose] = [
                FailedCaseRow(
                    purpose=run.purpose,
                    case_id=str(r["case_id"]),
                    question=str(r["question"]),
                    score=float(r["score"]),
                    failure_stage=r["failure_stage"],
                    judge_rationale=r["judge_rationale"],
                    expected_json=r["expected_json"],
                    actual_json=r["actual_json"],
                )
                for r in rows
            ]
    return out


def load_call_health(
    conn: sqlite3.Connection, *, window_days: int = CALL_HEALTH_WINDOW_DAYS
) -> list[CallHealthRow]:
    """§5.3: per-purpose error/fallback/cost rollup over recent llm_calls.
    Worst error rate first so a degrading purpose tops the table."""
    if not _has_table(conn, "llm_calls"):
        return []
    since = (datetime.now(UTC) - timedelta(days=window_days)).replace(tzinfo=None)
    rows = conn.execute(
        """
        SELECT purpose,
               COUNT(*) AS n,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errs,
               SUM(CASE WHEN fallback_used IS NOT NULL THEN 1 ELSE 0 END) AS fb,
               COALESCE(SUM(cost_estimate_usd), 0.0) AS cost,
               AVG(elapsed_ms) AS avg_ms
        FROM llm_calls
        WHERE purpose IS NOT NULL AND called_at >= ?
        GROUP BY purpose
        """,
        (since.isoformat(),),
    ).fetchall()
    health = [
        CallHealthRow(
            purpose=str(r["purpose"]),
            calls=int(r["n"]),
            errors=int(r["errs"] or 0),
            fallbacks=int(r["fb"] or 0),
            cost_usd=float(r["cost"] or 0.0),
            avg_elapsed_ms=int(r["avg_ms"]) if r["avg_ms"] is not None else None,
        )
        for r in rows
    ]
    health.sort(key=lambda h: (-h.error_rate, -h.calls))
    return health[:_CALL_HEALTH_MAX_ROWS]


def render_evals_panel(db_path: Path) -> str:
    """The Evals tab fragment. Pure DB reads — eval runs only ever start
    through the run bar's explicit POST (judge spend stays deliberate)."""
    runs: list[LatestRunRow] = []
    failed: dict[str, list[FailedCaseRow]] = {}
    health: list[CallHealthRow] = []
    versions: list[VersionSummary] = []
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            runs = load_latest_runs(conn)
            failed = load_failed_cases(conn, runs)
            health = load_call_health(conn)
        finally:
            conn.close()
        versions = [v for v in summarize_by_prompt_version(db_path=db_path) if v.score_count > 0]
    return compose_evals_page(runs, failed, versions, health)


def compose_evals_page(
    runs: list[LatestRunRow],
    failed: dict[str, list[FailedCaseRow]],
    versions: list[VersionSummary],
    health: list[CallHealthRow],
) -> str:
    """Pure page assembly (testable without DB)."""
    return "".join(
        [
            _PANEL_CSS,
            _run_bar(),
            _runs_section(runs, failed),
            _versions_section(versions),
            _health_section(health),
            f"<script>{_RUN_JS}</script>",
        ]
    )


def _run_bar() -> str:
    # One solid-accent primary per view (design_language §4): viewspec_compile
    # is the live golden set, so it alone is .k-btn-primary; the rest are the
    # quiet, dense run buttons.
    buttons = "".join(
        f'<button type="button" data-purpose="{escape(p)}"'
        f' class="k-btn {"k-btn-primary" if p == "viewspec_compile" else "k-btn-quiet"} k-btn-sm">'
        f"{escape(p)}</button>"
        for p in RUNNABLE_PURPOSES
    )
    return (
        '<div class="ev-runbar" id="ev-runbar">'
        '<span class="k-label">Run eval</span>'
        f"{buttons}"
        '<span class="muted ev-note">viewspec_compile = live golden set (16 questions); '
        "the rest audit existing artifacts against their rubric (full corpus — the weekly "
        "cron covers fresh-only). Judge runs on the eval_judge budget; spot-check its "
        "agreement with <code>execution/spot_check_eval_judge.py</code>.</span>"
        '<pre class="ev-log" id="ev-log" hidden></pre>'
        "</div>"
    )


def _score_pill(avg: float | None) -> str:
    """A score as the kit's one filled status pill (``.k-pill``); ``None`` is the
    neutral (untoned) pill showing an em-dash."""
    if avg is None:
        return '<span class="k-pill">—</span>'
    tone = "ok" if avg >= 0.8 else ("warn" if avg >= 0.6 else "bad")
    return f'<span class="k-pill k-pill-{tone}">{avg:.2f}</span>'


def _mode_pill(mode: str) -> str:
    """The run mode as a kit pill: ``live`` accent-toned, ``audit`` neutral."""
    tone = " k-pill-accent" if mode == "live" else ""
    return f'<span class="k-pill{tone}">{escape(mode)}</span>'


def _runs_section(runs: list[LatestRunRow], failed: dict[str, list[FailedCaseRow]]) -> str:
    head = (
        '<section class="panel"><h2>Latest eval runs</h2>'
        '<p class="sub">One row per evaluated purpose — the most recent run, with its real '
        "cost and call count joined from the llm_calls ledger by run_id. Failed cases open "
        "below the row.</p>"
    )
    if not runs:
        return (
            f"{head}"
            '<p class="muted">No eval runs recorded yet — run one from the bar above '
            "(or <code>python execution/run_llm_evals.py --purpose viewspec_compile</code>).</p>"
            "</section>"
        )
    rows_html: list[str] = []
    for r in runs:
        pass_rate = f"{r.n_pass}/{r.n_cases}"
        cost = f"${r.cost_usd:.4f}" if r.cost_usd else "—"
        rows_html.append(
            "<tr>"
            f'<td class="ev-purpose">{escape(r.purpose)}</td>'
            f"<td>{_score_pill(r.avg_score)}</td>"
            f'<td class="num">{escape(pass_rate)}</td>'
            f"<td>{_mode_pill(r.mode)}</td>"
            f'<td class="ev-loc">{escape(r.prompt_version)}</td>'
            f'<td class="num">{cost}<span class="muted"> · {r.call_count} calls</span></td>'
            f'<td class="ev-loc muted">{escape(r.started_at)}'
            f"{' · ' + escape(r.git_sha) if r.git_sha else ''}</td>"
            "</tr>"
        )
        cases = failed.get(r.purpose, [])
        if cases:
            rows_html.append(
                '<tr class="ev-drawer-row"><td colspan="7">' + _failed_drawer(cases) + "</td></tr>"
            )
    return (
        f"{head}"
        '<table class="p-table"><thead><tr>'
        '<th>Purpose</th><th>Avg score</th><th class="num">Pass</th><th>Mode</th>'
        '<th>Prompt</th><th class="num">Run cost</th><th>When</th>'
        "</tr></thead><tbody>"
        f"{''.join(rows_html)}</tbody></table></section>"
    )


def _failed_drawer(cases: list[FailedCaseRow]) -> str:
    """The failed-case drill-down, rendered through the shared provenance kit
    (``prov_drawer`` + ``prov_case``; design_language §10). Each case is one
    ``prov_case`` — a bad-toned score pill + the case id, the question (+ failure
    stage) as the muted meta aside, the judge rationale, and the expected/actual
    JSON as an escaped evidence split (machine text, never markdown)."""
    items = "".join(
        prov_case(
            c.case_id,
            score=c.score,
            meta=f"{c.question[:160]}{f' · stage: {c.failure_stage}' if c.failure_stage else ''}",
            rationale=c.judge_rationale or "",
            expected=(c.expected_json or "")[:_EVIDENCE_CHARS],
            actual=(c.actual_json or "")[:_EVIDENCE_CHARS],
        )
        for c in cases
    )
    n = len(cases)
    summary = f"{n} failed case{'s' if n != 1 else ''} — expected vs actual + judge rationale"
    return prov_drawer(summary, items)


def _versions_section(versions: list[VersionSummary]) -> str:
    head = (
        '<section class="panel"><h2>Score by prompt version</h2>'
        '<p class="sub">The A/B dimension: every eval/grader score keyed to '
        "(purpose, prompt_version) — bump the registry, re-run the eval, and the rewrite "
        "is a comparable number instead of a vibe.</p>"
    )
    if not versions:
        return f'{head}<p class="muted">No calibration scores yet.</p></section>'
    by_purpose: dict[str, list[VersionSummary]] = {}
    for v in versions:
        by_purpose.setdefault(v.purpose, []).append(v)
    rows: list[str] = []
    for purpose in sorted(by_purpose):
        chips = "".join(
            f'<span class="k-chip k-chip-mono ev-vchip" title="n={v.score_count} · p25 {v.p25:.2f} · '
            f'p50 {v.p50:.2f} · p75 {v.p75:.2f}">'
            f"{escape(v.prompt_version)} {_score_pill(v.avg_score)}"
            f'<span class="muted">&times;{v.score_count}</span></span>'
            for v in sorted(by_purpose[purpose], key=lambda s: s.prompt_version)
        )
        rows.append(f'<tr><td class="ev-purpose">{escape(purpose)}</td><td>{chips}</td></tr>')
    return f'{head}<table class="ev-versions"><tbody>{"".join(rows)}</tbody></table></section>'


def _health_section(health: list[CallHealthRow]) -> str:
    head = (
        '<section class="panel"><h2>Call health (30d)</h2>'
        '<p class="sub">Per-purpose error and fallback rates over the llm_calls ledger — '
        "a purpose silently degrading (errors swallowed at section scope, or quietly "
        "running on the Gemini fallback) surfaces here as a number.</p>"
    )
    if not health:
        return f'{head}<p class="muted">No LLM calls in the window.</p></section>'
    rows = "".join(
        f"<tr{_health_data(h)}>"
        f'<td class="ev-purpose">{escape(h.purpose)}</td>'
        f'<td class="num">{h.calls}</td>'
        f'<td class="num {"ev-bad" if h.error_rate > 0.1 else ""}">'
        f"{h.error_rate * 100:.0f}%<span class='muted'> ({h.errors})</span></td>"
        f'<td class="num {"ev-warn" if h.fallback_rate > 0.1 else ""}">'
        f"{h.fallback_rate * 100:.0f}%<span class='muted'> ({h.fallbacks})</span></td>"
        f'<td class="num">${h.cost_usd:.2f}</td>'
        f'<td class="num">{f"{h.avg_elapsed_ms:,.0f}" if h.avg_elapsed_ms is not None else "—"}</td>'
        "</tr>"
        for h in health
    )
    return (
        f"{head}"
        + lg.grid_open()
        + lg.filter_bar(len(health), noun="purposes", placeholder="Filter by purpose…")
        + '<table class="p-table"><thead><tr>'
        + lg.th("Purpose", "purpose", "text", num=False)
        + lg.th("Calls", "calls", "num")
        + lg.th("Error rate", "err", "num")
        + lg.th("Fallback rate", "fb", "num")
        + lg.th("Cost", "cost", "num")
        + lg.th("Avg ms", "ms", "num")
        + "</tr></thead><tbody>"
        + f"{rows}</tbody></table>"
        + lg.grid_close()
        + "</section>"
    )


def _health_data(h: CallHealthRow) -> str:
    return (
        lg.data_text(h.purpose)
        + lg.data_text_key("purpose", h.purpose)
        + lg.data_num("calls", h.calls)
        + lg.data_num("err", h.error_rate)
        + lg.data_num("fb", h.fallback_rate)
        + lg.data_num("cost", h.cost_usd)
        + lg.data_num("ms", float(h.avg_elapsed_ms) if h.avg_elapsed_ms is not None else None)
    )


# Token-clean local CSS. Score/mode pills now ride the kit's .k-pill and the
# failed-case drawer rides .k-prov-drawer/.k-prov-case (controls.py), so this
# carries only the run bar, the log, the version chip, and a couple of table
# tweaks — all on the type/radius/color tokens.
_PANEL_CSS = """<style>
.ev-runbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 18px; font-size: var(--fs-body); }
.ev-note { font-size: var(--fs-caption); }
.ev-log { width: 100%; margin: 8px 0 0; padding: 8px 10px; background: var(--paper);
  border: 1px solid var(--border); border-radius: var(--radius); font-family: var(--mono);
  font-size: var(--fs-caption); max-height: 180px; overflow-y: auto; white-space: pre-wrap; }
.ev-drawer-row > td { padding: 0 0 10px 12px; border: none; }
/* The version locator rides the kit's .k-chip.k-chip-mono; only the inter-chip
   separation + tooltip affordance are layout-local. */
.ev-vchip { margin-right: 8px; cursor: help; }
.ev-bad { color: var(--bad); font-weight: 600; }
.ev-warn { color: var(--warn); font-weight: 600; }
/* Purpose names are sans labels (NOT tickers) — emphasis without mono. */
.ev-purpose { font-weight: 600; }
/* Genuine mono locators only (prompt_version, run timestamp). */
.ev-loc { font-family: var(--mono); }
</style>"""

# Run-bar wiring — same POST + SSE + refetch shape as the Memos panel.
# Plain string: braces are literal JS.
_RUN_JS = r"""
(function () {
  var bar = document.getElementById('ev-runbar');
  if (!bar) return;
  var logEl = document.getElementById('ev-log');
  function append(line) {
    logEl.hidden = false;
    logEl.textContent += line + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }
  function refetch() {
    var target = bar.closest('.cc-panel-body') || bar.parentElement || document.body;
    fetch('/api/panel/evals')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        target.innerHTML = html;
        var scripts = target.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
          var old = scripts[i];
          var s = document.createElement('script');
          if (old.src) s.src = old.src; else s.textContent = old.textContent;
          old.parentNode.replaceChild(s, old);
        }
      })
      .catch(function (e) { append('reload failed: ' + e.message); });
  }
  bar.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-purpose]') : null;
    if (!btn) return;
    var purpose = btn.getAttribute('data-purpose');
    var buttons = bar.querySelectorAll('button');
    buttons.forEach(function (b) { b.disabled = true; });
    logEl.hidden = false;
    logEl.textContent = '';
    append('starting eval: ' + purpose + '…');
    fetch('/actions/run-eval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purpose: purpose })
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    }).then(function (job) {
      var es = new EventSource(job.stream_url);
      var finished = false;
      es.onmessage = function (ev2) {
        var m;
        try { m = JSON.parse(ev2.data); } catch (_) { return; }
        if (m.event === 'start') {
          append('> job ' + m.job_id + ' started (' + m.kind + ')');
        } else if (m.event === 'log') {
          append(m.line);
        } else if (m.event === 'done') {
          finished = true;
          append('# exit code ' + m.exit_code + (m.exit_code === 3 ? ' (below --min-score gate)' : ''));
          es.close();
          buttons.forEach(function (b) { b.disabled = false; });
          if (m.exit_code === 0) refetch();
        }
      };
      es.onerror = function () {
        if (!finished) append('stream closed');
        es.close();
        buttons.forEach(function (b) { b.disabled = false; });
      };
    }).catch(function (e) {
      append('failed: ' + e.message);
      buttons.forEach(function (b) { b.disabled = false; });
    });
  });
})();
""".strip()
