# Directive: Fund-Grade Build (2026-06-12 review)

Canonical plan from the 2026-06-12 comprehensive codebase review. Goal restated:
a daily-driver where the operator runs his personal portfolio like a hedge-fund
manager — **(1)** slick front end, **(2)** extremely robust provenance/citation/
credibility ranking, **(3)** LLM infusion with eval loops enabling truly multi-turn
advisory research conversations grounded in the platform's own data, **(4)** full
process capture (journal, initiate/trim/exit/pass rationale with falsifiable
conditions), **(5)** directly modifiable DCFs grounded in real data, performance
vs index — compounding into a smarter retail investor over time.

Session-spawn reference: each session below is one chip/worktree, follows the
one-PR-per-phase cadence (reset + cherry-pick onto fresh main between PRs), runs
the diff-scoped gates (ruff/format/pyright-strict on touched files + targeted
tests), and pushes/merges autonomously per standing authorization. Model
recommendations follow the "Session & Agent Model Selection" rule in `GEMINI.md`.

---

## 1. Current-state scorecard (2026-06-12)

| Pillar | State | Verdict |
|---|---|---|
| Front end | Token system (`src/ui/tokens.py`), command-center shell + lazy panels + palette + peek + SSE are genuinely good. Monoliths: `workspace_html.py` ~4.3k lines, duplicated card CSS across ~5 surfaces, implicit client state (sessionStorage/localStorage ad hoc), thin responsive/a11y, no perceived-latency budget. | Sound foundation, ceiling visible. Polish ≠ rearchitecture; no framework needed yet. |
| Provenance | Every fact row carries `source_doc_id`; 5-tier `SourceQualityTier`; locators (section/line/page/json_path); restatement chains; source chips on financial cells; `/source` viewers; Ask answer-level citations. BUT: `confidence` columns default 1.0 everywhere (never scored); KPI series/charts carry no chips (~70% of on-screen numbers unlabeled); derived metrics have no `computed_from` lineage; cross-source disagreement logged (`validation_issues`) but invisible at the cell; `extracted_by` never rendered. | ~half-way to the bar. Architecture is there; scoring + universality are not. |
| LLM / advisory | Ticker chat persists per report date w/ priors anchor + prior-thread tail. Portfolio-scope Ask has ZERO server-side memory (client-supplied 8-turn tail). Evidence retrieval is one-shot keyword match over facts/filings/transcripts only — it cannot see holdings, weights, conviction, DCF gaps, decisions, or journal. Eval harness real but covers 1 of ~45 purposes. Injection surface on web-fed content un-hardened (see `llm_evals_plan.md` §5.1). | Strong tactical chat; not yet a multi-day advisory brain. |
| Process capture | `decisions` table w/ outcome grading, thesis ledger, sizing intents, journal panel, break rules + thesis_evaluations. BUT: "what would change my mind" is prose, never schema (not falsifiable/resurfaceable); no position-lifecycle ledger (entry/exit thesis snapshots); trades carry no rationale link; journal and decisions don't reference each other; no attribution narrative; concentration metrics not actionable. | Captures WHAT was decided, not WHY in a queryable, resurfaceable way. |
| DCF | 3 archetypes (FCFF redesign, bank NU, holdco BN unwired); edit-preserving rebuild + Sheets round-trip + Python value-of-record + `dcf_runs`. BUT: no scenarios/sensitivity (single-point forecast); assumption reasoning (Opus narratives) never surfaced in workbook or dashboard; workbook↔assumptions-JSON sync is best-effort and silently no-ops if `data/dcf_assumptions/` is absent (verify on MAIN); coverage/staleness opaque (which of ~90 workbooks are live vs orphaned). | "Directly modifiable" works for FCFF; "fully functioning" needs scenarios + provenance + sync repair. |
| Pipeline/ops | ~20 idempotent scheduled jobs, tier-aware FMP budgeting, run accounting (`ingestion_runs`), `source_calls`, validation engine. BUT: cron-registration drift is a proven failure mode (2026-06-11 incident; `fetch_macro_series` + `backup_db` are scripted but unregistered); no failure alerting/dead-man switch; no cron-health panel; no env preflight; no scheduled cross-source reconciliation. | Mature engineering, fragile deployment. Highest single risk to "grounded in data". |

## 2. In-flight sessions (count these as committed work, do not respawn)

| Session (running 2026-06-11) | Covers | Does NOT cover (picked up below) |
|---|---|---|
| LLM evals: execute plan PRs 2-4 | Rubric-judge mode (bear_case, transcript_summary, advisor_next_dollar), transcript sink, evals dashboard panel + weekly cron, golden sets for 3 classifiers, structured-output hardening | Citation-accuracy evals, model-downgrade PRs 2-4 |
| Inbox: flat ranked stream | Inbox v3 | — |
| Eval list: next-dollar attractiveness (PR #430 open) | Cockpit ranking | — |
| Provenance peek: freshness + refresh in-context | Freshness UX + refresh actions | Confidence scoring, chip universality, lineage |
| News beyond FMP: scope + EDGAR pilot | News source breadth | Injection hardening of news→trigger chain |
| Design language v3: fresh-eyes audit + control kit | Visual coherence, control kit | Renderer modularization, latency budget, state container |
| Portfolio: Synthesis subtab | Synthesis surfacing | — |
| Ask dock v2: persistent, split-screen | Dock UX | Server-side thread persistence (S3), evidence reach (S4) |

User action also outstanding: register `fetch_macro_series` (05:35) — folded into S1 instead.

## 3. The build: 17 sessions in 3 waves

Waves are parallelizable internally; later waves depend on earlier ones only
where marked. One session = one chip; multi-PR sessions ship each PR before
starting the next.

### Wave 1 — Trust the substrate (6 sessions)

| # | Session | Scope | Model | Why this model |
|---|---|---|---|---|
| S1 | Ops hardening & cron health | `verify_cron_registration.py` (enumerate .task.xml vs `schtasks /query`, weekly rung); `validate_environment.py` preflight in morning pipeline; register `fetch_macro_series` + `backup_db` tasks; `cron_health` dashboard panel over `ingestion_runs` (7-day timeline, failures w/ error_summary); dead-man post-flight after daily chain | Sonnet 4.6 | Well-scoped, established panel/cron patterns to copy |
| S2 | Provenance v2: confidence + universality + lineage | 2-3 PRs: (a) populate `confidence` on ingest = f(tier, extraction method, cross-source agreement) + render % in chips; (b) `sources_full`/`confidence_full` on `KpiSeries` → chips on KPI charts + ViewSpec renders; (c) `computed_from` JSON on derived kpi_facts + "calculated from X ÷ Y" in popover; surface `validation_issues` disagreement + manual-override reason + `extracted_by` at the cell | Sonnet 4.6 | Schema + plumbing with a clear spec; scoring function is parameterized in this directive, not invented |
| S3 | Ask memory: portfolio thread persistence | `ask_sessions` + `ask_turns` tables; load/save in `src/ask/engine.py` portfolio scope; thread list/resume/rename UI in the dock; audit trail of model answers (kills client-supplied-history trust gap) | Sonnet 4.6 | CRUD + existing ChatStore precedent |
| S4 | Advisory context v2: evidence reach | `gather_evidence` + context packs extended to: tracker holdings/weights, conviction + sizing intents, latest `dcf_runs` gap, open decisions + falsifiable conditions, journal/notes, performance/alpha snapshot; retrieval router decides which packs a question needs; token-budget per pack; eval cases for the router | **Opus 4.8** | This is the product's brain — context-architecture judgment, failure modes are subtle (wrong-context answers look right) |
| S5 | Decision process: falsifiable conditions + lifecycle | 2 PRs: (a) `decision_conditions` JSON schema on `decisions` (+ Haiku parse of "what would change my mind" from memos/rereads), evaluator rung that resurfaces a decision when an incoming KPI fact satisfies a condition; (b) `position_entries` lifecycle ledger (entry/exit date+price+thesis snapshot+conditions+lessons), populated on list_type transitions + tracker trades, surfaced on holding page | Sonnet 4.6 | Clear schema work; the directive fixes the shape |
| S6 | DCF v2a: scenarios + sensitivity | Base/Bull/Bear scenario columns in the redesign workbook + Sheets; `read_and_value()` computes all three (base → `dcf_runs`, bull/bear into snapshot JSON); WACC × terminal-multiple sensitivity grid sheet; dashboard card shows the range, risk-reward view consumes it | Sonnet 4.6 | Python engine already modular; formula work is mechanical |

### Wave 2 — Intelligence & safety (6 sessions)

| # | Session | Scope | Model | Why |
|---|---|---|---|---|
| S7 | Agentic retrieval loop for Ask | Model can request more evidence mid-turn (bounded loop, ≤2 rounds): structured "need: <kind, ticker, period>" → retrieval → continue; replaces one-shot keyword stuffing for complex questions; latency budget + eval cases | **Fable/Opus-class** | Architecture change on the conversational core; loop-safety + prompt design judgment |
| S8 | Per-claim citations + citation-accuracy eval | Structured grounding (claim→cite map) in Ask answers; render inline; golden set scoring citation precision/recall; wire into eval harness | **Opus 4.8** | Prompt/schema design where sloppy design quietly degrades trust |
| S9 | LLM injection hardening | sec-llm pass per `llm_evals_plan.md` §5.1: untrusted-content delimiting/spotlighting in web/news/IR prompts, instruction-priority preambles, injection canaries as mode-A eval cases, threat-model doc for news→trigger→alert chain | Sonnet 4.6 | Checklist-driven with a written plan; canary mechanics reuse the harness |
| S10 | Model-downgrade loop PR2+PR3 | Per `model_eval_loop.md`: verdict ledger + weekly sweep cron (PR2); `model_pin_overrides` guarded auto-switch + auto-demote + dashboard surface (PR3) | Sonnet 4.6 | Directive already written; engine exists (#429) |
| S11 | DCF v2b: assumption provenance + sync + coverage | Surface Opus reasoning per yellow cell (Assumptions & Reasoning sheet or cell comments); repair + verify bidirectional workbook↔`data/dcf_assumptions/` sync (fail loud, sync status in card); DCF coverage/staleness panel (last refreshed, skipped-why, orphans) | Sonnet 4.6 | Plumbing + rendering with existing patterns |
| S12 | Holdco/bank archetype completion | Calibrate BN SOTP marks vs disclosures, expose SOTP drivers as editable inputs, wire `holdco_sotp` live into refresh + dashboard parity; sanity-pass NU bank model assumptions | **Opus 4.8** | Financial judgment (calibrating carry/RE marks), not plumbing |

### Wave 3 — Polish & compounding (5 sessions)

| # | Session | Scope | Model | Why |
|---|---|---|---|---|
| S13 | Renderer modularization + component dedup | Split `workspace_html.py` into per-section modules; extract shared card/chip/table component functions; single CSS source per component (kills 5-way drift). No behavior change; rendered-output regression harness | Sonnet 4.6 | Large but mechanical refactor behind a snapshot harness |
| S14 | Perceived-latency + client state | Skeletons per panel, prefetch likely-next panels, warm-cache for Portfolio/Overview, stale-while-revalidate for panel fetches; small explicit client state container (current ticker/tab/ask context) replacing scattered storage keys | Sonnet 4.6 | Defined patterns, measurable target (sub-500ms perceived) |
| S15 | Journal↔decision loop + attribution narratives | Link `analyst_notes`↔`decisions` (FK + resolve-on-outcome); reversal/calibration analytics lens (hit rate by conviction, reversal patterns); per-position alpha narrative ("what drove over/under-performance") joining tracker alpha + thesis + events | Sonnet 4.6 | Composition of existing primitives |
| S16 | Responsive + accessibility pass | iPad/small-laptop breakpoints across shell + panels; keyboard nav, focus rings, aria labels, chart alt summaries; per global frontend rules | Sonnet 4.6 | Checklist-driven; visual verification loop |
| S17 | Backlog burn-down | Open `platform_backlog.md` items (saydo empty-state, prior-saydo toggle, extra transcript quarters) + `project_deferred_followups` shortlist; close or formally kill each | Sonnet 4.6 (Haiku 4.5 subagents for mechanical sweeps) | Mixed small items; cheap fan-out |

## 4. Sequencing rules

1. Wave 1 starts after the round-4 in-flight sessions merge (avoid collisions:
   S2 vs provenance-peek; S3/S4 vs ask-dock-v2; S13/S14/S16 vs design-v3 — read
   their landed PRs first).
2. S4 before S7/S8 (context packs before agentic loop / per-claim citations).
3. S2 before S8 (chips/confidence exist before citations point at them).
4. Evals-PRs-2-4 session (in flight) before S10 (downgrade sweep reuses rubric
   infra) — likely already satisfied by merge order.
5. Every session adds/extends eval cases when it touches an LLM purpose — the
   eval harness is the standing quality gate, not a separate workstream.

## 5. Explicitly not planned

- Frontend framework migration (Svelte/htmx/etc.) — revisit only if S13/S14
  fail to hit the slickness bar.
- Multi-user/tenant work, CI-run LLM evals, per-call online judging — single
  operator, wrong cost point.
- New data vendors beyond the in-flight news/EDGAR scoping.
