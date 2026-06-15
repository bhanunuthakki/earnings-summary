# Concepts Spine Decision (S8)

**Status:** decided — **DEFER** the cross-ticker concepts spine; ship a cheap
no-migration interim instead; revisit the full spine only on a concrete
dogfooding trigger (§9).
**Owner:** bhanu · **Session:** S8 (exploration/decision) of the
`capture_every_number` program · **Model:** Opus
**Companion directives:** `capture_every_number_program.md` (§1 locked this as
S8's question), `reference_platform_invariants` (FK-poisoning), `data_provenance.md`.

> The owner asked to "spawn a session to thoroughly explore this." This file is
> the deliverable: a recommendation with the evidence behind it, the concrete
> cross-ticker queries the spine *would* enable, the cheaper interim that
> captures most of the value, and an explicit trigger + sequenced plan for the
> full spine if it's ever warranted. The prior default was a flat "defer"; this
> pressure-tests it and lands on a sharper answer — **defer the spine, ship the
> cheap mitigation, gate the spine on a real signal.**

---

## 1. TL;DR

- The concepts spine (`concepts` / `concept_aliases` / `concept_definitions`,
  alembic 0036, + the nullable `concept_id` FK columns, alembic 0037) is **built
  but 100% dormant on the concept half**: a full CRUD API exists
  (`src/entity_store.py`) but **has zero production callers** — only tests touch
  it. **No row is ever written** to `concepts`, and **no fact-table `concept_id`
  is ever populated or read in a join.**
- The program's three stated goals (queryable via **ask**, **raw DB**, **DIY
  picker**) are all **per-ticker** and are fully met by `kpi_facts` + S1's
  per-ticker `canonical_metric_name`. **The spine is not on the critical path.**
- The one capability the spine uniquely unlocks is **cross-ticker metric
  identity** — "compare *the same* operating KPI across different holdings."
  There **is** a live consumer that wants this (the multi-ticker DIY ViewSpec
  builder + `metric_catalog`), and it currently does cross-ticker KPI alignment
  by **fragile exact-name-string coincidence**. Capture-all (S3/S4) makes that
  weakness worse.
- **But** the highest-value cross-ticker comparisons (revenue, margins, ROE,
  FCF, multiples) **already align fine** — financial line items use
  FMP-normalized strings, and the peer-comp table uses FMP `ratios-ttm`. The
  KPIs where the spine helps are the *bespoke* operating metrics (NIM, ARPAC,
  take rate, NPL ratio) — which are exactly the metrics where a naive
  cross-ticker join is **semantically dangerous** (per-company definitions
  differ; `concept_definitions` exists precisely because they do).
- **Recommendation:** **DEFER** the spine. Ship a cheap, reversible,
  no-migration mitigation (cross-ticker name-normalization in `metric_catalog`,
  routed to **S5**) that fixes the surface-variant majority without any backfill
  or curation burden. Wire the full spine **only after capture-all (S3/S4)** and
  **only if** dogfooding surfaces real cross-ticker friction (trigger in §9). A
  false cross-ticker merge poisons every ticker at once (FK-poisoning invariant)
  and the owner's current posture is "stop building, dogfood."

---

## 2. What's built vs. what's wired (the evidence)

### 2.1 Schema (alembic 0036 — `entity_concept_spine`)

Nine tables. The three **concept** tables (the subject of this decision):

| Table | Key columns | Purpose |
|---|---|---|
| `concepts` | `id`, `kind` (`financial_line_item`/`kpi`/`ratio`/`segment_metric`), `canonical_name` (UNIQUE), `unit_kind`, `taxonomy_xbrl_tag`, `generic_definition_md`, `computation_kind`, `computation_formula_md` | One canonical row per metric *concept*, ticker-agnostic. |
| `concept_aliases` | `id`, `concept_id`→concepts, `ticker` (nullable = universal), `alias_text`, `alias_kind`, `confidence` | Every reported surface form → concept. `ticker`-scoped aliases win over universal. |
| `concept_definitions` | `id`, `concept_id`→concepts, `ticker` (NOT NULL), `effective_from/to`, `definition_md`, `computation_change_md`, `superseded_by_id` | Per-(concept, ticker, period) definition history — "NU redefined Active Customers in Q3'24." |

Supporting infra also in 0036 and **also dormant for concepts**:
`mapping_proposals` (the self-updating queue with `auto_apply_threshold=0.85`,
`pending_review` 0.50–0.85, reject <0.50) and `extractions` (provenance rows).

### 2.2 The `concept_id` FK columns (alembic 0037 — `facts_concept_fks`)

All nullable, **no SQLite FK enforcement** (added via `ALTER TABLE`; integrity
is "maintained at the writer layer" per the migration docstring — but there is
no writer). Inventory and live status:

| Column | Added by | Indexed | Written by prod? | Read in any join? |
|---|---|---|---|---|
| `kpi_facts.concept_id` | 0037 | `idx_kpi_facts_concept` | **No** | **No** |
| `financial_facts.line_item_concept_id` | 0037 | `idx_financial_facts_concept` | **No** | **No** |
| `management_commitments.kpi_concept_id` | 0037 | `idx_commitments_concept` | **No** | **No** |
| `segment_facts.segment_entity_id` | 0037 | — | n/a — **table dropped** | n/a |
| `predictions.kpi_concept_id` | 0038 | — | **No** (plumbed as optional param, always `None`) | **No** |
| `document_numeric_facts.kpi_concept_id` | 0040 | — | **No** | **No** |

Grep proof: every reference to `*.concept_id` / `kpi_concept_id` /
`line_item_concept_id` outside the migrations lives in `entity_store.py` (the
dormant API operating on `concept_aliases`/`concept_definitions`, not on fact
tables) or in tests. `resolve_concept()` has **zero production callers**.

> Note the 0037 `segment_facts.segment_entity_id` column was carried into
> `segment_dimensions.segment_entity_id` when `segment_facts` was dropped
> (alembic 0057) — and **that** column **is** backfilled and live. See §2.3.

### 2.3 The entity half IS partially live — the concept half is not

This is the most telling fact. The **entity** side of the same spine
(`entities` / `entity_aliases` / `entity_relationships`) **is populated and
used** for segments:

- `execution/seed_entity_graph.py` seeds company/segment/product/competitor
  entities from `src/entity_seed.py` (hand-curated, ~11 portfolio + ~63
  watchlist tickers) and **backfills `segment_dimensions.segment_entity_id`** by
  resolving `dim_name` against `entity_aliases` (deterministic; unresolved →
  `mapping_proposals` at confidence 0.45).
- So the seed + alias + backfill + proposal-queue pattern is **proven to work**
  — the machinery to light up the concept half cheaply already exists and has a
  reference implementation.

**Yet ~8 months after 0036/0037 (Create Date 2026-05-24), nobody wired the
concept half.** That is a weak but real revealed-preference signal: the
cross-ticker *metric-identity* need has not bitten hard enough to pull the infra
into use, even though the directly-analogous entity-identity need (segment
attribution) did. The owner built the spine speculatively, used the half that
solved a felt problem, and left the other half dark.

---

## 3. What the spine would unlock that S1's per-ticker canonicalizer cannot

S1's `canonical_metric_name` (`src/compute/kpi_resolver.py`) is deliberately
**per-ticker**: it folds unit/casing/whitespace variants of *one ticker's* labels
onto *that ticker's* existing `kpi_definitions` rows, and is conservative by
design ("a false merge is worse than a duplicate"). It has **no cross-ticker
notion** — it never compares NU's labels to HDB's.

A wired concept spine adds exactly one thing: **a shared identity for the same
metric across different tickers.** Concretely, it would enable:

**(a) Robust cross-ticker KPI comparison in the DIY builder.** Today
`metric_catalog` (`src/viewspec/engine.py:271`) builds the KPI picker with
`GROUP BY kd.name` + `COUNT(DISTINCT kf.ticker)`. A token only shows
`tickers: N` when *N tickers literally store the same name string*. With a
concept join it would group by `concept_id`:

```sql
-- TODAY (fragile): "Net interest margin" and "Net Interest Margin (annualized)"
-- and "NIM" are THREE separate tokens, each tickers:1.
SELECT kd.name, COUNT(DISTINCT kf.ticker) AS n
FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
WHERE kf.ticker IN ('NU','HDB','IBN','JPM') GROUP BY kd.name;

-- WITH SPINE: one "NIM" concept, tickers:4 — a single comparable row.
SELECT c.canonical_name, COUNT(DISTINCT kf.ticker) AS n
FROM kpi_facts kf JOIN concepts c ON c.id = kf.concept_id
WHERE kf.ticker IN ('NU','HDB','IBN','JPM') GROUP BY c.id;
```

**(b) Peer screens on a shared operating KPI** (not just FMP ratios): "rank my
bank holdings by NIM trend," "ARPAC growth across NU / MELI-fintech / Inter,"
"net retention across NOW / VEEV / RBRK / WIX." Today the peer-comp panel
(`src/compute/peer_selection.py` + `report.sections.p3_data.load_peer_comp`)
compares peers on **FMP-standardized fundamentals** (`profile` +
`key-metrics-ttm` + `ratios-ttm`) — it cannot put the platform's *own* extracted
operating KPIs side by side because they have no shared key.

**(c) Cross-ticker ask** ("which of my banks has the best NIM trend?") that
resolves the metric name once → concept → all tickers' series, instead of
`_fact_evidence` (`src/ask/grounding.py:661`) matching the question phrase
against *each ticker's own* definition labels independently.

**(d) Definition-divergence footnotes** — `concept_definitions` would let a
cross-ticker view auto-annotate "NU's NIM is computed on X; HDB's on Y" so the
comparison is honest rather than silently apples-to-oranges.

**What it does NOT unlock (already solved without it):** cross-ticker comparison
of **financial line items** (revenue, net income, FCF, margins). Those align
today because `financial_facts.line_item` carries FMP-normalized strings that are
already identical across tickers (`metric_catalog`'s `fin` domain groups by
`line_item` and routinely shows `tickers: N`). The single most common
cross-ticker comparison need is therefore **already met**.

---

## 4. The semantic trap (why naive cross-ticker KPI joins are dangerous)

The metrics the spine would help with are precisely the ones where a shared
identity is *least* safe:

- **NIM** for NU (a Brazilian digital bank, NIM on a credit portfolio) vs **NIM**
  for HDB (an Indian universal bank) vs JPM (US money-center) are computed on
  different bases. Memory `nu_capital_adequacy_car` already records that NU's
  "capital adequacy ratio" is Nu Pagamentos prudential CAR, **not** group CET1 —
  a cross-ticker join to other banks' CET1 would be flatly wrong.
- The platform's entire ethos (`feedback_break_rules_business_model`,
  S1's invariant) is that **false equivalence is a correctness bug**, and on the
  *write* side S1 chose "duplicate over false merge." A cross-ticker concept is a
  *deliberate* merge across tickers — the highest-leverage place to get it wrong.
- **FK-poisoning blast radius:** a per-ticker bad merge corrupts one ticker's
  series (recoverable). A bad *concept* assignment silently mis-joins **every**
  ticker mapped to it, and every read path that trusts `concept_id` inherits the
  error. This is the platform's documented dead-end class
  (`reference_platform_invariants`).

So the spine's value (cross-ticker comparison) and its risk (false cross-ticker
equivalence) are the same surface. That argues for *curated, conservative,
opt-in* concept assignment — never bulk auto-merge — which raises the cost.

---

## 5. Read-path rewiring cost (if the spine is wired)

Three consumers would change to *use* `concept_id`. None is wired today; all key
on `(ticker, name)` strings.

| Consumer | File:fn | Today | Change to use concept | Effort |
|---|---|---|---|---|
| DIY catalog | `viewspec/engine.py:271` `metric_catalog` | `GROUP BY kd.name`, count distinct ticker | `LEFT JOIN concepts`, group by `concept_id` (fall back to name when null) | S–M |
| DIY view exec | `viewspec/engine.py:173` `execute_view` + `_load_row_data` + `timeseries/loaders.py:884` `load_kpi_series_with_provenance` | resolve `WHERE kd.name = ?` per ticker | resolve a concept token → per-ticker definition → series | M |
| Ask facts | `ask/grounding.py:661` `_fact_evidence` | per-ticker label-core phrase match | match question → concept → all tickers' defs for that concept | M |

The rewiring is **additive and gated** (every query falls back to the name path
when `concept_id IS NULL`), so it can ship behind the backfill incrementally.
Total read-path work ≈ **L** (one focused session). The harder/riskier work is
the backfill and curation (§6), not the read paths.

---

## 6. Backfill cost / risk

The job: assign a `concept_id` to every existing `kpi_definitions` row (the FK
lives on `kpi_facts`, but the natural unit of assignment is the per-(ticker,name)
definition). Three tiers of approach, in increasing cost:

1. **Deterministic surface-variant clustering** (cheap, safe, *incomplete*).
   Cluster definitions whose `normalize_kpi_name` (kpi_resolver) collapses to the
   same key *across* tickers. Catches "Net interest margin" ≈ "Net Interest
   Margin (annualized)". **Misses true synonyms** ("NIM" vs "Net interest
   margin") — which is the part with real cross-ticker value *and* the part with
   highest false-merge risk. Roughly the same logic as the §7 interim, just
   persisted into `concepts` instead of computed at catalog-time.
2. **LLM-assisted mapping** (the designed path; infra exists). Feed each distinct
   normalized label to an LLM "which canonical concept is this?" → write a
   `mapping_proposals` row (`new_concept` / `new_concept_alias`). Auto-apply
   ≥0.85, queue 0.50–0.85 for review, reject below. **The queue, thresholds, and
   apply logic are already built** (`entity_store.propose_mapping` /
   `_apply_proposal_payload`). Cost = LLM calls (bounded, one-shot) + a
   **curation dashboard surface** (not built) + **owner review time** for the
   0.50–0.85 band.
3. **Manual curation** for the analyst tier-KPI registry (the watchlist's
   `tier_*_kpis`). Small, high-value, low-risk — these are already hand-named.

**Population scale.** ~74 tracked tickers. The analyst registry is O(hundreds)
of definitions today. Post-capture-all (S3/S4) the long tail grows to O(thousands)
— and crucially, capture-all mints *ticker-specific surface forms*, so the
cross-ticker fragmentation the spine fixes is exactly what capture-all multiplies.

**Risk.** The auto-apply ≥0.85 band is where false cross-ticker merges enter. The
ongoing cost is real too: the concept ontology becomes a **maintained artifact**
(new tickers, new metrics, redefinitions) — a recurring tax, not a one-shot.

---

## 7. The cheap interim (recommended now, routed to S5)

There is a way to capture the **surface-variant majority** of cross-ticker
alignment value with **no migration, no backfill, no curation, no new table** —
and it's reversible:

> In `metric_catalog` (and the matching `execute_view` resolution), group the KPI
> domain by `normalize_kpi_name(kd.name)` *across* tickers instead of by the raw
> `kd.name`, picking a display representative (most-observations / shortest
> clean name) and keeping the token resolvable per ticker via the existing
> lenient `resolve_kpi_definition_name`.

This makes "Net interest margin" and "Net Interest Margin (annualized)" collapse
into one cross-ticker comparable token in the picker — the same defragmentation
S1's read resolver already does *within* a ticker, lifted to the *catalog* axis.
It deliberately does **not** unify true synonyms ("NIM" vs "Net interest
margin") — that's the false-merge-risky part best left to a curated spine.

- **Effort:** S (a few hours). **Risk:** low, fully reversible (it's a `GROUP BY`
  expression change + a representative-picker; no data written).
- **Owner:** route to **S5** (DIY picker), which already edits `metric_catalog`
  (lifting `limit_per_domain`, type-ahead, origin badges). It's a natural rider.
- **It is NOT in S8's scope to ship** (S8's deliverable is this decision). S8
  recommends it; S5 implements if the owner agrees.

This is the pressure-test result: the prior "defer" was right about the spine but
left value on the table. The interim banks ~60% of the cross-ticker upside for
~5% of the cost and keeps the heavyweight option in reserve.

### 7a. Hard acceptance criteria (owner-confirmed 2026-06-15)

The owner confirmed two requirements after capture-all (S3/S4) shipped. They are
**acceptance criteria, not aspirations** — S5 is done only when all hold.

**Ask half — DONE (PR #623, merged 2026-06-15).** A typed metric name must
resolve a captured KPI regardless of (a) an `(annual)`/`(annualized)`
parenthetical, or (b) a different company reporting it under a slightly different
qualified name. `ask.grounding._fact_evidence` now matches the question against
the full core **OR** the post-last-separator **leaf** core
(`_label_match_keys`), since capture-all names are `section — axis — leaf` (the
`_build_name` format in `table_extractors/generic_xbrl_capture.py:471`,
separator `" — "`). The leaf is used only when it's a distinct ≥2-word phrase so
a generic single-word leaf (`Total`/`Net`) can't flood. Per-ticker independent
matching covers the cross-company case. Tests in `tests/test_ask_grounding.py`.

**DIY-picker half — for S5.** Acceptance criteria:

1. **Free-flowing, DB-driven, no FMP preset.** The picker for a selected ticker
   set must list **everything stored in the fact tables** for those tickers —
   `financial_facts.line_item`, every `kpi_facts` definition (incl. the S3/S4
   captured long tail from IR decks / investor days / 10-K / 10-Q), and segment
   slices. This is **already true** structurally (`metric_catalog` reads the live
   tables, never an FMP allowlist) — S5 must **not regress** it and must verify
   the captured `origin='capture'` rows actually surface.
2. **No silent truncation of the long tail.** Lift `limit_per_domain=300`
   (`viewspec/engine.py:275`) and the NL caps so a metric-rich ticker (post-
   capture GOOG ≈ 880 KPI facts) isn't cut off. If any bound remains, `log()` /
   surface what was dropped — never present a truncated list as complete.
3. **Cross-ticker + within-ticker de-fragmentation.** Group the KPI domain by a
   normalized key across tickers so surface variants collapse into ONE comparable
   token. The normalization must **mirror the ask leaf logic** (peel a
   `section — axis —` qualifier to its leaf, then `normalize_kpi_name`) so the
   picker and ask agree on what "the same metric" means. Pick a display
   representative (most-observations / shortest clean leaf); keep the token
   per-ticker resolvable via `resolve_kpi_definition_name`.
4. **Conservative, like S1's write path.** Collapse only unit/casing/whitespace/
   qualifier-prefix variants. Do **not** unify true synonyms (`NIM` vs `Net
   interest margin`) — that's the false-merge-risky part reserved for the curated
   spine (§9). A duplicate token is acceptable; a false merge is not.
5. **Type-ahead search over the full (uncapped) set**, so the long tail is
   reachable even when not in the top-N by ticker-count.

These criteria deliberately stop short of the full spine: they give the owner
"search/pick anything in the DB, and variants don't fragment it" **without** a
migration, backfill, or curated cross-ticker ontology.

---

## 8. Interaction with capture-all (before or after?)

**After.** Two reasons:

1. **Canonicalize once, not twice.** Wiring the spine before S3/S4 would
   backfill concepts over a half-populated namespace, then need re-running once
   capture-all floods in the long tail. Do the (expensive, LLM + curation)
   backfill against the *final* population.
2. **Capture-all is what makes the gap bite.** Pre-capture, the KPI namespace is
   mostly the curated analyst tier (reasonably consistent names). Post-capture,
   ticker-specific surface forms proliferate → cross-ticker fragmentation becomes
   visible in the DIY builder. That visibility is the trigger signal (§9), not a
   reason to pre-build.

So capture-all makes the spine **more valuable** (more genuine cross-ticker
metrics to unify) **and more painful** (more to backfill). The resolution: the
cheap interim (§7) rides along with S5 regardless; the full spine waits until
after capture-all *and* a real signal.

---

## 9. Recommendation & trigger condition

**DEFER the full concepts spine.** Ship the §7 interim (via S5). Do not backfill
`concept_id` columns in the `capture_every_number` program (consistent with the
program directive §1).

**Revisit the full spine when ALL of these hold (the trigger):**

1. **Capture-all has shipped** (S3 + S4 merged; the long tail is in `kpi_facts`).
2. **A real cross-ticker friction is observed in dogfooding** — e.g. the owner
   reaches for a cross-ticker KPI comparison in the DIY builder or ask and gets
   fragmented/missing alignment that the §7 normalization interim did **not**
   fix (i.e. a true-synonym case like "NIM" vs "Net interest margin," not a mere
   surface variant).
3. **The need recurs** (≥ a handful of distinct such metrics, not a one-off) —
   enough to justify a maintained ontology over a one-time manual fix.

Absent (2)+(3), the spine stays dark: the per-ticker canonicalizer + the §7
interim meet the program's goals, and the maintenance tax isn't earned.

**If/when triggered, the sequenced plan (its own ~3–5 session program):**

| Step | Work | Effort | Risk |
|---|---|---|---|
| C1 | Concept seed: extend the `entity_seed.py`/`seed_entity_graph.py` pattern to seed `concepts` + universal `concept_aliases` for the curated tier KPIs (manual, high-value). | M | low |
| C2 | Backfill driver: deterministic cross-ticker normalize-cluster → auto-assign; remainder → `mapping_proposals` (infra exists) for LLM + review. | L | med (auto-apply band) |
| C3 | Curation dashboard tab for `pending_review` proposals (the 0.50–0.85 band) — reuses `entity_store.pending_proposals` / `decide_proposal`. | M | low |
| C4 | Read-path rewiring (§5): `metric_catalog` + `execute_view`/loaders + ask `_fact_evidence`, all `concept_id IS NULL`-tolerant fallbacks. | L | low |
| C5 | Cross-ticker definition footnotes from `concept_definitions` so comparisons are honest about methodology divergence. | M | low |

Sequence is C1→C2→C3 (populate + curate) then C4→C5 (consume) — populate before
read paths trust the column. Schema is **already in place** (0036/0037); no new
migration is required beyond possibly a partial index — wiring is code + data,
not DDL.

---

## 10. Report back to the program owner (one paragraph)

The concepts spine is built but fully dormant on the concept half (no writers, no
read joins; only the entity half — segment attribution — is live). It is **not**
needed for the three `capture_every_number` goals, which are per-ticker and met
by S1's canonicalizer. Its one unique payoff is cross-ticker *operating-KPI*
identity — wanted by the already-multi-ticker DIY builder, which today aligns
KPIs by fragile exact-name match — but the highest-value cross-ticker
comparisons (financials/ratios) already align via FMP-normalized strings, and the
KPIs the spine would help with are the ones where a naive cross-ticker join is
semantically dangerous and FK-poisoning's blast radius is widest. **Decision:
defer the spine; ship a cheap no-migration normalization in `metric_catalog` (fold
into S5); revisit the full spine only after capture-all (S3/S4) lands AND
dogfooding shows recurring true-synonym cross-ticker friction the interim
doesn't fix.** Sequenced plan and trigger above; schema needs no new migration if
it's ever lit up.
