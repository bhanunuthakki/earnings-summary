# Disclosure Intelligence v1 — Ratified PRD

Ratified 2026-07-25 by the owner, from the adversarial review of the
2026-07-24/25 build session. **This document SUPERSEDES every other plan from
that session** — `disclosure_change_build_stack.md` (what was built),
`disclosure_program_ruling.md` (evidence standard; still authoritative for the
standard itself), and `disclosure_gap_scoping.md` (feasibility input to D2).
Where they conflict with this document, this document wins.

## Owner rulings incorporated (2026-07-25)

1. **Universe:** narratives are company-specific; the tracked book stays the
   core. Cross-company analysis is IN SCOPE as *industry/peer-group
   comparison* (via the platform's existing comp-sets/peer machinery), NOT as
   a wide-index screening layer. No S&P 500 build.
2. **FMP Starter:** still pending. Design around EDGAR-only FPI annuals; keep
   the honest 403 coverage rows; revisit when purchased.
3. **P4 non-answer rate: DROPPED.** Not "fix until it reproduces ~11%" —
   dropped. The construct never reproduced Gow/Larcker/Zakolyukina and the
   owner ruled against maintaining a homegrown evasiveness metric. The
   transcript *substrate* (92.9% attribution) stays — it serves D2's tone and
   role-separation work.
4. **Surface order confirmed:** Ask + workspace first; feed chips only for
   high-materiality events.

Standing rules (from the ruling, unchanged): published research is the
evidence gate — never in-sample validation on the owned book; deterministic
first, LLM for judgment only; every event carries a verbatim receipt; absence
is never silent; drift-not-announcement framing on every surface.

Acceptance criteria are **rows/artifacts verified on prod** — never "merged".
Two audits this session found merged, green, scheduled features that had never
executed once.

---

## D0 — Close running work

| # | Requirement | Acceptance |
|---|---|---|
| D0.1 | 10-Q ingestion completes (agent in flight); detectors re-run over new sections. Event-study re-run is CANCELLED (P5 parked; in-sample validation is not a gate). | 10-Q section counts per ticker reported; `disclosure_events` refreshed |
| D0.2 | Merge #1031 (gap scoping) after flake rerun | MERGED |
| D0.3 | Session LLM cost pulled from `llm_calls` for the new purposes | one table, actual dollars |
| D0.4 | PIP-PRD residuals stay tracked separately (Telegram delivery test, inbox doorway task_b7ed2cbb, stale-directive edits pending owner authorization) | listed, not silently dropped |

## D1 — Correctness & data completeness (agent: **D**, sonnet)

| # | Requirement | Acceptance |
|---|---|---|
| D1.1 | NVO 20-F partition handled (thin incorporate-by-reference layout; boundary-suspect since day one) | NVO `risk_factors` timeline queryable on prod |
| D1.2 | WIX 6-K validated per the `sec_6k_fetch` spike protocol (fetch one live 6-K, inspect index, confirm real narrative HTML) → quarterly narrative lane or honest confirmed-negative | WIX quarterly sections exist OR coverage row says why not |
| D1.3 | RGEN FY2023 "(Restated)" TOC edge fixed in the 125-doc corpus harness | 0/97 business sections TOC-contaminated |
| D1.4 | FYE-aware period bucketing resolves the AVGO/DHR/LITE/NVDA cross-source mismatches | reconciler reports 0 findings for those four |
| D1.5 | stockanalysis.com / tickertrends.io transcripts (11/308) moved off the old flattening parser | attribution coverage reported for those sources |
| D1.6 | **P4 non-answer construct removed** (owner ruling 3): scoring path deleted, its `LLM_MODELS` purpose retired if unshared, ruling row updated to DROPPED. Tone judgment is KEPT (D2 dependency). No transcript non-answer events ever wrote to prod — verify, no purge needed | grep-clean; tests updated |

## D2 — Evidence-first signals, in evidence order (agents: **F** filings, **T** transcripts)

| # | Signal | Evidence | Build |
|---|---|---|---|
| D2.1 | **Guidance withdrawal** | Zhou/Zhou 2020 (−41bps); Chen/Matsumoto/Rajgopal 2011 (−4.8%) | Generalize `metric_lifecycle` to a second subject family (per #1031: same detector, new candidate feed from `management_commitments` + MD&A guidance headings). Emits `guidance_withdrawn` events |
| D2.2 | **Detrended doc-level YoY similarity** (the Lazy Prices construct) | Cohen/Malloy/Nguyen 2020 | Aggregate item-grain Jaccard to canonical-section grain; quintile against (a) same-period tracked book AND (b) **peer/comp-set group** per owner ruling 1. Emits per-ticker score + percentile |
| D2.3 | **ABTONE** — abnormal tone | Huang/Teoh/Zhang 2014 | Deterministic residual fit over the already-cached tone score (zero new LLM calls). Rename/fix `tone_shift_is_abnormal`, whose docstring admits it is not the construct |
| D2.4 | **CFO-vs-CEO separation** | Larcker/Zakolyukina 2012 (CFO tradeable, CEO not) | PARSE the CORPORATE PARTICIPANTS roster (currently deleted as noise by `strip_document_artifacts`) → per-role tone deltas. Explicitly NOT claiming the L&Z deception model |

Acceptance for each: typed events with verbatim receipts on prod, for the
tracked book.

## D1e — Ground truth for the judgment layer (agent: **E**, sonnet)

Hand-label ~50 events across `metric_lifecycle_triage` and
`disclosure_item_specificity_triage`; register golden sets per the
four-registries-in-lockstep recipe; report measured verdict accuracy.
**Until this lands, no verdict statistic (including "5 concealment / 5,954")
may be cited as a finding.** Transcript-purpose evals follow after D2.3/D2.4
settle the purposes.

## D3 — Proactive operation (after D1/D2; needs owner directive authorization)

New-accession trigger: ingest sees a new filing for a tracked name →
detectors run for that ticker; plus a weekly sweep. Registered in
`llm_quota_scheduling.md` with the per-item degrade pattern, clear of
protected windows (04:00 PT pipeline, Sun ~10:30 PT rungs). Patch text will be
proposed for authorization — directive edits are not covered by this PRD's
execution approval. *Accept: a new filing produces events with no human in the
loop.*

## D4 — Surfaces (after D2; order per owner ruling 4)

1. **Ask grounding** — "what changed in NU's risk factors" answers from
   `filing_section_items`/`disclosure_events` with verbatim quotes; supports
   peer-group comparison ("across LatAm fintech peers") via comp-sets.
2. **Workspace strip** — per-ticker disclosure-change panel, drift-framed
   (relevant for weeks, never a day-of alert), kit components, receipts,
   `tests/test_ui_controls.py` + golden gates.
3. **Feed chips** — high-materiality events only, per the feed-density
   standard.

## D5 — Explicitly out / parked

- P5 event study: parked; code kept; no cell citable; no re-runs.
- P4 non-answer: dropped (D1.6).
- Wide-index screening: out (owner ruling 1).
- Vocal affect: out of reach text-only; never proxied from disfluencies.
- FMP FPI refresh: blocked on the pending Starter purchase.

---

# Status After Execution & Open Items (2026-07-25)

Execution complete through D2. All 16 program PRs merged; closing pass run
against prod as the single writer, verified end state: **35,570 sections / 45
tickers / 39,964 items / 35,486 disclosure events** across 13 event types.
Acceptance spot-checks: NVO risk_factors restored (3 sections), business TOC
contamination 0/110 (was 93/97), concealment verdicts 5 → 2 after the
mandatory-GAAP suppression stage.

Closed by adjacent sessions since ratification: mobile-inbox doorway (#1044,
merged), transcript period-duplication root cause with schema-enforced
uniqueness (#1048, merged — supersedes the read-time dedup as the primary
guard).

## Open items

| # | Item | Owner / next action |
|---|---|---|
| O1 | **D3 proactive operation** — new-accession trigger + weekly sweep. Code is unwritten pending the directive registration it must ship with. | **Owner: authorize the directive patch below**, then one build task |
| O2 | **D4 surfaces** — Ask grounding → workspace strip → high-materiality feed chips (order ratified). Events exist; nothing consumes them yet. | Next build task; unblocked |
| O3 | **3,269 `item_*` events unclassified** — P3 covered part of the 10-Q volume; the run is idempotent and skips classified rows. | One run: `python execution/classify_disclosure_specificity.py --tickers <all> --db-path <prod>` |
| O4 | **WIX 6-K exhibits** — hint + status wired (D1.2); coverage honestly reads `source_missing/no_local_exhibits` until an exhibit is fetched. | Resolves on the segment 6-K pipeline's next run for WIX |
| O5 | **FMP quarterly backfill partial** — free tier: GOOG 402 (permanent at this tier), daily 429 after LITE. Fetcher skips existing files, so re-runs resume. FPI annual refresh remains blocked on the **pending FMP Starter purchase**. | Owner (purchase); otherwise re-run `fetch_fmp_10q_json.py` on later days |
| O6 | **Stale directive text** — `directives/navigation_ia.md` and `directives/llm_quota_scheduling.md` still say the Senior Partner Brief is "NOT YET MERGED"/"not yet built"; it merged 2026-07-24 (#1002) and has now run twice. | **Owner: authorize edit + commit** (directive-change gate); the fix is deleting the two stale passages |
| O7 | **77 `decision_drafts` awaiting confirmation** — mechanism verified working; doorway now exists (#1044). | Owner triage in the mobile Inbox |
| O8 | **Telegram delivery untested** (PIP DoD item 8) — every brief run used `--no-telegram`. | One supervised run without the flag |
| O9 | **Thin-substrate watch items** (honest limits, grow with data): ABTONE residual fit n=48, CFO/CEO roster resolves on 25/548 calls, `guidance_withdrawn` = 2 events (coverage-gated by design). Stored transcript rows for the 11 stockanalysis/tickertrends files predate the D1.5 parsers — a re-pull of just those sources would lift their attribution. | Watch; optional targeted transcript re-pull |
| O10 | **Stale-verdict cleanup session** (task_d01494ff) — the closing pass's P1 re-run superseded most stale concealment rows; confirm that session found nothing conflicting when it reports. | Verify on its completion |

## Proposed directive patch for O1 (NOT applied — awaiting authorization)

Addition to `directives/llm_quota_scheduling.md` (registration required for any
new scheduled LLM job):

> **disclosure_change_sweep** — weekly, Sat 14:00 PT (clear of the 04:00
> pipeline, Sun 09:00 brief, and Sun ~10:30 eval rungs). Runs P0→P3 +
> guidance/similarity/transcript detectors for any tracked ticker with a new
> accession since the last sweep; per-item degrade on transient LLM failure
> (defer + tally + retry next run), hard stops loud. Budget: reuses the
> existing per-purpose budgets; no new purpose. Idempotent; safe to re-run.

Plus a new-accession fast path: `ingest_filing_sections` already records new
accessions; the trigger runs the same detector set for just that ticker,
outside protected windows, deferring to the sweep when inside one.

