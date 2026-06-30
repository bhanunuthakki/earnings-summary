# The Ledger — A Longitudinal Investor Thought-Partner

*A design download for the sole owner of the earnings-summary platform. Opinionated, grounded in your actual seams, obedient to the 3 Laws and the single-user posture.*

---

## 1. The Big Idea

Your platform already knows an enormous amount about your *companies* — 8,839 capture facts, nine-sheet DCFs, falsifiable thesis conditions, calibration math with Wilson CIs. It knows almost nothing about *you*: how your conviction on NU has moved over six quarters, whether you reverse positions with evidence or with vibes, what you were musing about last Tuesday at 9:40am that turned out to matter. The whole substrate — Socratic priors, drift detection, longitudinal synthesis — *presumes* a corpus of dated, linked, self-authored thought that does not exist, because every write to `analyst_notes` demands a `kind` at the moment of capture. You don't capture, because capturing costs a classification decision you don't want to make mid-thought. The fix is not to abolish the taxonomy — it's to *default* it: the system supplies the identity (`musing`) so you never pay the classification tax.

**The paradigm: the system supplies the identity so capture costs nothing, then turns the accumulated stream into a thought-partner.** The Ledger is a new Command-Center surface with two faces. Its *capture face* is a keyboard-summoned tray — one keystroke from any tab — that swallows a fragment of thought with zero required structure and parks it as a legitimate, stamped, defaulted-kind note. Its *synthesis face* is a three-band reading room (your notes are the SOURCES, not the output) where the system reads back what you've thought, longitudinally, with every claim clicking to the source note and date.

Five capabilities ride this one spine:

- **(a) Capture** stream-of-consciousness as typed fragments — kind defaulted, no friction, never lost.
- **(b) Synthesize** those fragments longitudinally — "here is your standing stance on NU, consolidated from 11 notes since January, each cited."
- **(c) Coach** the investor on the one genuine thin spot — conviction-drift and self-contradiction — computed *deterministically* over your own conviction history, with the LLM only phrasing the Socratic question.
- **(d) Feedback-to-feature** — when you gripe about the software mid-thought, that becomes a clarified, deduped, living backlog row.
- **(e) Background agents** — narrow, deterministic-triggered cron stages that narrate breaches and surface stale theses into a pull lane, never interrupting.

The discipline that keeps this from becoming a second platform: **the producer earns its keep before any consumer ships.** Voice, synthesis, coaching, agents — all are *consumers of captured thought*. You cannot consume what was never captured. So we ship the cheapest possible capture surface first, with zero LLM in the write path, and find out if you actually use it before building a single consumer. Everything bold here is sequenced behind that one proof.

The critics cut hard, and their cuts shaped this design rather than decorating it: there is **no always-on rail** (it fights your no-reserved-bands density preference — it's a keyboard-summoned overlay instead), **no voice in v1** (you are keyboard-only; voice is deferred to Phase 1 behind real demand), **no ViewSpec compiler** (you write UI faster by hand than any DSL), and **no background-agent framework** (the lethal trifecta is live; the two useful checks are plain cron stages, not a registry-driven runner).

---

## 2. Day in the Life

*This is the end-state once all phases ship; see §8 for what lands when. Feedback and agents are Phase 4 (deferred and re-justified); synthesis and coaching are weeks 4–7.*

**8:55am.** Pre-market. You're reading NU's overnight ADR move. A thought lands: *"NPL formation worries me but the guide was for seasonality — am I anchoring on the Q1 scare?"* You hit `⌘.` from the Triage tab. The capture tray opens as a dismissible overlay. You type the fragment, hit commit. It lands instantly as one `analyst_notes` row, `kind='musing'` (defaulted at write time, no decision asked of you), `ticker='NU'` (auto-matched by the roster matcher — exact roster-alias match, deterministic, not an LLM guess), `source='journal'`. The tray closes. Total cost: one keystroke, one sentence, zero LLM calls.

**9:40am.** You're in the NU report drawer, asking the dock "what was Q1 NPL vs guide?" Mid-conversation you mutter into the chat: *"ugh, this NPL chart should show formation not just the ratio."* A deterministic regex pre-gate (`this chart|should show|I wish`) catches the shape; only then does `feedback_triage` (Flash-Lite) fire, fire-and-forget, off the hot path. It returns `is_software_feedback=true, feedback_kind=wish, target_ref=kpi:NU:npl_ratio`. A `feedback_items` row is appended. It does **not** become a chip, does **not** touch code — it joins the backlog projection you'll read later. Your stock thought ("NPL worries me") earlier that morning was correctly classified `is_software_feedback=false` and never reached the LLM at all.

**12:15pm.** Lunch. You open the Ledger tab deliberately — this is a pull ritual, nothing interrupted you. The **SOURCES** band shows today's musings, searchable via FTS5 (you type "anchoring" and three notes across two months surface). The **CANVAS** band shows your NU stance-over-time: a dated spine of note-density and stance rows with a hot-colored edge marking where your tone flipped Q1→Q2 (conviction-% timeline arrives in Phase 3 once decisions carry it; in Phase 2 this band draws from musings only). The **SYNTH** band shows one insight-lane item, non-decaying, accept/dismiss/track buttons: *"Your stance on NU turned constructive in Q2 with no intervening evidence note — possible drift."* Every clause is a doorway. You click "you said X" and land on the exact Q1 note. The deterministic core computed the verdict; the LLM (Haiku) only wrote the sentence. You click **track**.

**3:30pm.** You're recording a decision to trim MELI. The Decision Capture overlay (CCOverlay, Escape-dismissible) asks for conviction-% (immutable once set), expected range, and one falsifier. As you type, a just-in-time nudge fires in the Socratic flow — `recency_signature`, because there was a high-severity MELI alert 18 hours ago and your conviction moved with its sign. It's *one* question: "Would you trim this today absent yesterday's alert?" You answer it in your head, keep the trim. The nudge decays; if you'd missed it, it's gone (correct — a stale recency nudge is noise).

**6:00am next day.** Two cron stages ran post-pipeline on refreshed data. `thesis_watch` found a MELI `decision_condition` newly breached — that earns an inbox PUSH (held name, falsifiable threshold crossed, severity from the live enum). `synthesis` re-consolidated only NU and MELI (the two names with new notes — every other holding cost zero LLM). The cross-portfolio digest didn't run (it's weekly, portfolio-watermark-gated). You wake to one earned push and a quiet, current Ledger.

---

## 3. The Interface

### Where it lives

The Ledger is a **seventh Command-Center surface**, peer to Journal / Triage / Ask / Inbox / Diet. It does not replace them; it sits beside them and reuses their patterns:

| Existing surface | Relationship to the Ledger |
|---|---|
| **Journal** (`journal_panel.py`) | Journal is the structured, typed note view. The Ledger SOURCES band is the *untyped/raw* lens over the same `analyst_notes` spine — same storage, different `source` filter. |
| **Triage** (`triage_panel.py`) | The architectural template. Every Ledger band is a pure read-over-spine, exactly like Triage filters by a `context_json` discriminator. |
| **Ask** (`ask/engine.py`) | Synthesis read-back streams through `respond_turn`. The feedback tap is a fire-and-forget consumer of the same event stream that drives standup. |
| **Inbox** (`inbox.py`) | The decaying PUSH lane. Coaching tripwires and thesis breaches land here *only when earned*. |
| **Diet** (`diet_panel.py`) | The non-decaying PULL discipline the new **insight lane** borrows wholesale — stored-order, weight≠urgency — plus a per-item lifecycle the diet lane lacks. |

### The capture surface — a keyboard-summoned overlay, not a rail

The original spine proposed an always-on left rail in every tab. **Cut.** It is a reserved vertical band by definition, and you've documented a hard preference against those — you'd collapse it to an icon strip on day two and it would become dead chrome. Instead:

```
┌─────────────────────────────────────────────────────────┐
│  ⌘.  ← pressed from ANY tab                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │  CAPTURE                              [Esc] [×]     │  │  ← CCOverlay
│  │  ┌─────────────────────────────────────────────┐   │  │    (scrim,
│  │  │ NPL formation worries me but the guide was  │   │  │     Escape,
│  │  │ for seasonality — anchoring on Q1?          │   │  │     close-×)
│  │  └─────────────────────────────────────────────┘   │  │
│  │  ticker: NU (auto)   [commit ⏎]   [commit+new]      │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

This is **one keystroke away** (the actual requirement) with **zero permanent screen cost**, and it reuses Law 3's dismissible-layer machinery instead of fighting your density taste. A vanilla-JS IIFE in `SHELL_JS` binds `⌘.`, POSTs the body, closes.

### The synthesis surface — the three-band reading room

```
┌─────────────┬──────────────────────────┬─────────────────┐
│  SOURCES    │  CANVAS                   │  SYNTH          │
│  (read over │  (stance-over-time;      │  (insight lane) │
│  analyst_   │   conviction-% in P3)    │                 │
│  notes)     │                          │  ▸ Drift: NU    │
│             │   NU ●──●──●═══●──●       │    stance flip, │
│  🔍 [search]│        Q1  Q2(hot edge)  │    no evidence  │
│  • musing   │                          │    [accept]     │
│  • watch    │   each ● = doorway to    │    [dismiss]    │
│  • decision │   the source note        │    [track]      │
│             │   [generate overview]    │                 │
└─────────────┴──────────────────────────┴─────────────────┘
```

### The 3 Laws, explicitly obeyed

- **Law 1 — Identity over source.** A raw fragment is stamped `kind='musing'` *at write time* — a legitimate, closed-under-no-fit identity, the self-authored analogue of the `needs_triage` terminal that already exists for mirrored comments. It is **not** "no identity"; it is a real one, defaulted rather than asked-for, resolved through the one shared resolver. No internal string (`musing`, `[insight #N]`) ever reaches a user-facing label. (Proposed default: `musing` over `raw_thought` — it reads better in the resolver; see Open Q1, which must confirm it before the Phase 0 migration.)
- **Law 2 — Every datum is a doorway.** Every synthesized clause carries exactly one shell-handled attr: `data-fact-ref` (the cited note's exact PK, emitted via `fact_anchor_attrs`) beats `data-ask-q`. Every timeline node is an `<a>`/`<button>` to the source note. No inert `<span>` with depth-in-tooltip.
- **Law 3 — Every surface is a dismissible layer.** The capture tray, the Decision Capture form, and every coaching nudge register with `CCOverlay` (close-× + Escape + scrim). Escape resolves by priority: PALETTE > PEEK > DRAWER > DOCK. The Ledger tab's three bands each collapse to one `panel_toolbar` operating band; nav owns the title.

---

## 4. Architecture — One Spine, Five Capabilities

All five obey: naive-UTC via `src/clock.py`; `db_paths.resolve_db_path` for the DB path; **no real FK `REFERENCES`** (FK-poisoning — code-level RI only, plain INT); no render-path full-scans or network calls; every LLM call through `call_llm_structured` with a non-None purpose, pinned in `LLM_MODELS`, seeded in `llm_budgets`, registered in all **4 registries in lockstep** (`run_llm_evals.PURPOSES`, `evals_panel.RUNNABLE_PURPOSES`, `coverage`, `prompt_versions`) with sync guards run before push.

A note on migration numbers up front: the component designs each independently claimed `0115`. They cannot all be. The labels below (`L1`, `L2`, …) are **logical order**, not migration numbers; pick the real number and `down_revision` at rebase time per `reference_alembic_number_collisions_parallel_sessions.md`.

**One canonical `provenance` enum, defined once.** A single enum governs every actionable record: `owner` (you authored it), `derived` (the system synthesized it from owner content), `fetched` (came from the web — inert, action-incapable, §6). Each table below declares which subset is *legal* for it rather than redefining the enum. Legal values per table: `insight_notes` → `owner|derived|fetched`; `feedback_items` → `owner|fetched` (no `derived` — feedback is never synthesized); `investor_signals` → `owner|derived`. This is the §6 enforcement primitive; one definition prevents the per-table drift that causes a missed check.

### 4.1 Capture

**What it does.** Swallows a typed fragment with zero required structure, parks it on the spine as `kind='musing'`, never loses a word. Voice is **deferred** — you are keyboard-only, and a WhisperX sidecar is a long-running local process you'd have to babysit for a modality there's no evidence you want. If you later miss voice, Phase 1 adds it behind the *same* pipeline (typed and spoken converge after transcription).

**Auto-ticker-matcher — the one deterministic step in the LLM-free write path.** Exact roster-alias matching only (roster aliases include `NU`, `Nu`, `Nubank`, `Nu Holdings`), no ranking, no LLM. Ambiguity behavior is specified so Phase 0 is buildable:
- **No match** → `ticker=NULL` (a musing need not be about a held name).
- **Single match** → stamp it.
- **Multiple distinct roster tickers mentioned** → `ticker=NULL` + set a `needs_ticker` flag for one-click owner disposition in the SOURCES band (never guess between two names).
- **Non-roster name** (e.g. a company you don't follow) → no match → `NULL`.

**Correcting a fumbled capture.** The thesis spine is append-only and bi-temporal; a just-captured musing is fixed by **supersede, not edit** — a new `analyst_notes` row with `supersedes_id` pointing at the fumbled one, which flips to `superseded`. (The mutable layer is only the *raw staging* transcript in Phase 1, §6; the spine never mutates.) Re-stamping a `NULL`/`needs_ticker` musing's ticker is the one in-place metadata update allowed, since it corrects a system-defaulted field, not your words.

**Data model.** Phase 0 needs almost nothing — `create_note` already exists; we extend the `NOTE_SOURCES` enum (free-validated text in `notes.py`, not a schema change). The staging table arrives only with the structuring pipeline (Phase 1):

```sql
-- migration L1 (Phase 1): raw_capture_sessions — only needed once structuring exists
CREATE TABLE raw_capture_sessions (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'typed',   -- typed | voice
    transcript    TEXT NOT NULL DEFAULT '',        -- durable at stage 1, NEVER lost
    redacted_text TEXT,                             -- on-box redaction output (§6)
    status        TEXT NOT NULL DEFAULT 'captured',
    total_llm_cost REAL NOT NULL DEFAULT 0,         -- rolled-up, surfaced on the card
    created_at    TEXT NOT NULL,
    purge_after   TEXT                              -- retention TTL (§6)
);
```

**LLM purposes.** Phase 0: **none** — the write path is LLM-free, which is the whole point (no correctness risk in capture). Phase 1 adds **one merged purpose** `note_structure` (not three): the chunk+classify collapse the cost critic demanded. It emits `{thoughts:[{text, kind, direction, claim_type, confidence}]}` in a single call, routed to **Haiku/Flash-Lite** with a per-row **Sonnet** escalation only on low-confidence rows. Budget `warn` (interactive, never refuse your words). `note_classify` is folded into this one purpose — there is no separate classify call and no schema collision.

**Reuse.** `create_note` (constructor), the roster matcher (exact roster-alias match, deterministic, no LLM), `journal_links.link_note` (fact_ref attach, code-level RI), `triage_panel.py` (the Capture lens template).

**Degrade.** The invariant: *words are durably stored before any fallible step.* No LLM in Phase 0 means nothing to degrade. In Phase 1: cleanup fails → raw passes through; structuring fails → whole transcript becomes one `musing` note; budget exhausted (`warn`, never hard-stop) → one raw note lands, structure recoverable later by re-running against the retained transcript.

**Telemetry.** Phase 0 ships a trivial usage counter (captures/day, written on every commit) so the kill/keep checkpoint reads real data, not vibes.

### 4.2 Longitudinal Synthesis (NLM)

**What it does.** Three memory tiers over one spine. **Episodic** = `analyst_notes` (already append-only, supersede-chained — that IS episodic memory; we do not re-propose it). **Semantic** = a new `insight_notes` (the consolidated standing stance per scope). **Consolidated** = a weekly `synthesis_digests` summary-of-summaries. Every synthesized sentence cites a source note (NotebookLM closed-corpus rule — refuse beyond corpus).

```sql
-- migration L2 (Phase 2): insight_notes
CREATE TABLE insight_notes (
    id              INTEGER PRIMARY KEY,
    scope_key       TEXT NOT NULL,          -- ticker OR 'theme:<slug>'
    kind            TEXT NOT NULL,          -- stance | thesis | theme
    body_md         TEXT NOT NULL,
    source_note_ids TEXT NOT NULL,          -- JSON array of analyst_notes.id (grounding edge)
    as_of           TEXT NOT NULL,
    window_start    TEXT, window_end TEXT,
    watermark_id    INTEGER,                -- max(analyst_notes.id) consolidated (incremental)
    supersedes_id   INTEGER,               -- bi-temporal invalidate-don't-delete
    status          TEXT NOT NULL DEFAULT 'current',  -- current|superseded|dismissed|tracked
    esi             REAL,                   -- Evidence Sensitivity Index; NULL until Phase 3 (computed in 4.3)
    provenance      TEXT NOT NULL DEFAULT 'derived'   -- canonical enum (§4 intro); legal: owner|derived|fetched
);
-- FTS5 virtual table over analyst_notes.body for BM25 (ships with SQLite, no-build)
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(body, content='analyst_notes');
```

`esi` is **nullable-until-Phase-3**: the column ships with the Phase-2 table for schema stability but stays `NULL` until the conviction-drift detector (4.3, Phase 3) exists to populate it. Nothing in Phase 2 reads it.

**Retrieval** is metadata-filtered first (ticker/scope/date-window), then BM25 over the FTS5 table — load-bearing in finance ("NU"/"Nubank"/"Nu Holdings"). Retrieval-failure beats hallucination; no vector store.

**LLM purposes.** `stance_consolidate` (**Sonnet** — the one synthesis call worth it; summarization with citations) and `portfolio_digest` (**Sonnet**, **weekly only**, portfolio-watermark-gated — daily cross-portfolio synthesis is a solution to a problem N=1 doesn't have). Both `required_keys` include `citations[]`; both `skip`-budgeted (batch).

**The #1 correctness gate: citation validity, not presence — and the validator is itself validated by a deterministic backstop.** An LLM judge over an N=1 corpus can hallucinate support, so we do not trust an LLM to police an LLM. Two layers:
1. **Deterministic pre-check (the backstop).** Before the LLM judge runs, assert every `source_note_ids` entry resolves to a real `analyst_notes` PK *and* that note's date falls within the claim window. Any row failing referential/temporal validity is rejected outright — no LLM involved. This kills the worst failure class (a citation to a note that doesn't exist or post-dates the claim) cheaply and with certainty.
2. **LLM rubric judge (semantic only).** Only rows that pass the deterministic pre-check go to the judge, which adjudicates *semantic support* — does the cited note actually back the clause? Synthesis does not ship to you until the judge demonstrably catches a planted bad citation on a row that passed referential validity.

**Reuse.** `advisor/memos.py:persist_memo` (writer fan-out model), `standup/memory.py` (generalize the stub — see below), `signals/store.py` (non-decaying lane discipline), `data/insights/*.json` (materialization — on-demand reads hit cached JSON, no render-path LLM).

> **`standup/memory.py` extension point.** Today the stub stores a single per-run dict of standup carryover (last-run timestamp + a short list of prior standup highlights) keyed by run date, read at the next standup to avoid repeating itself. The extension is one new keyed read/write on the *same* dict shape: `insight_notes`' consolidated stance is written under a `scope_key` and read back the next synthesis pass as the **PRIOR**, explicitly labelled "the model's prior claim, not truth." This is why it generalizes cleanly — it's the same last-state-keyed-by-scope pattern, not new cross-session memory infrastructure (which is on the do-not-re-propose list). If that one-line extension point ever stops being statable in one sentence, treat it as a signal the reuse has become aspirational and stop.

**Degrade.** A holding with no new notes reuses its cached `insight_notes` verbatim (zero LLM). If the digest LLM fails, the prior digest stands. Materialized JSON means the Ledger tab never blocks on an LLM.

**Telemetry.** Each insight-lane item records an accept-vs-dismiss-vs-track tally on disposition, so the Phase 2 kill/keep gate ("accept > dismiss") reads recorded counts.

### 4.3 Coaching + Calibration

**What it does.** Fills the *one* genuine thin spot — conviction-drift / self-contradiction — and nothing else. Bias detection, pre-mortem, and hit-rate trends already exist in `calibration_coach.py` and `decision_calibration.py`; we do **not** rebuild them. The catalog of nine detectors in the component design is mostly re-pitching what exists; **build detector #1 (`conviction_drift`) and stop.** The rest is deferred until something calls it.

**Deterministic-scaffold-first, LLM-narrates-second.** RAG does not reduce drift; only a numeric diff over your own history does. The detector reads `position_sizing_intent` **full history** (not just latest — the load-bearing change) plus stance history, feeds the conviction series as `Observation(period_end, value)` into the never-before-used `timeseries/primitives.py`, and classifies each transition: **stable** / **evidence-revision** (direction changed *with* an intervening evidence note) / **contradiction** (reversed, no evidence) / **drift** (magnitude moved, no evidence). ESI = update-rate-with-evidence minus update-rate-without; this is what backfills `insight_notes.esi`.

```sql
-- migration L3 (Phase 3): investor_signals
CREATE TABLE investor_signals (
    id            INTEGER PRIMARY KEY,
    signal_type   TEXT NOT NULL,        -- stamped (Law 1): conviction_drift | unclassified_shift
    ticker        TEXT,                 -- NULL = portfolio
    strength      REAL NOT NULL,        -- deterministic, 0..1, NO ML
    score_why     TEXT NOT NULL,        -- transparent
    evidence_refs TEXT NOT NULL,        -- JSON: fact_ref / decision_id / note_id doorways
    status        TEXT NOT NULL DEFAULT 'open',  -- open|accepted|dismissed|tracked|superseded
    lane          TEXT NOT NULL,        -- inbox | insight
    cooldown_until TEXT,                -- frequency governor
    detected_at   TEXT NOT NULL,
    detector_version TEXT,
    provenance    TEXT NOT NULL DEFAULT 'derived'  -- canonical enum (§4 intro); legal: owner|derived
);
-- migration L4 (Phase 3): extend `decisions` with immutable conviction_pct, expected_low/high,
-- horizon_days; add decision_conditions.kind = 'premortem' | 'falsifier'.
```

**Decision Capture** is the one durable addition: `conviction_pct` captured **immutably before outcome** (Kahneman/Parrish — revisions create a *new* decision row, never an edit). This is what makes the existing Brier math honest; the process-quality axis (0114) already grades process not result. This is also where the CANVAS conviction-% timeline gets its data — which is why CANVAS draws stance-over-time from musings in Phase 2 and only gains the conviction series here in Phase 3.

**LLM purposes.** `coach_socratic` (**Sonnet** — user-facing voice, phrasing quality *is* the product; `warn`) and `belief_transition` (**Haiku** — pure narration of a Python-computed verdict; `skip`). The classifier is unit-tested against **planted-drift fixtures** (the real oracle — we control ground truth); the LLM cannot fabricate a signal because it never sees the corpus, only the pre-computed signal plus its citation rows.

**Reuse.** `socratic.py` (stance), `scoring.py` (stance grading), `MIN_COACH_GRADED=10` (min-n gate), the monthly scorecard cadence, `timeseries/primitives.py` (pointed at investor data for the first time — zero new stats code).

**Degrade.** No signals below the strength floor or before 10 graded decisions. If `belief_transition` fails, the deterministic verdict still renders (with a flat label, no prose). A dismissed `signal_type` raises its own future threshold — you train the coach down.

**Telemetry.** Dismissal-rate-by-`signal_type` is recorded per disposition, feeding the Phase 3 kill/keep gate directly.

### 4.4 Feedback-to-Feature

**What it does.** Turns in-flow gripes about the *software* (not about stocks) into a typed, deduped, living backlog. **Cut from the original design:** the Tier-(a) ViewSpec compiler (a UI-compilation DSL for a man who writes UI by hand — weeks of work to save a 20-minute hand-edit) and the three-tier auto-service. What remains is capture → backlog row.

```sql
-- migration L5 (Phase 4): feedback_items
CREATE TABLE feedback_items (
    id            INTEGER PRIMARY KEY,
    semantic_kind TEXT NOT NULL,    -- feedback.wish|bug|track|friction|needs_triage (stamped)
    status        TEXT NOT NULL DEFAULT 'captured',  -- captured|clarified|filed|dismissed|superseded
    body          TEXT NOT NULL, paraphrase TEXT, surface TEXT, target_ref TEXT,
    source        TEXT NOT NULL,    -- chat | note | comment
    provenance    TEXT NOT NULL DEFAULT 'owner',  -- canonical enum (§4 intro); legal: owner|fetched — fetched can NEVER action
    dedup_group   TEXT, supersedes_id INTEGER, backlog_anchor TEXT,
    created_at    TEXT NOT NULL
);
```

**The cardinal rule:** default verdict is `is_software_feedback=false`. A thought about a stock must never be misread as a request. A **deterministic regex pre-gate** (`I wish|why don't you|this chart|bug|add a column`) decides whether `feedback_triage` fires *at all* — stock-thought turns never reach the LLM (the cost critic's biggest waste, plugged).

**LLM purpose.** `feedback_triage` (**Flash-Lite** behind the pre-gate; `skip`). Its golden set — stock-thoughts-that-look-like-requests vs real requests — is the highest-value eval in the program; false-positive rate is the metric.

**Reuse.** `respond_turn` (fire-and-forget tap, off the hot path), `inbox_rank` (transparent dedup/rank, `score_why`, no ML), `platform_backlog.md` (becomes a **one-way generated projection** of `feedback_items`: DB → file, regenerated each pass). Manual edits go **in the DB**, never in the markdown — the file is read-only output. This deliberately avoids the bidirectional markdown↔DB round-trip that bit the DCF gsheets path (the CLOBBER TRAP): no "manual edits reconcile back," because reconciling a hand-edited projection against its source is exactly the fragile two-way sync the rest of this design refuses.

**Degrade.** Triage fails → the utterance lands as `needs_triage` for one-click owner disposition, never silently dropped, never auto-actioned.

**Telemetry.** Accept-vs-dismiss tally on backlog rows feeds the Phase 4 re-justification gate.

### 4.5 Background Agents

**What it does.** Two narrow cron stages — **not** a runner/registry/framework. The component design's `src/agents/runner.py` + `AGENTS` registry + `agent_runs` ledger + kill-switch hierarchy is SaaS-shaped worker infrastructure for a throughput problem one user doesn't have; you merge your own diffs already. **Cut the framework.** Write the two useful checks as ordinary morning-pipeline stage functions:

- `thesis_watch` — for each held name, deterministically detect a `decision_condition` whose latest fact now breaches its falsifiable threshold (via the existing `convert_unit` compare path). On breach → inbox PUSH (`semantic_kind='thesis_breach'`, severity from the live enum). On mere staleness → insight PULL lane.
- The `synthesis` stage from 4.2 (per-holding consolidation + weekly digest) is itself the second "agent" — a plain stage, incremental/watermarked.

**LLM purpose.** `thesis_watch_brief` — **Sonnet** on a genuine breach, **Haiku** on a staleness nudge (split by severity). `skip`-budgeted.

**Reuse.** Morning-pipeline stages + Windows scheduled-tasks (no new scheduler), `llm_calls` ledger (cost/latency already recorded), the inbox/diet/insight lane discipline.

**Degrade.** Idempotency via a deterministic `work_unit_key` (`thesis_watch:{ticker}:{decision_id}:{fact_period}`) — re-running on unchanged data is a no-op with zero LLM cost. If the stage fails, the pipeline continues without it.

---

## 5. The Feedback-to-Feature Loop + Agents — The Bold Claim, Safely Scoped

*This section governs Phase 4, which is deferred and must be re-justified before building. It is deliberately spec-ahead-of-build: the safety model is written **before** the feature is greenlit so the loop cannot ship without its governance already designed. That asymmetry — the most-elaborated safety section guarding the least-committed feature — is the intended order, not an oversight.*

The boldest version of this paradigm is *the thought-partner hears a wish and grants it*. The honest version, for your posture, is narrower and safer — and the narrowing is the design, not a caveat.

**What is autonomous** (append-only, reversible, no action against the running app):

| Action | Autonomy | Why safe |
|---|---|---|
| Classify an owner utterance, append a `feedback_item` / `investor_signal` / `musing` row | **Autonomous** — *only if `provenance=owner`* | Append-only, supersede-chained, never deletes |
| Regenerate the `platform_backlog.md` projection | **One-click** | One-way DB → file; show the diff |
| Run deterministic detectors + narration over local data | **Autonomous** | Verdict is Python; LLM only phrases |
| Surface an insight/breach into a lane | **Autonomous** | Lane discipline + frequency governor gate noise |

**What requires one click — always:**

- **Any code change.** The two cron stages never write code. If a gripe *does* warrant a fix, it surfaces as an inert `spawn_task` chip you click to spin into an isolated worktree producing a **reviewable diff, never a live mutation**. The chip carries no pre-granted capability. **The spawned coding agent binds to the scrubbed fixture DB and runs network-less (or fetch-quarantined), identical to every other agent** — it never holds prod-DB-read + web-fetch + action-write together. Without this binding the trifecta defense would have a hole exactly where real code gets written; with it, the spawned coder is no more privileged than any narrate-from-quarantine pass.
- **The oracle gate before the merge button even renders.** "Tests green" is insufficient for numeric correctness. Any diff touching valuation/KPI/financial math must validate against ground truth (the diff-aware CI eval + a golden numeric check on the affected `fact_ref`) before merge is offered.

**Never autonomous, red-flagged:** any diff touching a safety control — the redactor, retention TTLs, kill switches, provenance tagging, the trifecta isolation, or `src/llm` governance — renders a **distinct red merge path** requiring explicit acknowledgement that it modifies a safety control. Self-modification of safety controls is never a routine one-click.

The frequency governor is load-bearing: N=1 has no crowd to average out a bad signal, and over-firing trains dismissal. A venting session bumps an existing item's rank rather than spawning ten chips.

---

## 6. Safety, Privacy, Cost — The Consolidated Model

Stream-of-consciousness investment thought is the single most sensitive datum you produce: unfiltered, MNPI-adjacent, emotionally revealing. The capture degrade paths *guarantee* the rawest version persists. So the model protects the **raw transcript**, not the cleaned gist.

### Three trust zones

1. **Local-sacred** — raw audio (if voice ever ships) + raw transcript + the prod DB. Never crosses the LLM-provider boundary; never visible to spawned coding agents (which bind to a **scrubbed fixture DB**, asserted before spawn, and run network-less — the worktree-resolves-to-MAIN footgun is real); loopback-bound; retention-windowed with one-click forget.
2. **Redacted-exportable** — masked text that may cross to a metered LLM purpose, produced *only* by a local, **pre-LLM redaction pass** between durable-persist and any LLM hop. The pass is **not uniformly deterministic, and the spec is honest about which parts are**:
   - **Deterministic (regex + denylist):** phone/email/account-number patterns and an owner-maintained MNPI/source denylist of exact strings (deal codenames, source names). These are truly regex-grade and assert-able.
   - **Not deterministic (names):** arbitrary person-name detection is NER, which is a model, not a regex. We take option (b): **scope name-stripping to roster + owner denylist only** and accept that a novel, never-listed person name can pass. (If that proves insufficient in practice, the upgrade is a *local* NER model — explicitly a model, run on-box — not a pretend-deterministic regex.)

   The raw never leaves localhost regardless. (This is the inverse of the naive design, where `dictation_cleanup` would have shipped the rawest version verbatim.)
3. **Untrusted-inbound** — all fetched web content. `provenance=fetched`, **inert, action-incapable**. The feedback classifier hard-refuses to set `is_software_feedback=true` on any utterance whose context contains fetched content — this single rule breaks the prompt-injection → feature-request → code-change chain (OWASP LLM01→LLM06). Synthesized output re-entering a prompt is `provenance=derived`, delimited as *the model's prior claim, not ground truth*.

### Prompt-injection defense

The lethal trifecta (private DB + fetched content + outbound calls) is live. Defenses: **no single process holds DB-read + web-fetch + action-write** — *including the spawned coding agent* (§5), which binds to the scrubbed fixture DB and runs network-less. Web-fetch agents (if ever built) run fetch-and-quarantine (no DB write, no spawn) then a *network-less* narrate-from-quarantine pass. The single canonical `provenance` enum (§4 intro) on every actionable record is the enforcement primitive, not a slogan — one definition, legal-subset declared per table, so there's no per-table enum drift to slip a check through.

### Redaction of the ledger itself

The `llm_calls` ledger must not become a second uncontrolled copy. For all capture/synthesis/coaching purposes: store a content hash + token counts, never the prompt body; truncate `error` to a taxonomy code, never raw text; audit `score_why` to confirm it paraphrases the *signal*, not the *utterance*.

### Cost caps + kill switches

- **Global daily spend ceiling.** Past a soft threshold it **forces all `warn` purposes to single-shot cheapest-model mode** — it degrades, it never blocks. This is the precise meaning of `warn` here: it still never refuses your words, but past the threshold it stops choosing the expensive model for you. One inbox notice fires.
- **Per-session cost rollup** on `raw_capture_sessions.total_llm_cost`, surfaced on the card — the expensive path is visible so you self-govern.
- **Model routing:** anything that narrates a deterministic verdict → cheapest tier (Haiku/Flash-Lite); the user-facing voice (`coach_socratic`) and the two synthesis writers → Sonnet; **Opus nowhere** except a hypothetical future `spawn_task` coding leg, which gets a *separate `autonomous_build` budget line* with a weekly count-cap and its own circuit-breaker (the one real dollar hole — `spawn_task` bills outside `llm_calls`).
- **Retention:** raw transcripts default to a 90-day TTL then hard-purge; committed redacted notes survive on the spine. `POST /api/capture/session/<id>/forget` purges transcript + audio + staging in one shot. Append-only is right for the *thesis spine*, wrong for the *raw capture staging layer* — distinguish them.

---

## 7. What We Are NOT Building

These cuts are the design. Each respects either your posture, the do-not-re-propose list, or a critic's verdict.

| Cut | Why |
|---|---|
| **Voice / WhisperX sidecar / VAD / audio store** (v1) | You are keyboard-only across every existing surface. A held-mic is *more* friction than the textarea you live in. The binding constraint is *cognitive* (the mandatory `kind`, now defaulted), not input-modality. Deferred to Phase 1 behind real demand; the dead `quarterly_artifacts.audio` stub stays dead. |
| **Always-on left rail across all tabs** | Fights your documented no-reserved-bands density preference; you'd collapse it to dead chrome. Replaced by a `⌘.` CCOverlay tray — same one-keystroke access, zero permanent cost. |
| **The Tier-(a) ViewSpec compiler + tiering** | A declarative UI DSL for the person who hand-writes the f-strings. You build the view faster by typing it. Keep capture→backlog only. |
| **The background-agent runner / registry / `agent_runs` ledger / kill-switch hierarchy** | SaaS-shaped worker infra; edges toward event-driven discovery feeds (do-not-re-propose). The two useful checks are plain cron stages. |
| **The triplicated drift engine + ~11 of 14 LLM purposes** | Three components each claimed credit for the same belief-transition mechanism. Build it once (4.3); synthesis and agents *read* it. Ship 4 purposes (`note_structure`, `stance_consolidate`, `coach_socratic`, `feedback_triage`), defer the rest until called. |
| **Eight of nine coaching detectors** | Bias/pre-mortem/hit-rate already exist in `calibration_coach.py`. Build `conviction_drift` only. |

**Do-not-re-propose, respected:** episodic memory is `analyst_notes` (not a new raw-thought table); synthesis generalizes the `standup/memory.py` stub (one keyed read/write on its existing shape, framed as extending it — see 4.2 — not inventing cross-session memory); we do not pitch "true Brier as new" (the immutable-conviction *capture discipline* is the net-new, not the math); no paid data, no options model, no 8th signals writer, no email/push/remote, no event-driven discovery feeds.

---

## 8. Phased Build Plan

### Phase 0 — The Thinnest Usable Slice (1 week): "Capture → park → read back," zero LLM in the write path

**Thesis to prove:** frictionless capture + a searchable read-back changes how you work. If this doesn't earn daily use, no consumer matters.

**Deliverables:**
- One migration: extend `NOTE_SOURCES`; add the `musing` kind. (No staging table yet.)
- `⌘.` capture tray in `SHELL_JS` — POSTs a body-only `musing` note, ticker auto-matched (exact roster-alias, no LLM), with the no-match/multi-match/single-match behavior from 4.1.
- The Ledger SOURCES band — read-over-spine via the `triage_panel.py` template, including one-click ticker disposition for `needs_ticker` musings.
- FTS5 virtual table over `analyst_notes.body` + a metadata-filtered search box (the brief's highest-leverage low-effort win; pure SQLite).
- A trivial captures/day usage counter (so the checkpoint below reads data, not memory).
- *(Conditional, see Open Q3)* a placeholder "dictate" affordance **only if** you want voice demand-gated — without it, Phase 0 cannot measure voice demand and the demand-gating language drops to "deferred, revisit."

**Modules touched:** `alembic/`, `src/user_state/notes.py`, `src/dashboard/triage_panel.py`, `command_center_shell.py`, `SHELL_JS`.

**Kill/keep checkpoint:** Do you capture daily for two weeks (per the counter) without prompting? If no → stop. Voice and synthesis would be polishing a thing nobody touches.

### Phase 1 (weeks 2–3) — Structuring pipeline (+ optional voice)

**Deliverables:** `raw_capture_sessions` + `capture_thoughts` staging; the merged `note_structure` purpose (Haiku/Flash + Sonnet escalation); the **review tray** (the genuinely hard frontend — Alpine holds the tray array, one bulk-commit POST; budget real time). The on-box redaction pass (deterministic regex+denylist + roster/denylist name-scoping, §6) and ledger redaction ship *here*, before any LLM sees a transcript. Voice (WhisperX sidecar) only if Phase 0 surfaced real demand (which requires the Q3 dictate stub to have shipped in Phase 0).

**Modules touched:** `src/capture/pipeline.py`, `src/capture/routes.py`, `src/llm/cli.py`, `SHELL_JS`.

**Kill/keep:** Does the structured tray beat the raw park, or do you just commit raw every time? If raw wins, the pipeline is over-built — keep Phase 0 and stop.

### Phase 2 (weeks 4–5) — Synthesis (CANVAS + SYNTH bands)

**Deliverables:** `insight_notes` (with `esi` shipped nullable, populated only in Phase 3); the scheduled `synthesis` stage (incremental, watermarked); `stance_consolidate` + weekly `portfolio_digest`; the non-decaying insight lane lifecycle (`InboxItem.kind='synthesis'`) with the accept/dismiss/track tally. CANVAS draws **stance-over-time from musings** in this phase (note-density + tone), **not** conviction-% — that series doesn't exist until Phase 3. **The gating deliverable is the citation-validity pipeline** — the deterministic referential/temporal pre-check *plus* the LLM rubric judge; synthesis does not reach you until the judge catches a planted bad citation on a row that passed the pre-check.

**Modules touched:** `src/advisor/memos.py` (writer model), `src/standup/memory.py` (generalize stub per 4.2), `src/signals/store.py`, `run_llm_evals.py`, `data/insights/`.

**Kill/keep:** Do you accept/track more insights than you dismiss (per the tally)? High dismissal = mis-tuned or untrusted — fix grounding before proceeding.

### Phase 3 (weeks 6–7) — Conviction-drift coaching (detector #1 only)

**Deliverables:** `investor_signals`; the `conviction_drift` detector over `position_sizing_intent` full history + `timeseries/primitives`; `coach_socratic` + `belief_transition`; Decision Capture overlay (immutable `conviction_pct`). This phase is where conviction-% begins to exist, so it **adds the conviction-% series to CANVAS** and **backfills `insight_notes.esi`**. Deterministic core unit-tested against planted fixtures (a **hard merge gate**). Frequency governor + min-n gate + dismissal-rate telemetry from day one.

**Modules touched:** `src/calibration_coach.py`, `src/decision_calibration.py`, `src/advisor/socratic.py`, `src/timeseries/primitives.py`.

**Kill/keep:** Dismissal-rate-by-`signal_type` telemetry — if you dismiss most drift flags, the detector is wrong or naggy. Tune or kill.

### Phase 4 (deferred, re-justify before building) — Feedback loop + thesis-watch stage

The `feedback_items` table + regex-gated `feedback_triage` + the `thesis_watch` cron stage, plus the accept-vs-dismiss tally that feeds the re-justification. Until the capture core earns daily use, manual `platform_backlog.md` is fine. The `spawn_task` fix path (with its scrubbed-fixture/network-less binding, §5) and its `autonomous_build` budget line only earn their keep once there's a synthesized corpus worth acting on.

---

## 9. Open Questions for the Owner

1. **Confirm the proposed `musing` default (vs `raw_thought`).** §3 Law 1 proposes `musing` as the defaulted untyped kind; this Q is the confirmation step. `musing` reads better in a user-facing resolver; `raw_thought` is more honest about what it is. Must be confirmed *before* the Phase 0 migration, because the wrong choice forks the resolver. Your call.

2. **The Phase 0 daily-use bar.** What count of captures over what window convinces you the producer earns its keep — and what's the honest kill threshold if it doesn't? The captures/day counter records it; you set the line. The whole sequencing rests on answering this truthfully rather than building the consumers anyway.

3. **Voice: demand-gated, or just deferred?** These are not the same and the plan can't be both. If you want voice *gated on Phase-0 demand*, the Phase 0 "dictate" placeholder **must ship** (you can't measure reaching-for-it without it) — commit it to Phase 0 deliverables. If you don't want that stub, drop the demand-gating language and we simply say "voice deferred, revisit after Phase 1." Pick one here so Phase 0 scope is settled.

4. **MNPI/source denylist ownership.** The redactor's *deterministic* leg needs an owner-maintained list of private-deal codenames and source names to mask before any LLM hop (names outside roster+denylist can pass — see §6). Are you willing to maintain that list, and where should it live (a gitignored file like the META pin store)?

5. **Provider routing for the rawest hop.** With `ANTHROPIC_API_KEY` set (metered), even redacted text crosses to Anthropic/Google. Do you want the most sensitive purpose run against a *local* model (the no-build stack tolerates a sidecar) so the rawest redacted text never leaves the box — or is the deterministic+denylist redaction pass sufficient for your comfort?

6. **Retention window.** 90 days for raw transcripts before hard-purge (committed notes survive) — too short, too long, or do you want retention off entirely and rely solely on one-click forget?

7. **Decision Capture friction tolerance.** The immutable-conviction-before-outcome rule is what makes calibration honest, but it adds a form to every recommendation. Are you willing to pay that friction on every decision, or only on a flagged subset (e.g. position changes above a size threshold)?

8. **Citation-validity bar for synthesis.** The deterministic pre-check (§4.2) guarantees every citation resolves to a real, in-window note; the LLM judge then polices semantic support, where grounding takes hallucination ~40%→~13%, not zero. At N=1, one confident-but-wrong "you said X last quarter" erodes trust permanently. What *semantic* false-citation rate, measured against the rubric judge on rows that already passed the deterministic pre-check, would you accept before you let synthesis read back to you unsupervised?


---

# Appendix A — Why this spine was chosen (judge scorecard)

I have a complete, self-contained judging task. The three visions and grounding brief are all provided inline. No codebase exploration is needed — this is a design-judgment synthesis. Let me deliver the scorecard, spine choice, and merged-spine description.

# SCORECARD

| Criterion (1-10) | A — Companion-Ledger | B — Notebook-Canvas | C — Ambient-Coach |
|---|---|---|---|
| (a) Fit to owner workflow + substrate | 9 | 8 | 7 |
| (b) Boldness / genuine new paradigm | 8 | 7 | 9 |
| (c) Buildability on no-build stack within posture | 8 | 7 | 8 |
| (d) Serves ALL asks (dictation / synthesis / coaching / feedback→feature / agents) | 9 | 8 | 7 |
| (e) Risk profile (higher = safer) | 8 | 8 | 5 |
| **Total /50** | **42** | **38** | **36** |

### Why these scores

**A — Companion-Ledger (42).** Its defining move — capture is the binding constraint, so make capture cheaper than not-capturing — is the single most correct insight across all three. The persistent left rail means capture is always one glance away in *every* tab, which fits a single owner who thinks in fragments all day. It serves all five asks without forcing the owner to "visit" anything. Mild fit ding: an always-present rail across all 6 tabs is real screen-cost and could feel heavy; it leans hardest on the distiller, whose mis-attachment risk is correctly named as Risk 1.

**B — Notebook-Canvas (38).** The strongest *synthesis* surface — "your notes are the SOURCES, not the output" is the cleanest framing of the longitudinal-memory ask, and the three-band SOURCES/CANVAS/SYNTH layout maps perfectly onto the Triage-lens + diet-discipline + persist-memo seams. But it is a *destination you visit*, which reintroduces the journaling-paradox failure mode (the corpus nobody rereads — it even names this as its own Risk 1). Capture is bolted onto a read-surface rather than being the spine. Less bold than C, less workflow-fit than A.

**C — Ambient-Coach (36).** Boldest paradigm ("an interlocutor that earns the right to interrupt you") and the most decisive on the genuinely-thin gap (drift/self-contradiction). But it inverts the dependency: it makes *coaching* the product and capture merely its fuel. At N=1 this is the riskiest possible bet — its own Risk 1 (over-firing trains dismissal, fatal, hard to tune with sample size of one) and Risk 2 (a confident liar accusing the owner of contradicting himself) are existential, not mitigable to comfort. A proactive nag as the *primary personality* of the product is a trust gamble; better as a tenant than a landlord.

---

# CHOSEN SPINE: **A — The Companion-Ledger**, named **The Ledger**

**Why.** Capture is the load-bearing constraint the whole substrate is starved on — every downstream capability (Socratic priors, drift detection, synthesis) already presumes rich dated linked notes that don't exist because every write demands a `kind`. A wins because it puts the cheapest-possible capture surface everywhere (the persistent rail), making it the *spine* rather than a *place*. B's synthesis and C's coaching are both magnificent — but they are **consumers of captured thought**, and you cannot consume what was never captured. You graft consumers onto a producer, not the reverse. A is also the safest of the three (no existential N=1 tuning bet as primary personality) while still being bold.

The grafts are surgical: take **B's three-band reading room** as the Ledger's full-screen tab (the rail is the always-on capture mouth; the tab is the synthesis lens), and take **C's earned-interruption Coach lane + frequency governor** as a strictly-budgeted, deterministic-core tenant inside the rail — never the product's face.

---

# THE MERGED SPINE (~600 words) — "The Ledger"

**Primary interface.** A seventh Command-Center surface, **The Ledger**, with two faces. Its *capture face* is a persistent, collapsible left rail (a `CCOverlay`-registered band, Law 3) rendered by `command_center_shell.py` across **every** tab — a push-to-talk mic plus a compose strip, never more than a glance away, collapsing to a single icon strip. Its *synthesis face* is the full-screen Ledger tab, which adopts **B's three-band layout**: SOURCES (a pure Triage-pattern lens over `analyst_notes` with the new BM25+semantic+metadata search), CANVAS (the view-timeline — your conviction over time with belief-transition markers, plus "generate overview" and the local-TTS audio roll-up), and SYNTH (the non-decaying Insight lane with per-insight accept/dismiss/track). The rail produces; the tab reads back. This fuses A's "always-on capture" with B's "your notes are the sources" reading room — resolving B's only real weakness (a destination nobody visits) by making the producing surface ambient and the consuming surface a deliberate ritual.

**Where it lives / how components attach.**

- **Capture (A's spine, B's chip-confirm).** Push-to-talk → `MediaRecorder` + mandatory WASM VAD gate → local WhisperX sidecar via `fetch` to `127.0.0.1` (frontend stays a vanilla-JS IIFE in `SHELL_JS`). Raw transcript lands instantly as one durable `analyst_notes` row, `kind="raw_thought"` (a legitimate stamped closed-under-no-fit identity — resolving Tension 1). A governed `thought_distill` purpose (retrieval-grounded with BM25 to avoid hallucinated `fact_ref`s) splits it into atomic, linked, doorway-bearing chips — surfaced as **B's editable confirm/edit/merge/drop chips** (human-steers-AI), never auto-committed.

- **Synthesis (the NLM).** Three memory tiers over one spine: episodic = `analyst_notes`; consolidated = a new monthly summary-of-summaries (writer modeled on `persist_memo`, reader on `anchors.py`); semantic = existing thesis records, read not duplicated. Belief edges via a thin `thought_links` table (plain INT, no FK). Every synthesized clause carries a mandatory Law-2 citation doorway; refuse-beyond-corpus. Lives in the non-decaying Insight lane (Tension 2 resolved as a third lane with lifecycle).

- **Drift (C's differentiator, deterministically scaffolded).** `timeseries/primitives.py` finally pointed at investor data; a new `investor_signals` writer diffs `position_sizing_intent` *history* + `stance_scores`, classifying each transition stable/evidence-revision/contradiction/drift and computing the Evidence Sensitivity Index. The LLM only **narrates** the computed verdict (Tension 4).

- **Coaching (C's Coach lane, demoted to tenant).** Two cadences: just-in-time tripwire/anchoring nudges in the existing Socratic flow pre-decision; contradiction/calibration/drift batched into the monthly scorecard and Insight lane. **C's frequency governor (≤2/day) + `MIN_COACH_GRADED` min-n gate are load-bearing** — the Coach is a strictly-budgeted voice inside the Ledger, never the product's primary personality (defusing C's existential Risk 1).

- **Feedback→feature + agents.** Mid-stream gripes stamp `feature_gripe`, clarified-before-built into a scored living backlog. Background agents are scoped to the safe envelope: diff-never-mutation, break-a-leg isolation, mandatory human merge gate, controlled generative-UI only, oracle-before-autonomy; falls back to gripe→backlog if isolation wobbles.

**One-line build mandate for the synthesizer.** Ship the always-on rail (capture) + the three-band Ledger tab (B's reading room) first; attach the deterministic drift engine and the governed Coach lane (C) as tenants of the SYNTH/Insight lane — coaching is a budgeted voice, never the landlord.


---

# Appendix B — Grounding brief (the substrate this builds on)

# GROUNDING BRIEF: Longitudinal Investor Thought-Partner

A design agent acting on this brief is extending a **single-user, local-first equity-research platform** (one owner, "bhanu"; ~11 holdings + watchlist + ~27 evaluation names). The deliverable target is a richer journaling/synthesis/coaching loop on top of an already-deep substrate. **Build through the existing seams; do not greenfield.**

---

## 1. WHAT ALREADY EXISTS (the substrate to reuse)

**Notes spine — `src/user_state/notes.py` + `analyst_notes` (alembic 0074/0093/0099/0101).** One append-only, never-hard-deleted table; corrections are supersede-chains (`supersedes_id`). Single low-level constructor `create_note`; lifecycle verbs resolve/reclassify/supersede/archive. Columns of note: `kind` (question·decision·watch·assumption·observation), `status` (open·resolved·superseded·archived), `body`, `ticker` (NULL=portfolio), `anchor_type`/`anchor_key`, `fact_ref` (stable doorway handle `kpi:{T}:{def}` / `fin:{T}:{line}:{period}`), `source` (manual·comment·advisor; chat/alert declared-but-unwired), `decision_id`/`position_entry_id` (plain INT, no FK), `context_json` (overloaded, reconciler-owned — NOT safe for user metadata). Defensive decoder `AnalystNoteRow` loads pre-migration rows. **Lens pattern proven:** Triage (`triage_panel.py`) is a pure read over the spine filtered by a `context_json` discriminator — the template for any new view. Capture surfaces (all keyboard-only): ✎ Notes drawer (`command_center_shell.py` / `ticker_command_center.py:432-489`), Journal panel, `POST /api/notes` (`comments_server.py:1338`), comment-mirror sync, advisor echo.

**Advisor — `src/advisor/` (Socratic, never directive).** Own table `advisor_memos` (0077) + `persist_memo` fan-out into 4 places (memo row, `analyst_notes` source=advisor, `thesis_ledger_entries`, backlinks). Three memo kinds: next-dollar, swap-discipline (deterministic screen, LLM only on cleared pairs), and **Socratic think-through** (`socratic.py` — the ONLY per-holding stance path; stances persist + are graded by `scoring.py`/`stance_scores` 0078). **Coaching is ~80% already built: `src/calibration_coach.py`** — named bias detection, calibration grounding, eval-gated pre-mortem, behavioral experiment, min-n gate (`MIN_COACH_GRADED=10`), monthly scorecard. Wired into the Socratic flow via `premortem_block`/`calibration_block`.

**Ask engine — `src/ask/engine.py` + `ContextPack` (`context.py`).** "One brain, two entry points"; pack differs by scope (ticker/portfolio). Event-stream seam `respond_turn` (stage/delta/fragment/final/citations), already driven by 4 callers (dock, report drawer, **standup** = proactive-turn precedent, evals). Persistence: portfolio → `ask_sessions`/`ask_turns` (0085, server-authoritative, `role` is free text so assistant-initiated turns need no schema change); ticker → per-report JSON. Dock UI (`ask_dock.py`) has min/float/split states + `CCOverlay` registration. **Cross-session memory is a deliberate stub** (`standup/memory.py:6-9`).

**Inbox / lanes — `src/dashboard/inbox.py` + `inbox_rank.py`.** `InboxItem` with two discriminators: `kind` (alert·draft·ledger·note·synthesis = lane) and `semantic_kind` (identity, stamped at write). Transparent NO-ML score = severity × 30h-decay × position-weight × thesis-relevance, ships `score_why`. **Two orthogonal lanes:** inbox = decaying PUSH (action); **diet** (`signals/store.py`, `diet_panel.py`) = non-decaying PULL (reading), stored-order, weight≠urgency, guard-pinned. Cockpit (`research_cockpit.py`) = per-holding attention triage. The weekly cross-portfolio synthesis memo is fire-and-forget (lowest weight, 7-day window, no lifecycle).

**Decisions / thesis / timeseries.** Five durable records: `decisions` (0046/0086/0114 — recommendation audit + falsifiable conditions + process-quality axis), `position_sizing_intent` (0061 — owner's stated conviction/target, append-only, **only latest ever read**), `thesis_ledger_entries` (0062), `analyst_notes`, `position_entries` (0088 — entry thesis + post-mortem lessons). `src/decision_calibration.py` already computes hit-rate-by-conviction (Wilson CI), Brier-vs-baseline, process×outcome matrix, reversals, omissions, expectancy, cohort "am I improving" trend. `src/timeseries/primitives.py` = 6 generic stat primitives over `Observation(period_end, value)` — **entirely company-metric-facing today, never pointed at investor data.** Company thesis health = `thesis_evaluations`/`thesis_history.py`.

**LLM governance — `src/llm/cli.py:741` `call_llm` / `call_llm_structured` / `call_llm_with_web`.** Single entry point; purpose-keyed `_model_for` picker (DB override → `LLM_MODELS` → default Sonnet); model-first backend dispatch (Claude/Gemini); every call writes an `llm_calls` ledger row (cost/latency/error/fallback); per-purpose budgets (`block`/`skip`/`warn`); `is_hard_stop` taxonomy (budget/setup propagate, transient degrades). Structured output = `expect`+`required_keys`, one retry, fail-visible (never silent `{}`). Eval harness `run_llm_evals.py` (golden + rubric modes); **new purpose → 4 registries in lockstep** (`run_llm_evals.PURPOSES`, `evals_panel.RUNNABLE_PURPOSES`, `coverage`, `prompt_versions`) + sync guards.

---

## 2. THE HOUSE RULES (non-negotiable)

**The 3 Instrument-Paradigm Laws** ("one instrument, not pages"):
- **Law 1 — Identity over source.** Category/label/rank/links derive from a `semantic_kind`/`signal_type`/`fact_ref` discriminator *stamped at write time*, resolved through ONE shared resolver — never re-sniffed from the source table or a render-time regex. No internal-format string (`observation`, `[advisor memo #N]`) reaches a user-facing label.
- **Law 2 — Every datum is a doorway.** Any number/cell with depth is an `<a>`/`<button>` carrying exactly one shell-handled attr: `data-peek-url`, `data-ask-q` (relative-window phrasing — period COUNTS, never ISO ranges), or `data-fact-ref` (exact PK; **beats `data-ask-q`**; emit via `fact_anchor_attrs`). Inert `<span>` with depth-in-tooltip is forbidden.
- **Law 3 — Every surface is a dismissible layer; every section is one labeled instrument.** Every overlay registers with `CCOverlay` (close-× + Escape + scrim by construction; Escape resolves by PRIORITY not recency: PALETTE>PEEK>DRAWER>DOCK). Nav owns the title; panels collapse to ONE operating band (`panel_toolbar`).
- **Corollaries:** one item-model-many-lenses; classifiers **closed under no-fit** (always a `needs_triage` terminal); ranked surfaces = weighted typed/dated signals + `score_why`, NO ML; severity color from the live `Severity` enum.

**Design language** (`src/ui/tokens.py`, `src/ui/controls.py`): compose `palette_css + controls_css + layout-only CSS`; **zero raw hex**; 3 button intents; one `.k-chip`; 6 type steps; 3 font families; one radius/motion/shadow; weight ≤600. All prose through `ui.prose.render_prose()` (never bare `escape()`, never a 2nd markdown renderer). Enforced by opt-out CI guard over ~41 auto-discovered surfaces (but the `tests` job is NOT a required merge gate — **run UI guards locally before push**).

**LLM governance requirements:** every call through `call_llm*` with a non-None `purpose`; pin purpose in `LLM_MODELS` with rationale; structured output via `call_llm_structured`+`required_keys`; own `llm_budgets` seed migration (mode by surface: `warn` for interactive/never-block, `skip` for batch, `block` rarely); eval-covered + 4 registries in lockstep; gate user-facing judgment inline with the rubric judge.

**Single-user posture (CONFIRMED, adversarially verified).** Multi-tenant is the WRONG target — FMP ToS forbids redisplay to a 2nd user + LLM COGS reverts to metered API + no wedge. Stays single-user, loopback-bind, pull-only on localhost. **Do-NOT-re-propose:** paid data (FMP-Ultimate/options/13F), cross-session memory pitched as new (a stub exists), "true Brier calibration" as new (cheap hit-rate trends exist), options/derivatives in the position model, the 8-writer signals spine, email/push/remote/phone, event-driven discovery feeds, features outside one-investor research scope.

**Frontend (CONFIRMED):** server-side Python f-strings, vendored+inlined HTMX 2.0.4 + Alpine 3.14.1, hand-written vanilla JS in `SHELL_JS`. **No bundler, no npm, no CDN, no TS/JSX.** Any new client capability is a vanilla-JS IIFE dropped into `SHELL_JS`. Other invariants: naive-UTC datetimes via `src/clock.py` only; `db_paths.resolve_db_path` for DB path; no real FK `REFERENCES` (FK-poisoning); no render-path full-scans/network calls; canonical deliverable `build_artifacts.py → output/research/<T>/<DATE>_report.html` (6 pinned tabs).

---

## 3. THE GAPS (what's missing for the vision)

1. **Frictionless stream-of-consciousness capture.** Every write demands a `kind` at capture (taxonomy-first = the opposite of braindump). No body-only/untyped park-then-classify path for *self-authored* notes (the `needs_triage` machinery exists only for mirrored comments). **Zero voice/dictation/STT anywhere** (the `quarterly_artifacts.audio` columns are a dead, never-written stub — do not reuse). No threading/session/daily-entry model — notes are flat singletons. No full-text search / tagging / temporal grouping (filters are exact-match only).

2. **Longitudinal investor-memory synthesis.** Notes are *consumed* by synthesis (priors anchor) but no engine periodically reads N notes over a window and emits a roll-up with a lifecycle. The cross-portfolio synthesis memo is the only precedent and it's fire-and-forget. No episodic/semantic/consolidated memory tiers. No "what did I muse about last week."

3. **Coaching/calibration — the one genuine thin spot is conviction-drift / self-contradiction.** Bias/calibration/pre-mortem already exist (`calibration_coach.py`). Missing: nothing diffs the owner's *own successive stances/notes* on a name to flag "you said X last quarter, now not-X without new evidence." `position_sizing_intent` stores conviction append-only but only latest is read; `timeseries/primitives` is never run over investor data; no `investor_signals` persistence table.

4. **Feedback-to-feature loop.** No in-flow gripe-capture that becomes a clarified, scored, living backlog (the platform tracks `directives/platform_backlog.md` manually).

5. **Background agent research system** (the boldest, riskiest gap) — request → background coding-agent task → reviewable diff, not live mutation.

---

## 4. THE RESEARCH DISTILLATE (most buildable external patterns)

1. **Closed-corpus grounding + mandatory inline citations** (NotebookLM): every synthesized claim clicks back to the source note+date; refuse beyond corpus. Grounding ~3x's hallucination down (~40%→~13%), not zero. → Synthesis roll-ups must cite `analyst_notes` rows; reuse Law-2 doorways as the citation handle.

2. **Hybrid retrieval (semantic + BM25) + metadata filtering** by ticker/date. BM25 is essential in finance (NU/Nu Holdings/Nubank, exact tickers). Retrieval-failure > hallucination as the dominant RAG bug. → The single highest-leverage low-effort win is metadata-filtered retrieval over the notes spine.

3. **Three memory tiers** (TSM/Synapse): episodic (raw notes, append-only — `analyst_notes` already IS this) → consolidated monthly "summary-of-summaries" Topics → semantic standing thesis. → The consolidation rung is the new build; model the writer on advisor `persist_memo`, the reader on `anchors.py`.

4. **Bi-temporal invalidate-don't-delete** (Zep/Graphiti): supersede stale beliefs, never overwrite; preserve history + queryable current state. → `analyst_notes` supersede-chains already encode this; extend to belief edges.

5. **Belief-transition operators + Evidence Sensitivity Index** (BeliefShift, the differentiator): classify every view change as **stable / evidence-revision / contradiction / drift**; ESI = update-rate when evidence present vs absent (positive=disciplined, negative=drift). Critically: **RAG does NOT reduce drift** — build belief-tracking as deterministic scaffolding + evidence-discrimination prompting, not freeform LLM. → This is the conviction-drift gap (#3); spine = `decision_calibration` reversal accounting + `position_sizing_intent` history.

6. **Decision-journal capture fields** (Kahneman/Parrish/Klein): confidence as an immutable **number** captured pre-outcome; alternatives considered; outcome range; premortem tripwires (user-authored); grade **process not result** ("resulting"/anti-outcome-bias). → Most already in `decisions`/`decision_conditions`; the immutable-numeric-confidence-before-outcome rule is the load-bearing constraint.

7. **Calibration science** (Tetlock/Brier/Murphy): reliability diagram (stated vs realized), Murphy decomp (reliability vs resolution → opposite coaching for same Brier), reference-class forecasting as top technique. → Extend `decision_calibration`; do NOT pitch "true Brier" as net-new (do-not-re-propose).

8. **Voice pipeline = MediaRecorder + VAD-gate + Whisper, NOT Web Speech API.** Web Speech streams audio to Google (breaks local-first) + no raw audio + weak on rambling. **VAD gate before Whisper is mandatory** (Whisper hallucinates 55% of silence as "so"). Disfluency cleanup is an LLM job, not ASR. Unit of interaction = the **LLM-structured "gist," not the transcript** (Rambler: semantic split/merge/zoom). Human-steers-AI (Granola). Push-to-talk, NOT always-on (the Meta/Limitless shutdown = legal/trust minefield). → For no-build stack: in-browser WASM Whisper (tiny live + small re-pass on stop) OR a local WhisperX sidecar via `fetch`; cleanup/split through a governed `dictation_cleanup` purpose.

9. **Typed structure at capture beats both folders and auto-magic** (Tana supertags; Mem's zero-structure failed at scale). → Parse free text into `{ticker, claim_type, direction, conviction_delta}` at capture without a filing step.

10. **Malleable-software safety** (Litt / lethal trifecta / Rule of Two / DGM): request → background agent → **reviewable diff, never live mutation**; the **lethal trifecta is LIVE here** (private financial DB + untrusted fetched content + outbound calls = all 3 legs) → any such agent needs a mandatory human gate; generative UI on the **controlled/declarative** end only (fill slots in the vetted kit, never free HTML/JS); archive + traceable lineage for rollback; **solve the oracle problem** (validate correctness vs ground truth, not "tests green") before granting autonomy.

---

## 5. OPEN DESIGN TENSIONS (the proposals must resolve)

1. **Frictionless capture vs identity-at-write (Law 1).** Stream-of-consciousness wants no `kind`; the house demands a stamped discriminator. Resolve via a `needs_triage`-style "raw/untyped" terminal that IS a legitimate stamped identity (closed-under-no-fit), classified later — not by abolishing identity.

2. **Where does longitudinal synthesis live — and does it decay?** It is NOT an event (must not decay on 30h half-life → wrong for inbox) and NOT an atomic external fact (→ wrong for diet's CHECK-locked vocabulary). Likely a **third non-decaying "insight" lane** borrowing diet's stored-order discipline + a per-insight lifecycle (accept/dismiss/track) the synthesis memo lacks. Must reconcile with "don't re-propose cross-session memory as new" — frame as generalizing the existing stub, not inventing.

3. **Voice fidelity/privacy/cost vs no-build ethos.** In-browser WASM (zero server, multi-GB model download, Chrome-leaning) vs local WhisperX sidecar (a non-vanilla Python process reached by `fetch`). Both keep the *frontend* buildless; pick per the privacy/cross-browser/fidelity bar. Whichever: VAD gate + push-to-talk + retain-audio-locally-with-one-click-delete.

4. **Belief-tracking as deterministic scaffold vs LLM.** Research is unambiguous: drift detection must be deterministic (BSV diff + transition classifier over `position_sizing_intent`/stance history) with the LLM only narrating — adding RAG won't fix drift. Tension with the urge to "just prompt for it."

5. **Coaching cadence: just-in-time nudge vs review-ritual boost.** Tripwire/recency/anchoring fire pre-decision in-context (Socratic flow); calibration/contradiction are cognition-change → the monthly scorecard ritual. N=1 means no crowd averages out a bad signal → frequency governor + min-n gate are load-bearing; over-firing trains dismissal.

6. **Malleable-software ambition vs the lethal trifecta.** A background-agent feature-loop is the boldest gap but all 3 trifecta legs are live. Resolve by: diff-not-mutation, mandatory human merge gate, break-a-leg isolation (the code-writer must not simultaneously hold live DB read + outbound), controlled generative-UI only, and a real oracle for numeric correctness. If these can't be guaranteed, scope the loop down to feedback-capture + clarified backlog (gripe → clarify-before-build, since N=1 has no crowd to average out a bad request).