# Improvement roadmap — research companion & investment advisor (2026-06)

Source: the 2026-06-10 five-surface audit (dashboard, workspace report, design
language, provenance, comment/chat loop) against the owner's brief:

> Deep fundamental research into companies; surface interesting new companies;
> deeply grounded auditable data that can be sliced and diced on command;
> understand opportunity costs and portfolio positioning; record thoughts on
> provided advice persistently. A research companion and investment advisor
> that gets better over time.

Working rules: one PR per phase; every phase independently shippable and
tested; no phase blocks daily use of the app. Tracks are orthogonal — order
within a track matters, order across tracks is a choice (recommended sequence
at the bottom).

---

## Track D — Persistent analyst memory  *(in flight)*

The system could apply the analyst's thoughts but not remember them: comments
and chat died with each report build, and no LLM call consulted prior thinking.

- **D1 — durable notes substrate. SHIPPED (#356, prod migrated + backfilled
  2026-06-10).** `analyst_notes` (alembic 0074): semantic kinds
  (question / decision / watch / assumption / observation), supersede-not-
  delete, anchored to objects, auto-mirrored from every comment write,
  backfill CLI.
- **D2 — analyst priors in every prompt. THIS PR.** `load_priors_anchor`
  (open notes, grouped, cache-stable) composed as the 4th anchor block into
  the 6 anchor assembly points (per-quarter summary/pairwise/SayDo filter via
  workspace_data, recent developments, earnings_tone, material_news,
  kpi_inflection, saydo_due) + the report chat system prompt; chat also
  carries the tail of the previous build's thread.
  *Acceptance:* a question asked last build is visible to this build's
  prompts; chat references prior discussion unprompted.
- **D3 — event resurfacing.** Digest gains an "open items" panel (questions /
  watch-items by ticker, age-sorted). Earnings-prep (saydo_due) and new report
  builds lead with the analyst's open items for that name. Alerts attach
  relevant open notes to their evidence drawer (note ↔ alert by anchor_key /
  KPI name).
  *Acceptance:* on the next NU print, the report header and digest both show
  the open NU watch-items without being asked.
- **D4 — notes panel UI.** Command-center "Journal" tab: list/filter by
  kind/status/ticker, resolve / reclassify / supersede actions
  (`/api/notes` endpoints on comments_server), plus "add note" capture from
  any report section (reuses comment anchors, kind-first instead of
  intent-first).
  *Acceptance:* full note lifecycle possible without touching the DB or CLI.

## Track A — Holdings-first cockpit + design foundation

The landing surface is an ops console (fetch/build status + maintenance
forms); investment signal lives 2+ clicks deep across 12 tabs.

- **A1 — design tokens + formatting module.** One shared token set (colors —
  including ONE semantic good/bad pair, typography scale, spacing) consumed by
  every surface (workspace_styles, _styles, shell CSS, analytical inline CSS);
  one formatting module (money via the #355 helper, percent vs pp, bps,
  dates, relative time) replacing the ad-hoc f-strings; favicon + title
  convention. Mechanical sweep; zero behavior change.
  *Acceptance:* same value renders identically on every surface; one place to
  change any color/format.
- **A2 — cockpit Overview.** One dense row per holding answering (owner's
  picks, 2026-06-10): **thesis health** (verdict badge, breach/watch, tier-1
  KPI deltas), **valuation** (price & day move, DCF fair-value gap / MoS,
  PEG where applicable), **events** (next earnings date from
  expected_earnings, unreviewed alerts count, new docs since last visit).
  Ops controls move to a dedicated Ops tab. Evaluation list gets the same
  row, thinner.
  *Acceptance:* the question "which holding needs my attention today?" is
  answered by the landing screen alone.
- **A3 — tab consolidation.** 12 tabs → ~6: Cockpit / Portfolio / Holding /
  Signals (triggers + pre-reads + insiders + predictions) / Journal
  (decisions + thesis ledger + notes) / Ops (IR docs, data cache, LLM spend,
  maintenance).

## Track B — Report cohesion

Nine-plus sections render "cold ticker" stubs with alembic/CLI text; three
table classes, two collapse idioms, staleness shown on ~4 of ~14 LLM
sections; almost no cross-links.

- **B1 — shared section chrome.** Every panel: same header anatomy (title ·
  as-of stamp · source chip), analyst-language one-line empty states (no
  migration numbers in reports — "how to fill" hints move to Ops), empty
  sections collapse instead of stubbing, one table class, one collapse idiom.
- **B2 — coverage-driven composition.** Per-ticker section coverage report;
  fill-or-hide policy per section; tabs with nothing real to say fold into
  neighbors. Includes the peer-comp quality fix (UBER note 2026-06-07: peers
  are wrong — pick comparables from profile/industry + holdings overrides,
  or drop the panel).
- **B3 — cross-links.** Earnings themes ↔ bear case; valuation rationale ↔
  thesis KPI drivers; §3.5 signals ↔ news items; customer concentration →
  bear case; recent-decisions sidebar → Decisions tab anchors.

## Track C — Provenance to CapIQ/BamSEC standard

Storage already has tiers, sha256'd documents, restatement chains,
as-of-date loaders. Missing: locators (where IN the document) and any
per-number UI affordance.

- **C1 — locator schema.** `documents` gains `accession_number` +
  `filing_date` (backfilled from source_url / FMP metadata); fact tables gain
  a nullable `locator` JSON (10-K section key, transcript line ids, PDF page,
  FMP JSON path). Backfill what is derivable (FMP path from doc_type +
  period; 10-K section keys exist in the parsed form_10k JSONs).
- **C2 — extractor wiring.** All fact writers populate locators going
  forward; `source_excerpt` populated systematically on transcript- and
  filing-sourced KPI facts.
- **C3 — per-number source UI.** Source chip on rendered numbers
  (Financials + Earnings tabs first): hover = tier + fetched-at; click =
  source drawer with the excerpt and an "open source" link (EDGAR URL /
  local doc). Fix the alert evidence drawer's dead citation kinds
  (transcript_line, filing_section render "—" today).
- **C4 — source viewers.** In-workspace transcript reader with line anchors
  (speaker turns already stored); 10-K section reader over the parsed JSONs;
  Sources tab gains tier / URL / fetch-status columns; restatement
  "was X, now Y" comparison on superseded facts.

## Track E — Wave 2 (rides on C + D)

- **E1 — slice-and-dice query surface.** A query box on the dashboard:
  natural language → SQL over financial_facts / kpi_facts / segments
  (read-only, schema-constrained), rendered as table + chart with source
  chips (needs C). "Show NIM by quarter for NU vs MELI since 2023" without
  leaving the app.
- **E2 — discovery/screener.** The eval-flavor pipeline becomes a funnel:
  screen (factor filters over the facts layer) → auto-onboard top names →
  eval brief → "interesting because" memo referencing the portfolio's
  existing exposures.
- **E3 — opportunity-cost & positioning.** Cross-holding view: sizing vs
  conviction (sizing intents + decisions) vs valuation gap (DCF/PEG) vs
  thesis health; pairwise "swap test" memos ("$1 added: NU or MELI?")
  surfaced in the Portfolio tab (needs live tracker API).

## Recommended sequence

D2 (this PR) → **A1+A2** (the daily-use win; A1 first makes every later UI PR
cheaper) → **D3** (memory starts resurfacing on its own) → **C1–C3** (the
trust moat; C4 as polish) → **B1–B3** → **A3** → **D4** → **E1→E3**.
Re-order freely between tracks; within a track, order is load-bearing.
