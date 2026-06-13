# Directive: Residual backlog + chip plan (2026-06-13)

Catalog of all pending/unbuilt work after the two big 2026-06 programs landed, with a
prioritized chip plan. Produced by a full sweep of: both canonical session plans
(`fund_grade_build_2026_06.md` S1–S17, `interaction_paradigm_2026_06.md` S1–S12), every
`directives/*.md`, the `platform_backlog.md` tracker, and a code-level verification pass
(several directives are stale — verdicts below were checked against the code, not the docs).

**Headline:** both programs are ~95% landed. `platform_backlog.md` has exactly one open
item (peer selection). The residual is small and well-bounded — no large unfinished
workstream is hiding. Model rule per `GEMINI.md` → Session & Agent Model Selection.

---

## 0. Stale-directive corrections (do not re-build these)

- **`llm_evals_plan.md §5.1` (prompt injection)** — DONE. Shipped as fund-grade S9
  (#480/#485): `src/llm/untrusted.py::spotlight()`, `WEB_CONTENT_NOTICE`, injection canaries.
- **`model_eval_loop.md` PR2 + PR3** — DONE (code-verified). `model_eval_verdicts` +
  `model_pin_overrides` tables in alembic `0084`; weekly sweep `execution/run_weekly_model_eval.py`;
  auto-switch wired into `src/llm/cli.py::_model_for()` via `active_override(purpose)`. Only
  PR4 (anti-regression golden cases + cost-savings rollup) may be open — minor; verify & close.
- **`report_comments_and_chat.md`** — SHIPPED. Comments + in-report chat landed across the
  comments_server + S4 iframe work (`workspace_chat.py`/`workspace_comments.py` exist).
- **`improvement_roadmap_2026_06.md`** — SUPERSEDED by `master_build_2026_06.md` (COMPLETE,
  all 20 phases #361–#393). Historical records only.
- **News 13F leg (`news_sources_plan.md`)** — partly absorbed by IP-S6's live EDGAR 13F-HR
  miner (#516). Remaining news follow-up is the free yfinance `Ticker.news` rung (Cat E).

---

## 1. Categories (relevance verdicts)

### A — Owner-flagged, spec'd, ready (top priority)
- **LLM peer selection** — `directives/peer_selection_llm.md` (#519 spec, verified unbuilt:
  no `peer_selection` in `cli.py`, no `PeerSuggestion` model). The actual root cause of the
  owner's "shit peers" complaint (NU→Barclays, NOW→Applied Materials). S5 added *steering*;
  this is the *generator*. The only open `platform_backlog.md` item.

### B — Interaction-Paradigm tail (finish the recent wave)
- **IP-S8** Ask panel: drop `<h2>Ask</h2>` (`explore_panel.py:740`), kill `--fs-section`
  override on input (`:125`), reset placeholder after submit. NOT BUILT.
- **IP-S9** Cockpit doorways: shell-global `data-ask-q` delegate, `/api/peek/documents`,
  "N new docs"→`data-peek-url`. PARTIAL (holding `.cc-combo` search-bar done). **Owns the
  latency baseline that gates the S12-spine decision.**
- **IP-S12 `fact_ref` first-half**: thread `kpi_definition_id` through ledger rows, emit
  `data-fact-ref`, `fact_ref` column on `analyst_notes`. NOT BUILT. (Full 8-writer signals
  *spine* stays deferred-by-design, profiling-gated — see Cat F.)
- **IP-S11 diet second leg**: `model_revision`/`media_appearance` feeds, investor-day +
  podcast→takeaway summarization (LLM), triage panel/route, journal-silo. NOT BUILT (first
  leg `diet_panel.py` only). Free legs buildable; paid legs (`buyside_rating`,
  `estimate_revision`) stay scaffolded-disclosed in `src/signals/store.py`.

### C — Token-conformance endgame (finish the keystone)
- `test_ui_controls.py::test_full_conformance_is_red` is a `strict=True` xfail that flips to
  a hard CI failure the moment `QUARANTINE` empties. Still **12 surfaces** quarantined, two
  clusters: (a) 7 provenance/coverage panels (cron_health, dcf_coverage, evals, ir_coverage,
  restatements, source_calls, source_viewers) — mostly off-scale `radius`, evals also
  color+font-size; (b) 5 report-iframe surfaces (workspace_charts/chat/comments/styles,
  cite_marks) blocked on ONE coupled change — unforking the legacy-alias `:root` in
  `workspace_styles.py`. Until empty, the conformance guarantee isn't enforced.

### D — Spec'd-but-unbuilt capability tracks (older directives, still valid)
- **Document deep tables** (`document_tables_design.md`): Phase 2 MVP = `lease_commitments`
  (deterministic FMP parse) + `customer_concentration` (LLM). Phase 3 = 11 stub extractors
  (`src/table_extractors/__init__.py` already raises NotImplementedError with contracts).
  Phase 4 = DEF 14A. New schema + LLM + parser. Take MVP slice only.
- **Annual-KPI cadence** (`annual_kpi_cadence_design.md`): `reporting_cadence` column +
  `FY`-tagged annual facts so annual-only KPIs (bank capital-adequacy) stop being forced
  quarterly in break-rules. Small, design-ready (8 modules listed in the directive).
- **Cross-asset Phase B** (`cross_asset_data_model.md`): reclassify legacy `list_type='etf'`
  (FLKR)→watchlist + drop `'etf'` from CHECK. Tiny cleanup. Phases C/D/E = Cat F.

### E — Loose ends / data quality (opportunistic)
- **Soft-rule evaluator wiring**: `src/compute/soft_rule_evaluator.py` exists but
  `process_report_comments.py:1121` notes it's "not yet wired up" (YELLOW signals dormant).
- **6 dead KPI rules** (RBRK SBC/Sub-Rev; NOW Now-Assist ACV, >$5M ACV; WIX Studio ARR,
  Partners Rev-share) allowlisted in `test_holdings_rule_kpi_alignment.py` — build derivers
  or formally kill.
- **`data_fixes.md`**: AMZN `-1469.5%` "looks wrong" — single validation bug, triage + fix.
- **yfinance `Ticker.news` rung**: free middle rung between FMP and the WebSearch+Opus
  fallback; cuts Opus fallback cost.
- **Q4CDN discovery stub** (`ir_pipeline/discover/q4cdn.py` NotImplementedError): FMP/SEC
  already cover it — likely kill, don't build.

### F — Explicitly deferred / not worth it now (document, don't build)
Full signals spine (profiling-gated by design) · cross-asset bonds/options/FX (single
operator, wrong cost point) · diet paid legs buyside/estimate (no free data path) · DEF 14A
pipeline (gated on doc-tables MVP) · S-1 table extraction (out of scope until volume) ·
frontend framework migration (off-plan) · `model_eval_loop` PR4 (minor; verify & close).

---

## 2. Chip plan (build order)

One chip = one worktree, one-PR-per-phase cadence, diff-scoped gates
(ruff/format/pyright-strict on touched files + targeted tests), push/merge autonomously
per standing authorization. Chips 1–3 are the high-value core; 4–7 optional depth.

| # | Chip | Model | Contents | Rank rationale |
|---|---|---|---|---|
| 1 | Peer selection | Sonnet | Cat A — build to `peer_selection_llm.md`, eval-gated | Only open tracker item; direct owner complaint; fully spec'd |
| 2 | Conformance endgame | Haiku→Sonnet | Cat C — PR(a) radii/color sweep on 7 panels; PR(b) `workspace_styles` `:root` unfork → flip the xfail green | Finishes the keystone; makes the guard enforced not advisory |
| 3 | UI doorways tail | Sonnet | Cat B: IP-S8 + IP-S9 + IP-S12 `fact_ref` half (all bind S1; all "datum=doorway / title-ownership") | Answers owner feedback; S9 captures the latency baseline for the S12 decision |
| 4 | Schema burn-down | Sonnet | Cat D2 (annual-KPI cadence) + D3 (cross-asset Phase B) + E2 (dead-rule disposition) | Small correctness wins, batched |
| 5 | Diet second leg (free) | Opus | Cat B IP-S11 free legs: investor-day + podcast takeaway summarization, `model_revision`, triage panel | The diet-curation the owner asked for; summarization needs judgment |
| 6 | Document deep tables MVP | Sonnet + Haiku | Cat D1 Phase 2 (lease + customer-concentration) | Net-new analytical surface; largest; lowest urgency |
| 7 | Loose-ends sweep | Sonnet + Haiku | Cat E1 (soft-rule wiring) + E3 (AMZN bug) + E4 (yfinance rung) + E5 (kill Q4CDN) | Cheap fan-out, anytime |

Coordination: `controls.py` / `design_language.md` / `test_ui_controls.py` / alembic-head
are shared seams — chips touching them serialize and rebase migration numbers at REBASE
time on the live head (collisions are silent). See `interaction_paradigm_2026_06.md §6`.
