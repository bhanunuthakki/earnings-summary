# Master build directive — research companion & investment advisor

**Status: ACTIVE — the canonical plan.** Supersedes `improvement_roadmap_2026_06.md`
(now a pointer stub). Origin: the 2026-06-10 audit + grill-me session; every
decision below was confirmed by the owner. Agents executing this directive:
update the phase checkboxes as PRs land, and record deviations in the
Decision log at the bottom.

## North star

> Deep fundamental research into companies; surface interesting new
> companies; deeply grounded auditable data that can be sliced and diced on
> command; understand opportunity costs and portfolio positioning; record
> thoughts persistently. A research companion and investment advisor that
> gets better over time.

## The three themes (target information architecture)

The application collapses from 12 dashboard tabs + scattered surfaces into
three themes. Everything ships under one of them:

1. **Research** — the holdings cockpit (thesis health · valuation · events
   per row) drilling into per-ticker workspaces; on-the-fly exploration via
   saveable pivots + a natural-language query box; the analyst journal
   (notes) woven throughout; discovery queue for new names.
2. **Portfolio** — performance, allocation, and the *advisor's home*: TWR
   vs SPY/QQQ/policy, positioning by account type/sector, sizing audits,
   next-dollar and swap memos, the Socratic think-through, and the record
   of key allocation decisions.
3. **Governance** — provenance, data quality, coverage, validation; admin
   reduced to a settings drawer (budgets, ticker settings, maintenance
   actions, job streams).

**Killed as standalone surfaces** (owner: "redesign entirely"): Pre-reads,
Insiders, Predictions, the Decisions tab (its content folds into Portfolio's
allocation-decisions record). Email/push delivery, remote/phone access, and
event-driven discovery feeds (IPO/spinoff watchers) are **out of scope** —
the app stays pull-only on localhost.

## Architecture decisions (locked in the grill)

- **Single brain.** earnings-summary is the one advisor: research, memory,
  and allocation advice live here. The portfolio-tracker sibling is a pure
  data API (`/api/portfolio/*`: holdings, performance, position-alpha,
  positioning, beta, policy). Small tracker-side PRs are authorized when an
  endpoint is missing (e.g. cash-available, account-type performance
  slices). The tracker's own `cio_advisor` / `coaching` / `decision_support`
  routes are retired or left dormant — never extended.
- **Advisor posture: thinking partner.** Default output is evidence +
  framing, not directives. Portfolio-level allocation memos (next-dollar,
  rebalance review) ARE in scope. Per-holding stances exist **on request
  only**, via the Socratic think-through: the system asks the owner 3–5
  pointed questions (their read, horizon, what would make them wrong), then
  writes a one-page decision memo — bull / bear / what-would-change-my-mind
  / stance-if-forced, with tracker sizing-and-alpha context — logged and
  later scored against outcomes. Every displayed stance carries its
  scorecard.
- **Benchmarks: reuse the tracker's.** SPY-counterfactual alpha primary;
  QQQ and the owner's policy mix alongside. Never rebuild return math here.
- **Slice-and-dice: ViewSpec substrate.** A deterministic, saveable pivot
  spec (metrics × tickers × period × transform: level/YoY/CAGR/margin) over
  financial_facts / kpi_facts / segments — instant and LLM-free. The
  natural-language box compiles to a ViewSpec via a fast model (never raw
  SQL); failures degrade to the structured builder. Saved views embed in
  the cockpit and reports.
- **Discovery: queue, never auto-build.** Factor screens over the tracked
  universe (incl. index members) + adjacency mining (competitors/suppliers/
  customers repeatedly named in transcripts, news, and competitive
  watchlists of current holdings) + one-click inbound. Candidates land in
  an approval queue; the owner triggers individual or bulk eval builds from
  the dashboard or chat. A full eval build costs ~25 min + LLM spend — the
  queue is the budget gate.
- **Tax-aware placement = context, not engine.** Memos use the tracker's
  account-type classification (taxable / deferred / Roth) and 1099 realized-
  lot detail to frame placement ("this trim realizes a short-term gain in
  taxable"). No per-lot open-position engine in v1.
- **Memory everywhere.** analyst_notes (0074) + the priors anchor (#357)
  are load-bearing: every advisor artifact reads them and writes back to
  them (memos create notes; resolved questions get marked).

## Working agreements (standing authority — granted 2026-06-10)

- **Cadence:** one PR per phase; build → verify → push → merge when CI
  green. Re-fetch origin/main + check for parallel-duplicate PRs before
  every push (parallel sessions are active in this repo).
- **Prod DB migrations:** autonomous, using the proven procedure — backup
  `data/portfolio.db` first, migrate, verify (revision + row counts +
  `PRAGMA integrity_check`).
- **Live MAIN checkout deploys:** autonomous at session boundaries via the
  safe reconcile — backup, `merge --ff-only` (clear colliders per its
  documented waves), restore the owner's uncommitted holdings/DCF files on
  top. Never force-pull MAIN.
- **LLM budget:** ~$150/month all-in ceiling, enforced through the existing
  `llm_budgets` caps (raise per-purpose caps to fit, keep `warn`/`skip`
  modes meaningful). Discovery eval builds are approval-gated, never
  ambient.
- **Quality gates per PR:** suite-adjacent pytest green; CI's diff-aware
  gates reproduced locally INCLUDING the `ruff format --diff` hunk-overlap
  check per changed file (repo line-length is 100; the local
  format_changed.py pass is not sufficient on Windows); pyright clean on
  new code.

## Phase plan

Phases are PR-sized. Order within a wave is load-bearing; waves may
interleave when blocked. A phase may split into at most two PRs if the diff
demands it (note the split in the Decision log).

### Wave 0 — Foundation
- [x] **P0.1 Design tokens + formatting.** *(#361; formatting-module extensions fold into P1.2)* One shared token module (palette
  with a single semantic good/bad pair, type scale, spacing) consumed by
  every HTML surface (workspace_styles, dashboard _styles, command-center
  shell, analytical inline CSS, digest/feed, calendar); one formatting
  module (compact money via `numfmt`, percent vs pp, bps, dates, relative
  time) replacing ad-hoc f-strings; favicon + `<title>` convention.
  *Accept:* the same value renders identically on every surface; one place
  changes any color/format; chart palette keys off tokens.

### Wave 1 — Three-theme shell + Research
- [x] **P1.1 Three-theme shell.** *(#363)* Nav = Research / Portfolio / Governance
  (+ settings drawer). Old tab deep-links 302 into the new homes.
  Pre-reads / Insiders / Predictions / Decisions panels removed from nav
  (code paths retired or folded; insider signals stay inside the per-ticker
  Exec-Comp section).
- [x] **P1.2 Research cockpit.** *(#366; P0.1b numfmt helpers folded in;
  next-earnings reads the FMP calendar cache — the expected_earnings table
  was dropped in 0031)* Landing rows per holding: thesis health
  (verdict, breach/watch, tier-1 KPI deltas), valuation (price & day move,
  DCF gap/MoS, PEG), events (next earnings from expected_earnings,
  unreviewed alerts count, new docs since last build). Evaluation list gets
  a thinner variant. Ops freshness shrinks to a dot. IR/maintenance actions
  parked under Governance → Actions until the P3.4 settings drawer.
  *Accept:* "which holding needs my attention today?" answered by the
  landing screen alone.
- [x] **P1.3 Holding drill-in.** *(#378)* Per-ticker command center + report iframe
  consolidated under Research; per-ticker open notes and recent alerts
  surface beside the report.

### Wave 2 — Portfolio theme (the advisor's home)
- [x] **P2.1 Tracker integration v2.** *(#367)* Expand the tracker client to consume
  /performance, /position-alpha, /positioning, /beta, /policy; Portfolio
  page v1: TWR vs SPY/QQQ/policy, allocation by account type/sector,
  concentration, per-position alpha. Degrades gracefully when the tracker
  is offline (existing pattern).
- [x] **P2.2 Allocation-decisions record.** *(#377)* Sizing-audit view (conviction
  vs weight vs valuation gap vs alpha, mismatches ranked); the decisions
  ledger + position-sizing intents fold in as the decisions timeline.
- [x] **P2.3 Advisor memos.** *(#381)* Next-dollar allocation memo (on demand +
  monthly cron) and swap-discipline checks (holding's expected return vs
  watchlist alternative by margin → swap memo). Memos read priors + facts
  + tracker context; persist as notes + ledger entries; rendered under
  Portfolio.
- [x] **P2.4 Socratic think-through.** *(#383)* Chat-initiated flow: 3–5 questions
  to the owner first, then the decision memo (template above), saved +
  scheduled for outcome scoring. Entry points: Portfolio page + per-ticker
  workspace chat.
- [x] **P2.5 Stance scorecard.** *(#385)* Scoring job grades past memos/stances
  against subsequent prints and price (configurable horizon); every
  rendered stance shows its track record. (Tracker-side mini-PRs for
  cash-available / account-type slices land here if needed.)

### Wave 3 — Provenance & Governance
- [x] **P3.1 Locator schema.** *(#365)* `documents` += accession_number,
  filing_date (backfilled where derivable); fact tables += nullable
  `locator` JSON (10-K section key, transcript line ids, PDF page, FMP JSON
  path). Migration + backfill CLI.
- [x] **P3.2 Extractor wiring.** *(#371)* Fact writers populate locators going
  forward; `source_excerpt` systematically on transcript/filing-sourced
  KPI facts.
- [x] **P3.3 Source chips + drawer.** *(#374)* Per-number source chip in Financials
  + Earnings tabs (hover: tier + fetched-at; click: excerpt + open-source
  link). Fix the alert evidence drawer's dead citation kinds.
- [x] **P3.4 Governance panel.** *(#375)* Coverage / data-quality / validation /
  source_calls dashboards consolidated; settings drawer (budgets, ticker
  settings, maintenance actions, job streams) replaces admin-as-tab.
- [x] **P3.5 Source viewers.** *(#376)* Transcript reader with line anchors; 10-K
  section reader over the parsed form_10k JSONs; restatement "was X, now Y"
  view on superseded facts.

### Wave 4 — Report cohesion + memory surfacing
- [x] **P4.1 Section chrome.** *(#379)* One header anatomy (title · as-of · source
  chip), analyst-language empty states (no alembic/CLI text in reports),
  empties collapse, one table class, one collapse idiom.
- [x] **P4.2 Coverage fill-or-hide.** *(#382)* Per-ticker section coverage report;
  hide-don't-stub policy; peer-comp fix (better comparable selection or
  drop the panel — owner flagged the current peers as wrong).
- [x] **P4.3 Cross-links.** *(#384)* Earnings themes ↔ bear case; valuation ↔
  thesis KPI drivers; signals ↔ news; concentration → bear.
- [x] **P4.4 (D3) Event resurfacing.** *(#387)* Digest "open items" panel; earnings
  prep + new builds lead with the owner's open watch-items; alerts attach
  relevant notes to their evidence.
- [x] **P4.5 (D4) Journal UI.** *(#388)* Notes lifecycle in the app (list / filter /
  resolve / reclassify / supersede via /api/notes) + "add note" capture on
  any report section.

### Wave 5 — Exploration + discovery
- [x] **P5.1 ViewSpec engine.** *(#389)* Deterministic pivot spec + renderer
  (table + chart, provenance-chipped once P3.3 lands); saved views; embed
  hooks for cockpit/reports.
- [ ] **P5.2 NL compile.** Query box → fast-model → ViewSpec (validated,
  schema-constrained, never raw SQL); degrade to the builder UI on parse
  failure.
- [ ] **P5.3 Discovery pipelines.** Factor screens over the tracked
  universe; adjacency miner over transcripts/news/competitive watchlists;
  candidates table with "why surfaced" evidence.
- [ ] **P5.4 Discovery queue.** Approval queue UI under Research + chat
  commands; individual + bulk eval-build triggers (budget-aware, streamed
  via the existing jobs SSE).

### Wave 6 — Hardening
- [ ] **P6.1 Consolidation cleanup.** Retire dead routes/renderers left by
  the theme migration; latency pass on the three landing surfaces; residual
  design-debt sweep; directive close-out review.

## Decision log

- 2026-06-10 grill: deliverable = this directive, executed autonomously
  (not one mega-PR). Advisor = thinking partner; stances on request via
  Socratic flow; portfolio-level memos in scope. Pull-only. Discovery =
  screens + adjacency + inbound, queue-gated builds (bulk/individual via
  chat or dashboard). Opportunity cost = all four lenses; tracker is the
  benchmark/tax source of record. Budget ≈ $150/mo. Standing authority for
  migrations + MAIN deploys. Kill list: Pre-reads, Insiders, Predictions,
  Decisions tab; whole app re-centered on the three themes.
- 2026-06-10: D1 (#356) and D2 (#357) shipped before this directive; D3/D4
  appear here as P4.4/P4.5.
- 2026-06-10 (P2.1, #367): landed while P1.2 was still in flight (#366) —
  waves interleaved per the phase-plan rule; zero file overlap (P2.1 touches
  only the tracker client + portfolio_panel; the shell was deliberately left
  untouched). The Portfolio sub-tab keeps its "Live portfolio" label for now
  — rename, if wanted, belongs with P2.2's reorganization. Policy weights
  come from `GET /api/policy` (the tracker has no `/policy/weights` route).
  Positioning's per-ticker correlation/beta rows are parsed-over in v1 (only
  the book-level weighted-avg-correlation is consumed); a per-position beta
  column in the alpha table is a P2.2+ candidate.
- 2026-06-10 (P1.2, #366): cockpit surfaced two latent data bugs, both fixed
  same-day in spun-off PRs: prod tracked_companies.user_id was TEXT '1'
  (0073's INTEGER-only remap missed it → empty company universe on current
  main; repaired + migration hardened, #369), and the bank/holdco DCF
  writers stored over_under_pct as percent-upside instead of the documented
  ratio (Triggers ladder misread NU/BN as sell; writers normalized, #368).
  The cockpit recomputes the FV gap from each run's own price + fair value,
  so it is immune to either convention. Old GET-/ ops tables + their
  renderers retired with the move (dashboard_html is now just the Actions
  fragment); /api/dashboard JSON unchanged.
- 2026-06-10 (P2.5, #385): scoring grades against PRICE (div-adjusted FMP
  caches) with the SPY benchmark taken from the tracker's /performance
  series (benchmark math stays with the tracker) and an honest 'absolute'
  fallback basis when it's down; "subsequent prints" grading (KPI deltas vs
  the memo's claims) is deferred — price is the unambiguous scoreable unit
  at this memo volume. Hold stances fail only below -5pp (a hold is a
  decision NOT to trim). Swap checks grade the screen (realized margin),
  not an imputed stance; next-dollar memos are unscoreable v1. No
  tracker-side mini-PR was needed (the optional cash-available /
  account-type slices weren't required by any shipped surface). Wave 2 is
  complete.
- 2026-06-10 (P2.4, #383): the flow is stateless two-step (questions ride
  the form; no session table) with SYNC endpoints — the owner is at the form
  on localhost, so the jobs/SSE machinery stays for fire-and-forget runs.
  "Chat-initiated" lands as: the workspace chat header links to
  /socratic/<T> AND the chat system prompt now declines in-chat stance
  requests, pointing to the flow — keeping stances on-request-only and
  scoreable rather than leaking unsccored stances through chat prose. The
  forced STANCE line parses into advisor_memos.stance + horizon_days
  (score_status pending); a missing stance still persists the memo as
  recorded-but-direction-unscoreable.
- 2026-06-10 (P2.3, #381): conviction stays the advisor's INPUT, never its
  output — both memo prompts hard-forbid directives; a swap memo concluding
  "the bar isn't cleared" is documented as a successful outcome. The swap
  screen gained a +100% upside plausibility ceiling after prod data showed
  currency-artifact watchlist DCFs (TSM +3570%, STNE +519%) dominating every
  row; excluded names render as a data-quality footnote. Deviation from "persist
  as notes + ledger entries": the portfolio-level next-dollar memo writes a
  note only — thesis_ledger_entries is ticker-keyed by schema (0062), so
  ledger entries are written for ticker-scoped memos (swap checks, Socratic).
  advisor_memos (0077) ships score_status='pending' dormant for P2.5; the
  monthly cron XML ships in-repo and registers at the deploy boundary.
- 2026-06-10 (P2.2, #377): conviction is *recorded, never inferred* — the
  audit's conviction/target columns read only `position_sizing_intent` (empty
  in prod at ship time; the panel's inline editor + `POST /api/sizing-intents`
  are the cold-start path), with the thesis verdict shown as its own column
  rather than blended into a pseudo-conviction. Mismatch heuristics are
  deliberately coarse rank-only scores with every contribution rendered as a
  chip. The Portfolio sub-tab rename deferred from P2.1 landed as
  "Performance"; the standalone Thesis Ledger tab folded into the Decisions
  record (hashes redirect, fragment endpoint kept). No new tables.
- 2026-06-10 (post-P2.1 follow-up, #372): owner asked for an editable
  analytics window on the Portfolio page. Window bar (1M/3M/6M/YTD/1Y/2Y
  presets + custom dates + the tracker's modeled-backfill toggle) refetches
  the panel with start/end passed to the four windowed tracker endpoints;
  Default remains the tracker's own windows; date presets are computed in
  the browser; tracker untouched. Window choice is session-scoped (lives in
  the loaded panel DOM) — server-side persistence deferred until something
  needs it.
- 2026-06-10 (P1.3, #378): the "consolidated under Research" half was
  already in place (#248/#250 + P1.1 — /ticker/<t> 302s into #holding=<T>),
  so the phase's delta is the report split: open notes (analyst_notes,
  open-only, read-only until the P4.5 journal) + recent alerts in a rail
  beside the embedded report. Alerts reuse the digest/feed card via a new
  package-public render_alert_card with the evidence drawer collapsed by
  default in the rail (new default_open passthrough; digest/feed contract
  unchanged). Each rail source degrades independently when its table is
  missing. The queued-action cards' relative approve links remain dead on
  the live server everywhere (no /approve route — pre-existing on /digest
  and /feed too; the CLI hint is the real path) — left for P4.4/P6.1.
- 2026-06-11 (P4.1, #379): chrome decisions taken in-phase — the one table
  class is `.tbl` (+`.tbl-nowrap` for dense numeric grids; semantic
  modifiers like kpi-ledger-table keep only table-specific rules); the one
  collapse idiom is `<details class="panel">` + `<summary class="panel-head">`
  (Q&A accordion moved off JS onto native details; the financials line-item
  drill is the documented JS exception — `<tr>` can't nest in `<details>`);
  empty states are collapsed-by-default `<details>` one-liners via
  `_empty_panel`, and `_missing_panel` no longer renders
  `MissingReason.fix_command` (reports speak analyst; ops live under
  Governance — html.py/markdown.py legacy renderers left as-is). Panel-level
  source chips wired where provenance already flows (financial highlights);
  remaining panels join as their data acquires locators (P4.3). Renderer
  diff is net-negative (-65 lines) despite the two new helpers.
- 2026-06-10 (/approve follow-up, #380): the dead approve/dismiss links
  noted at P1.3 are live — GET /approve on comments_server calls the
  approve CLI's newly-extracted shared core (approve_and_apply /
  dismiss_action: same ledger/sizing side effects, one code path), then
  303s back to the Referer surface (path+query only) or /feed; card hrefs
  made absolute so all three surfaces resolve identically. State-changing
  GET ⇒ the route carries its own same-site guard (cross-site Referer /
  Sec-Fetch-Site → 403; no-Referer stays usable). Fixed en route: a stale
  double-approve no longer appends a duplicate row to the append-only
  ledger (status pre-check before the downstream dispatch — latent in the
  CLI). P4.4/P6.1 no longer owe this.
- 2026-06-11 (P4.2, #382): hide-don't-stub drew the line at *informative
  absence* — cold-data panels (macro β, targets, leases, verdict ledger,
  prompt quality, alignment, unparsed print-vs-guide, unaligned highlights)
  hide outright, while absences that ARE facts keep the collapsed empty
  state (customer concentration "none ≥ 5%", Q&A quarter not parsed,
  validation cannot-tie, budget-forgone). The Governance → Coverage matrix
  (sections sidecar + live table probes, suppressed = n/a) is where hidden
  gaps stay accountable. Peer comp: kept the panel (not dropped) but
  selection is now scored — thesis-watchlist rival > industry > sector,
  ± scale, zero-affinity dropped, basis shown per row; payload-identity
  fallback covers rivals outside the cached-profile universe (ITUB). Also
  repaired stable-API field drift (mktCap/revenueTTM/roicTTM v3 keys) that
  had been blanking every peer metric since the FMP cutover.
- 2026-06-11 (P4.3, #384): the phase also closed the chip/viewer debt P3.5
  left noted — CellSource gained doc_id (the P3.3 mapping dropped
  source_doc_id) so chips deep-link `/source/<doc_id>` (locator-sharpened:
  transcript_line → #L<n>, section → ?section=); the drawer's
  transcript_line citations resolve through a transcript_docs map that
  load_brief_provenance now bundles (period parsed from processed-transcript
  file names; no call-site changes; unresolvable periods stay plain text).
  Cross-links are deterministic header links via the P4.1 anatomy's new
  `links` slot + a data-xtab JS handler (tab switch + scroll + flash) —
  themes↔failure-modes, valuation↔KPI ledger, signals→news,
  concentration→bear. No LLM-matched linking; pair-level links only.
- 2026-06-11 (P4.4, #387): all three resurfacing paths read analyst_notes
  with the same lead-kind ordering (watch, question, then the rest):
  digest "Open items" section + per-name prep notes under Upcoming
  (3 + overflow), the workspace "Open watch-items" strip under the thesis
  lede (render-time via load_workspace_p3_panels.open_notes; hidden when
  the journal is empty), and a "Related notes" drawer section fed by
  load_brief_provenance's payload (now sources_used + transcript_docs +
  open_notes — the per-ticker alert-context bundle; still zero call-site
  changes). Resolved/archived notes never resurface. Notes stay read-only
  on every one of these surfaces until the P4.5 journal UI.
- 2026-06-11 (P5.1, #389): the ViewSpec contract accepts metric refs as
  objects OR compact tokens (fin:revenue / kpi:ROE /
  seg:product:AWS:revenue) — one parser serves the builder UI's option
  values and the future P5.2 compiler output. Cross-ticker alignment is by
  CALENDAR bucket derived from fiscal period_end (offset fiscal calendars
  land in the quarter they ended in; labels can differ from issuer fiscal
  names) and transforms look back by calendar key, so series gaps yield
  empty cells, never misaligned ratios. Chips pair value + provenance from
  the SAME tier-aware winning row via two new with-provenance loaders
  (closing the wave-4 seam: kpi cells now carry doc_id on this surface);
  segment cells stay unchipped (period-level provenance — wire when a
  surface needs it). The P3.3 chip anatomy moved to ui/source_chip.py,
  re-imported by the workspace renderer under its original private names.
  MAX_TICKERS=16 so the whole-portfolio prefill runs first-click.
  saved_views (0079) upserts by (user_id, name) — the name is the stable
  embed handle; GET /api/views/<id>/fragment is the embed hook.
- 2026-06-11 (P4.5, #388 — wave 4 complete): the journal's lifecycle home
  is Research → Journal (/api/panel/journal + /api/notes REST, thin over
  user_state.notes; supersede = chained replacement, never an edit). The
  "add note" capture deliberately rides the comment sidebar's existing
  per-section anchors but POSTs straight to /api/notes (kind picker,
  source="manual") — the comments→intent→processor pipeline is untouched,
  so a journal capture never triggers a brief edit. Capture works on any
  data-commentable section of the workspace report; portfolio-level notes
  come from the Journal tab's own form (blank ticker).
