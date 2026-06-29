# THE LEDGER — PHASE 1 BUILD PLAN

*The Conversational Research Loop + the Reviewable-Proposal Spine*

**Status:** Phase 0 shipped and merged (capture loop, per-holding stance synthesis, `insight_notes` `0118`, governed `theme_synthesis`/`theme_seed_cluster`, the Ledger panel, `/api/capture/text`, Telegram client with `inline_keyboard` + `parse_update`, alembic head **`0119`**). This plan is the implementation directive for Phase 1.

> **WAVE 1 COMPLETE — 2026-06-29.** All 8 tickets shipped + merged: W1-1 tables (#688), W1-2 `wondering_detect` gate (#689), W1-3+W1-4 store/tap + tier resolver (#691), W1-5+W1-6 two-pass engine (#693), W1-5d+W1-7 run route + inbox lane (#695), W1-8 Telegram dispatch (#696). Alembic head **`0124`** (prod migrated). The three **[ENFORCED-GATE]** invariants live as build-failing tests: K1 (no function holds web-fetch + proposal-write — AST guard in `tests/test_research_run.py`), S2 (`call_llm_with_web` per-run cap clamped ≤ the hard ceiling; tier caps all ≤ `WEB_CEILING_USD`), and the quarantine (fetched evidence wrapped as untrusted data). The loop is end-to-end on web **and** Telegram. Research execution is gated **OFF** by default (`LEDGER_RESEARCH_RUN`); detection runs (`LEDGER_RESEARCH_TAP`, default on) so a captured wondering produces an inert chip with zero research spend. ~95 Phase-1 tests green.

**Source of truth:** `directives/the_ledger_build_plan_2026_06.md` §3 (the spec), §5 (purposes + cost/safety), §7 (risks), reconciled against `directives/journaling_thought_partner_2026_06.md` §5–6 (autonomy + trust-zones). Where they diverge, the build_plan is settled.

**House rules in force:** single-user posture (no SaaS drift), the 3 Instrument-Paradigm Laws, every LLM call governed, naive-UTC, `db_paths.resolve_db_path`, **no real FK `REFERENCES`** (code-level RI), no-build frontend, reuse seams over new frameworks. **SAFETY-FIRST: the lethal trifecta (prod-DB-read + web-fetch + action-write) is live — no single process may hold all three.**

**The cardinal rule of this plan:** several trifecta invariants below are load-bearing safety controls, and *prose is not enforcement*. Where the SAFETY model asserts an invariant ("no process holds web+action-write in one hop," "fetched content is inert," "the coder cannot reach prod"), Phase 1 ships a **structural test or a build-order gate that fails the build if the invariant regresses** — never a code-review eyeball. The four places this matters most are flagged inline as **[ENFORCED-GATE]** and collected in §8.

---

## 1. Scope recap — Phase 1 in brief

Phase 0 made the bot a *listener*: a Telegram text like "Ingest this deck and give me insights and critical evaluation: `<URL>`" lands as a `kind="musing"` note via `capture/poller.py:113 → ingest.ingest_capture` and goes no further. Phase 1 makes the bot an *actor* — but a **proposal-only actor**. The owner's test requests will, once built, do this:

- **"Ingest this deck and give me insights and critical evaluation: `<URL>`"** → a regex pre-gate (owner-authored musings only) flags it as a *wondering*, a `research_tasks` row lands in status `proposed` with an inert chip. The owner taps **one** button. A tier-budgeted, web-capable research pass fetches-and-quarantines the deck in a **network-isolated fetch process**, a **separate network-less process** narrates from quarantine and drafts a **memo + evidence** artifact, runs an **adversarial self-assessment** to set tone (assert vs. hedge+Socratic), and surfaces a `research_proposal` in the inbox with four actions — **approve / research-further / steer / reject** — mirrored to the Telegram thread as inline-keyboard buttons (a free-text reply *is* a steer).

- **"Add to watchlist: Dassault Systemes"** → same detect→propose path; the resolved artifact is a **saved-view/thesis** proposal (and, via the evaluation/portfolio floor, an entry-tier diligence memo), never a live write to `saved_views` or the tracker.

**The invariant:** every Phase-1 output is an *inert proposal row* + a *one-tap affordance*. Nothing writes live until the owner approves, and mutating artifacts (DCF, thesis, code) clear a higher bar before the apply button even renders. Full-auto is the published-app vision; Phase 1 is **semi-auto** — detection is automatic, *running* is one tap.

**A naming note carried from the seam maps:** the design context says "feedback classifier / `feedback_triage`," but `grep` confirms `feedback_triage`/`feedback_items`/`is_software_feedback` exist **only in the directives, nowhere in code**. That vocabulary is the owner's *separate Cowork framework*. In *this* repo the shipping classifier is **`wondering_detect`** — it gates the research loop. The `is_software_feedback`-hard-refuse rule still applies, but as a *property of the code-leg classifier* (Wave 4), not a separate Wave-1 subsystem. This plan builds `wondering_detect`; it does **not** build a generic feedback-to-feature loop in Wave 1.

---

## 2. The thinnest end-to-end slice (Wave 1)

The smallest build where a bot request becomes a reviewable inbox proposal the owner approves/rejects in **both** the web inbox and the Telegram thread. **Wave 1 ships exactly one artifact type — memo+evidence** — because a memo is non-mutating: `approve` just persists an already-drafted row, so the entire loop is proven (detect → tier → fetch → draft → adversarial → inbox → Telegram → act) before any mutating artifact or `spawn_task` complexity exists.

```
Telegram text  →  ingest_capture (Phase-0, untouched)  →  musing note lands
      │
      ▼  fire-and-forget tap, regex pre-gate (kind="musing" + owner-authored ONLY)
 wondering_detect (Flash-Lite, default is_wondering=false)
      │  is_wondering=true
      ▼
 research_tasks row (status='proposed') + INERT inbox chip   ← never auto-runs
      │
      ▼  owner taps ONE button  →  POST /research/<id>/run   (feature-flagged off until cap-passthrough test is green)
 tier resolver (deterministic, disk-only)  →  max_budget_usd
      │
      ▼
 PASS 1: research_fetch (Sonnet, call_llm_with_web, hard $-cap)   ── network-capable, NO proposal-write capability
      │  fetch → spotlight-quarantine → quarantine store   (returns ONLY quarantined text)
      ▼
 PASS 2: research_narrate (Sonnet, call_llm, NO web tools)        ── reads quarantine, NO network
      │  narrate-from-quarantine
      ▼
 artifact_draft_memo  →  llm_artifacts row (inert proposal)
      │
      ▼
 research_adversarial_assess (Sonnet)  →  assert | hedge+Socratic
      │
      ▼
 research_proposals row (status='pending', semantic_kind='research_proposal')
      │
      ├─►  inbox 6th lane, 4-action footer (HTMX)
      └─►  Telegram inline keyboard  research:<id>:approve|further|steer|reject
                 approve | research-further | steer | reject
```

**The two-pass split is two distinct processes with no shared writer handle [ENFORCED-GATE].** Pass 1 (`research_fetch`, `call_llm_with_web`) is the *only* leg that touches the network, and it is forbidden by code from importing or holding any handle to the proposal writer — it returns quarantined text and nothing else. Pass 2 (`research_narrate`, `call_llm`, **no web tools, no `--allowedTools` web grant**) is the *only* leg that drafts. This is not a property of how `orchestrate.py` happens to be written; it is asserted by the structural test in §8 (K1). See §4.2.

### The migrations (Wave 1)

Three net-new tables, all chaining off head **`0119_ledger_synthesis_budget`** — pick `down_revision` against the **live linear head at rebase time** per `reference_alembic_number_collisions_parallel_sessions.md`; do **not** hardcode. **No real FK `REFERENCES`** (code-level RI, mirroring `insight_notes` in `0118`).

```sql
-- 0120_research_tasks.py  (the detection→proposal spine)
CREATE TABLE research_tasks (
  id              INTEGER PRIMARY KEY,
  note_id         INTEGER,                 -- code-level RI to notes (the source musing)
  claim           TEXT NOT NULL,           -- the extracted wondering
  ticker          TEXT,                    -- nullable; resolved by wondering_detect
  status          TEXT NOT NULL DEFAULT 'proposed',
                   -- proposed → running → drafted → {approved,rejected,superseded}
  budget_tier     TEXT,                    -- deep|standard|entry, set at run time
  budget_usd      REAL,                    -- the resolved per-run hard cap
  adversarial_verdict TEXT,                -- assert|hedge (null until assessed)
  cost_usd        REAL,                    -- actual, joined back from llm_calls
  run_id          TEXT,                    -- correlation id into llm_calls / audit
  created_at      TEXT NOT NULL,           -- now_iso(), naive-UTC
  updated_at      TEXT NOT NULL
);
CREATE INDEX ix_research_tasks_status ON research_tasks(status);

-- 0121_research_proposals.py  (the reviewable artifact, the inbox source)
CREATE TABLE research_proposals (
  id              INTEGER PRIMARY KEY,
  task_id         INTEGER,                 -- code-level RI to research_tasks
  kind            TEXT NOT NULL,           -- memo|dcf|thesis|view|code (Wave1: memo only)
  ticker          TEXT,
  title           TEXT NOT NULL,
  body_md         TEXT,
  evidence_json   TEXT,                    -- [{claim, fact_ref|news_id|note_id|url}] (Law 2)
  source_note_ids TEXT,                    -- json array, the grounding edge
  status          TEXT NOT NULL DEFAULT 'pending',
                   -- pending → {approved, researching, steered, rejected}
  adversarial_verdict TEXT,               -- assert|hedge
  budget_tier     TEXT,
  provenance      TEXT NOT NULL DEFAULT 'derived',
                   -- 'derived' | 'contains_fetched'  (the inert-content marker)
  tainted_by_proposal_id INTEGER,          -- transitive-taint edge (S4): set when this
                                           --   proposal/its grounding note descends from a
                                           --   'contains_fetched' proposal; NULL if clean
  worktree_ref    TEXT,                    -- code artifact only (Wave 4); NULL here
  superseded_by   INTEGER,                 -- supersede-chain on reject/re-run
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX ix_research_proposals_status ON research_proposals(status);
CREATE INDEX ix_research_proposals_taint  ON research_proposals(tainted_by_proposal_id);

-- 0122_research_hot_flags.py  (the manual "actively-considering" signal)
CREATE TABLE research_hot_flags (
  ticker          TEXT NOT NULL,
  set_at          TEXT NOT NULL,
  expires_at      TEXT NOT NULL            -- time-boxed; OR'd into the tier resolver
);
CREATE INDEX ix_research_hot_flags_ticker ON research_hot_flags(ticker);
```

The `tainted_by_proposal_id` edge is the **transitive prompt-injection firebreak** (S4): a memo derived from a `contains_fetched` proposal inherits the taint even though the *memo's own* provenance is `derived`. The Wave-4 code-leg classifier checks transitive taint, not just the immediate utterance's provenance. See §4.3.

Plus the **governance budget-seed migrations** (one per Wave-1 purpose; see §5), modeled on `0119`'s idempotent `ON CONFLICT DO NOTHING` template. Wave 1 now has **five** LLM purposes (`research_orchestrate` split into `research_fetch` + `research_narrate`), so five seeds.

### The feedback classifier (`wondering_detect`) — regex pre-gate + governed LLM

A **deterministic regex pre-gate** decides whether the LLM fires *at all*. A flat observation ("NU's NPL ticked up") must never be misread as a request and costs zero tokens; the asymmetry is encoded as **default verdict `is_wondering=false`**. Modeled on the structural pre-gate in `src/ask/router.py`.

**The pre-gate enforces the trust zone, not just the vocabulary (P2).** The regex vocabulary (`ingest this | add to watchlist | research this | …`) is an *action-trigger* vocabulary — it will fire on fetched or quoted text, e.g. a pasted deck whose own body reads "ingest this data into your model," or a forwarded quoted message. Because the pre-gate is the thing that decides whether tokens burn, the trust zone is encoded **here**, not only at the Wave-4 code-leg: the pre-gate fires **only** when the note is `kind="musing"` **and owner-authored** (`provenance` is `derived`/`owner`, never `contains_fetched`). A fetched/quoted body can never trip the gate, closing a token-burn + mis-fire vector cheaply.

```python
# src/research/detect.py
_WONDER_RE = re.compile(
    r"\b(do .* still|is .* really|why is|what if|i wonder|"
    r"does .* hold|how .* compare|ingest this|add to watchlist|"
    r"look into|research this|give me insights)\b", re.I)

def detect_wondering(note: Note, *, db_path: str) -> WonderingVerdict:
    # Trust-zone gate FIRST: only owner-authored musings can ever burn tokens.
    if note.kind != "musing" or note.provenance == "contains_fetched":
        return WonderingVerdict(is_wondering=False)        # zero LLM tokens, fetched text inert
    if not _WONDER_RE.search(note.text):
        return WonderingVerdict(is_wondering=False)        # zero LLM tokens
    return call_llm(purpose="wondering_detect", ...)        # Flash-Lite, schema-validated
```

`wondering_detect` (Flash-Lite, budget `skip`) emits the schema-validated `{is_wondering, claim, ticker, suggested_artifacts[]}`. Positive verdict → a `research_tasks` row (`proposed`) + inert chip. **It never auto-runs the research pass.**

### The Telegram callback dispatch (reuse Phase-0 `inline_keyboard`/`parse_update`)

`src/capture/telegram.py` already has every primitive: `inline_keyboard` (its docstring already names the four actions), `send_message(..., reply_markup=)`, `parse_update` decoding `callback_query → Update(kind="callback", callback_data=…, callback_query_id=…)`, and `answer_callback` to stop the spinner. The **one build** is the dispatch branch — `capture/poller.py:144` is the explicit Phase-1 stub: `# callback updates drive the Phase-1 research surface; ignored in Phase 0.`

```python
# execution/capture_poller.py  (poll_once)
if update.kind == "callback":
    prefix, pid, verb = update.callback_data.split(":")   # "research:<id>:approve"
    if prefix == "research":
        _act_on_proposal(int(pid), verb, db_path=db_path)  # SAME core as the HTMX route
        telegram.answer_callback(update.callback_query_id)
elif update.kind == "text" and _is_reply_to_proposal(update):
    _act_on_proposal(pid, "steer", steer_text=update.text, db_path=db_path)  # free-text = steer
```

**One action core, two surfaces:** the inbox HTMX endpoint and the Telegram callback both call `_act_on_proposal` in `src/research/proposals.py`. No logic duplication.

### The research-task model + proposals store

`src/research/proposals.py` follows the codebase store convention exactly (writers own a connection + commit; readers are best-effort `[]` on a missing table — the `signals/store.py`/`alerts/store.py` pattern). It owns the `research_tasks` and `research_proposals` lifecycles and exposes `_act_on_proposal(pid, verb, …)` dispatching the four verbs.

**Why a typed `research_proposals` table and not `queued_actions`** (the inbox-lanes map's hard finding): `queued_actions.alert_id → alerts.id` is the codebase's **one genuine enforced FK** (`alerts/store.py:610`, `PRAGMA foreign_keys=ON`), so every queued action *requires* a parent `alerts` row — a research proposal is not an alert. And `action_kind` is a **closed 4-value CHECK** (`{thesis_update, bear_append, sizing_update, earnings_prep_append}`) that cannot carry the 5 artifact types or the wider 4-action state machine without polluting the alert substrate. So: **build the typed no-FK table; reuse the inbox lane + rendering + Telegram mirror.**

### The inbox surface (reuse, don't rebuild)

Add a **6th lane**: `"proposal"` to `_DEFAULT_KINDS` (`inbox.py:73`) and a `collect_inbox` block reading `research_proposals WHERE status='pending'` into `InboxItem(kind="proposal", semantic_kind="research_proposal", …)`. The standing-item pattern (`windowed=False`, until-only) fits proposals — a 3-day-old proposal is still waiting. The four-action footer is one new branch in `_render_card_footer` (`inbox.py:611`), gated on `semantic_kind == "research_proposal"`, **directly modeled on `_render_memo_actions`** (`inbox.py:675-695`) — proven precedent for "an inbox kind with its own bespoke affordance row from the `.k-chip` kit." Ranking: add `CATEGORY_PROPOSAL` across `inbox_rank.py:76,87,112,258`; a `pending` proposal already gets the `1.5` status multiplier (`inbox_rank.py:269`). **Do NOT route through `signals/store.py`** — it is explicitly non-decaying, pull-only, "never an InboxItem."

### The LLM governance (Wave 1)

Five new purposes, each shipping the **4-registry lockstep** (`LLM_MODELS` `cli.py:101`; `run_llm_evals` GOLDEN/AUDIT; `evals_panel.RUNNABLE_PURPOSES`; `prompt_versions._PROMPT_VERSIONS` + `coverage`) **plus** the 5th non-registry step — the alembic budget seed. The genuine hard-stop is the **per-run `--max-budget-usd`** flag which terminates the call at API-spend; the prompt's "AT MOST N searches" is advisory and untrusted.

**The load-bearing primitive change (the budget map flagged it; it is now its own isolated PR — see P1):** `call_llm_with_web` (`cli.py:876`) takes **no `max_budget_usd` kwarg** today — it hardcodes `CLAUDE_WEB_MAX_BUDGET_USD` ($2.0) into the `cmd` list at `cli.py:951`. So the entry tier ($0.15) and standard tier ($0.50) would *silently run at $2.0* until this is fixed. Wave 1 **adds a `max_budget_usd: float | None` kwarg** that overrides the constant in the `cmd` list. Because this is a shared-primitive change (the news-structurer calls this exact path), it ships **in isolation** (W1-5a) with a regression test that the existing caller still works, before any orchestrate or route code lands. The budget check `_enforce_budget_pre_call` (`cli.py:917`) fires **pre-call / pre-fetch** — so a budget-mode degrade (`warn`) happens before any network egress, not after (M1).

---

## 3. Waves 2–4 — the remaining artifacts behind the higher bar

Wave 1 proved the spine with memo-only. Waves 2–4 are fast-follows on the *same* spine: same tables, same inbox lane, same Telegram mirror, same action core. The ordering is deliberate — **memo first, code-change LAST behind the full safety gate.**

### Wave 2 — the higher-bar gate + the non-mutating-ish artifact

**Build the higher-bar gate FIRST** (before any mutating artifact). The apply affordance renders only when **all three clear, OR an explicit steer authorizes**:

| Gate component | Source |
|---|---|
| **Evidence-gated** | ≥1 concrete `fact_ref`/`news_id`/`note_id`/URL doorway in `evidence_json`, not prose (Law 2). |
| **Adversarial-survived** | `research_adversarial_assess.survives == true`. |
| **Deterministic numeric/oracle** | type-specific (DCF Python recompute; view `from_dict` validation; code golden+CI). |

- **Artifact 4 — saved view** (`artifact_draft_view`, Haiku/Flash, golden set). Drafted `ViewSpec` validated through `viewspec/spec.py:146 from_dict` (hard validation accumulating *every* error, bounded MAX_TICKERS=16/MAX_METRICS=10) but **not written to `saved_views`**. Preview executes **LLM-free** via `viewspec/engine.execute_view`. The NL→spec path exists at `ask/engine.py:280 compile_nl_to_viewspec`. `from_dict` validation *is* the oracle; treated as non-mutating; approve = the real `saved_views.py:39 save_view`. This serves the "Add to watchlist" request.

### Wave 3 — the two mutating data artifacts (DCF, thesis)

- **Artifact 2 — DCF dry-run** (`artifact_draft_dcf`, Sonnet, `skip`, numeric golden). **Schema build the seam map surfaced:** `dcf_runs`/`DcfRunRow` (`dcf/persist.py:34`) have **no `proposed` column**, and `upsert` is literal `INSERT OR REPLACE` keyed on ticker (`persist.py:120`) — one survivor row per ticker. A dry-run therefore needs a **`status`/`proposed` column + a guard so `reprice_runs` and the morning sweep ignore proposed rows** (`_latest_rows` picks `MAX(id)` and would otherwise surface a proposal as live). Recompute is **always Python** via `dcf/reprice.py:87 reprice_runs` — the dry-run *is* the oracle. Diff = old-vs-proposed `assumption_snapshot_json` + `npv_per_share`. **Higher bar mandatory.**

- **Artifact 3 — thesis edit** (`artifact_draft_thesis`, Sonnet, `warn`). `user_state/ledger.py:40 append_entry` is **append-only by construction** (no update/delete path) — the ideal proposal substrate: hold a drafted `ThesisLedgerEntryRow` un-appended; approve = the real `append_entry`. History can't be clobbered. **Nuance from the map:** `decisions` rows *are* mutable (`decision_conditions.py` runs post-hoc `UPDATE`), so the writer must treat `conviction_pct` + `falsifier` as **write-once fields, not the row as frozen** — enforcing row-immutability would break the conditions extractor. **Higher bar mandatory.**

### Wave 4 — the code-change artifact (LAST, behind the full safety gate)

- **Artifact 5 — code/feature** (`artifact_draft_code`, **Opus — the one and only Opus call**, separate `autonomous_build` budget, `skip`/fail-closed). `research_orchestrate` drafts `{spawn_prompt, title, files_touched[], oracle_check}`; the spawned coder produces a worktree + reviewable diff; approve = worktree merge. `spawn_task` exists **nowhere in `src/`** today (directive-only) — this is the highest-build-risk, highest-safety-stakes leg. **Full safety model applies (§4).**

  This is where the **transitive `is_software_feedback`-hard-refuse** lives (S4): the code-leg classifier hard-refuses any utterance whose **transitive taint** is non-null — i.e. it checks `tainted_by_proposal_id` up the derivation chain, not merely the immediate utterance's `provenance`. This severs the longer injection path *fetched-deck → memo → re-utterance ("add this feature") → code-change*, which a naive immediate-provenance check would miss because the re-utterance's own provenance reads `derived`. **Wave-4 precondition test (§8, S4-gate):** "fetched deck → memo → re-utterance → code classifier sees transitive taint → hard-refuse."

### Budget tier, adversarial calibration, iteration

- **Budget tier resolver** (`src/research/tier.py`, deterministic, disk-only, resolves *before* the orchestrate call — the dollar ceiling is the hard safety cap):

| Tier | Trigger | `--max-budget-usd` | Searches | Artifacts |
|---|---|---|---|---|
| **deep** | weight ≥5% OR (hot/spike AND held) | $2.00 | ≤2 | all 5 |
| **standard** | held, no spike | $0.50 | ≤1 | memo/view/thesis |
| **entry** | evaluation/unheld/cold | $0.15 | 0 | memo only |

Inputs: (a) weight via `read_materialized_weights` (`portfolio_weights.py:88`) through the `inbox_rank._materialized_weights` seam (`inbox_rank.py:320`) — **never** the live tracker; (b) actively-considering = **OR**(manual `research_hot_flags`, musing-count z-score via `timeseries/primitives.py`). The evaluation/portfolio **floor** comes from `list_type_reconcile.py` (`DEMOTION_LIST_TYPE="evaluation"`, `:59`) so unheld names still get entry-tier diligence.

  The resolved `budget_usd` must reach the subprocess `cmd` as `--max-budget-usd <tier>`. **This is a build-order gate, not a discipline (S2, §8 S2-gate):** the `POST /research/<id>/run` route **raises** if `tier.budget_usd` cannot be passed through to `call_llm_with_web`, and an assertion-test confirms an `entry`-tier run constructs a `cmd` containing `--max-budget-usd 0.15` (not `2.0`). Until that test is green the run button stays un-rendered behind a feature flag (W1-3 ships chip-only — encoded as a flag, not as discipline).

- **Adversarial assert-vs-Socratic calibration** (`research_adversarial_assess`, Sonnet, human-rubric eval only): a second pass tries to **refute the loop's own claim**, emitting `{survives, strongest_counter, residual_uncertainty, recommended_tone, socratic_q?}`. Survives → **assert** + apply-eligible. Fails → **hedge + Socratic question**, downgraded to a read-only insight on the **non-decay lane** — *confirmed present* in `signals/store.py:8-15`. **Same-model caveat (M2):** `adversarial_assess` is Sonnet judging `artifact_draft_memo`, also Sonnet — same-model self-critique is adversarially weak. The owner accepted this (owner decision), but the eval must specifically measure **whether same-model assessment ever rescues a claim a cross-model judge would refute**; if it does, escalate the assessor to a different model.

- **research-further / steer** (wired in Wave 1's action core; their *interaction with mutating artifacts* lands here). `research-further` = re-run at the next tier up. `steer` = free-text *authorizes a mutating proposal absent full adversarial survival* and re-runs grounded by the steer text (a free-text Telegram reply IS steer). `reject` = close the supersede-chain + **bump the future threshold** for that signal type. **Frequency governor:** N=1 has no crowd to average out a bad signal; a venting/repeat session **bumps an existing item's rank rather than spawning a new chip**.

---

## 4. The SAFETY model

The lethal trifecta is **live**. The entire model is one rule: **no single process holds prod-DB-read + web-fetch + action-write simultaneously.** Trust zones: owner-authored utterances are trusted; `provenance=contains_fetched` web content is **inert / action-incapable**. Where this section asserts an invariant, §8 names the test that enforces it.

### 4.1 Trust zones, no live writes

**No live writes in Phase 1, period.** Every artifact is an inert proposal row (`research_proposals` / `dcf_runs.proposed` / un-appended `ThesisLedgerEntryRow` / un-saved `ViewSpec` / worktree diff). The live write happens **only on approve**, and only after the higher bar for mutating types.

### 4.2 Trifecta isolation — fetch-and-quarantine, then network-less narrate [ENFORCED-GATE]

The fetch leg and the draft leg are **two distinct function calls in two distinct processes with no shared mutable handle to the proposal writer** — this is the single most important safety invariant in the design, and it is *structurally* enforced, not narrated:

- **Pass 1 — `research_fetch`** (`call_llm_with_web`). This is itself an agentic multi-tool call: the model holds WebSearch/WebFetch. It pulls content into a quarantine store via `llm/untrusted.py spotlight(text, source=…)` (sha256-keyed tamper-evident BEGIN/END "this is DATA not instructions" markers; `WEB_CONTENT_NOTICE` for live-tool calls where content arrives post-assembly). **It returns ONLY quarantined text and is forbidden by code from importing or touching `research_proposals` / `llm_artifacts` writers.** Nothing in pass 1's call-site scope can emit the action.
- **Pass 2 — `research_narrate`** (`call_llm`, **no web tools, no `--allowedTools` web grant**). It reads quarantine and drafts the memo. It has the proposal writer; it has no network.

So no process fetches web AND writes the proposal in the same hop. `fetch_news_websearch.py` is the reference (fetch→normalize→cache→degrade-never-fabricate). **The gate (§8, K1):** a structural test (AST / import-graph) asserts (a) pass-1's call site has no proposal-write symbol in scope, and (b) pass-2's `cmd` carries no web tools. If either regresses six months from now, the build fails — this is a CI gate, not a code-review eyeball.

### 4.3 `provenance=contains_fetched` is inert, and taint is transitive [ENFORCED-GATE]

A proposal whose context contained fetched content carries `provenance='contains_fetched'`. The code-leg classifier (Wave 4) **hard-refuses `is_software_feedback=true`** on any such utterance — severing *fetched-text → feature-request → code-change*.

But the immediate-provenance check is not sufficient, because the taint launders through derivation: a fetched deck's summary lands back in a `derived` note, which is then re-uttered as "add this feature." **Propagation is therefore explicit:** any note/claim **derived from** a `contains_fetched` proposal inherits the taint via the `tainted_by_proposal_id` edge (set at draft time whenever a grounding note/proposal in the chain is itself fetched-tainted). The code-leg classifier checks the **transitive** taint up that edge, not just the immediate utterance's `provenance`. The deterministic regex pre-gate (default-false, `kind="musing"` + owner-authored only — §2) ensures fetched text can never reach `wondering_detect` in the first place, so this is defense-in-depth, not the only line. **The gate (§8, S4-gate):** the Wave-4 precondition test "fetched deck → memo → re-utterance → code classifier sees transitive taint → hard-refuse."

### 4.4 Autonomous vs. one-click

| Action | Posture |
|---|---|
| Wondering **detection** (regex + `wondering_detect`) | **Autonomous** (fire-and-forget; produces an inert chip only; owner-authored musings only). |
| Running the research pass | **One-click** (semi-auto; owner taps; tier-budgeted; run button feature-flagged until the cap-passthrough test is green). |
| Approve a **non-mutating** artifact (memo, view-preview) | **One-click** (no higher bar). |
| Approve a **mutating** artifact (DCF, thesis, code) | **One-click ONLY after the higher bar clears** OR an explicit steer authorizes. |
| The spawned coder (Wave 4) | **Sandboxed-autonomous** inside its isolated worktree; output is a reviewable diff, never a live write. |

### 4.5 The scrubbed-fixture-DB binding for the code leg [ENFORCED-GATE]

The Wave-4 coder is a **three-way split that breaks the trifecta**:

- **Spawned coder** → binds to a **SCRUBBED FIXTURE DB**, runs **network-less**. It has action-write (its worktree) + DB-read (fixture only), **no web**. Fixture means no prod secrets/PII reach it.
- **Web-fetch agents** → fetch + quarantine, no action-write.
- **Narrate pass** → reads quarantine, no network.

**The worktree-resolves-to-MAIN footgun is REAL** (`reference_worktree_deregistered_after_merge.md`): after a merge, an orphaned `.claude/worktrees/<name>/` can deregister and resolve UP to MAIN's `.git` on branch `main` — which holds the owner's live untracked files. A coder assuming its bind could thus **live-write prod**.

The mitigation is **capability-by-construction, with assertions as a tripwire, not the control:**

1. **Construct the coder's env so prod is unreachable.** Spawn with a temp `HOME`/config dir whose `resolve_db_path` *can only* resolve to the scrubbed fixture — a capability the coder cannot name, it cannot misuse. The test asserts that resolving the prod path *from inside the spawned env* raises `FileNotFoundError`.
2. **Tripwire assertions before spawn** (normalized for the win32 case-insensitive FS — `endswith` on a raw string is fragile to separators/case):

```python
import os
# normalize both sides through normcase+realpath before comparing (win32, case-insensitive FS)
gitdir = os.path.normcase(os.path.realpath(git_rev_parse_git_dir(worktree)))
expected = os.path.normcase(os.path.realpath(os.path.join(repo, ".git", "worktrees", name)))
assert gitdir == expected                                    # not <repo>/.git (MAIN)

# the REAL control is the constructed env, not this equality:
assert db_paths.resolve_db_path(coder_env) == SCRUBBED_FIXTURE_DB   # tripwire
# and, asserted from inside the spawned env, prod must be UNREACHABLE:
#   resolve_db_path(prod_path) under coder_env  → FileNotFoundError
```

The chip carries **no pre-granted capability.** (§8, S3-gate.)

### 4.6 The red merge path for safety-control diffs

The oracle gate renders **before** the merge button: "tests green" is insufficient for numeric correctness, so any diff touching valuation/KPI/financial math validates the affected `fact_ref` against ground truth (diff-aware CI + golden numeric) before the apply affordance appears.

A diff touching a **safety control** — the redactor, retention TTLs, kill switches, provenance tagging, the trifecta isolation (the §8 gates themselves), or `src/llm` governance — renders a **distinct RED merge path** requiring explicit acknowledgement that it modifies a safety control. **Never routine one-click.**

### 4.7 Budget as a safety control

`research_fetch` (synchronous tap) is `warn` monthly → degrade to a read-only proposal, never break mid-tap; the per-run `--max-budget-usd` is the genuine ceiling, and the degrade fires **pre-fetch** (the `_enforce_budget_pre_call` check at `cli.py:917` is pre-call, so no network egress occurs on a degraded call — M1). `autonomous_build` (the code leg) is `skip`/fail-closed with a **separate weekly count-cap** living *outside* `llm_calls` — the one real dollar hole, and **it is governed by a real persisted row + a fail-closed check with its own test (K2)**, not an out-of-band counter that nothing tests. See §4.8 and §8 (K2-gate).

### 4.8 The `autonomous_build` count-cap (the one ungoverned-spend path) [ENFORCED-GATE]

The code leg's spend does not flow through per-call `llm_calls` budget the way the read legs do — it is a *count* of expensive Opus build runs per week. An out-of-band counter that nothing tests is a counter that's zero forever. Wave 4 therefore ships:

- a **real persisted row** (e.g. `autonomous_build_counter(week_iso, count, updated_at)`, its own migration) — not an in-memory tally;
- a **fail-closed check** that the Nth+1 build this week is blocked, with **its own test**: "11th build this week → blocked";
- a **kill criterion**: kill the code leg if the cap can be bypassed by a process restart or a concurrent race (the check reads-and-increments atomically under the writer's connection, mirroring the store-commit convention).

(§8, K2-gate.)

### 4.9 Audit-log privacy

`capture_audit_log` (Phase-0) extends to carry the research trail — utterance-summary → claim → adversarial outcome → assert/hedge → cost. **Never raw prompt bodies** (`prompt_sha256` + char count only); coded outcomes from the `is_hard_stop` taxonomy (`ok|budget_block|setup_error|transient_degraded|adversarial_failed|oracle_rejected`); join to `llm_calls` on `prompt_sha256`+`run_id`.

---

## 5. The new LLM purposes table

Each purpose ships the 4-registry lockstep + a budget-seed migration (template: `0119` for `skip`, `0104` for `warn`). Budget-mode rule: synchronous user-facing → `warn` (degrade); autonomous → `skip` (fail-closed). **Opus exactly once** (`artifact_draft_code`); everything else Sonnet or cheaper.

| Purpose | Model (tier) | Budget mode + cap | Governance / eval | Wave |
|---|---|---|---|---|
| `wondering_detect` | **Flash-Lite** (gate, behind regex) | `skip` | golden set (real wondering vs flat observation) — highest-value eval | 1 |
| `research_fetch` | **Sonnet** (`call_llm_with_web`; web-capable, **no proposal-write capability**) | **`warn`** monthly; per-run `--max-budget-usd` hard-cap = genuine ceiling, **degrade pre-fetch** | Mode-B rubric | 1 |
| `research_narrate` | **Sonnet** (`call_llm`; **no web tools**; holds the proposal writer) | `warn` | Mode-B rubric | 1 |
| `research_adversarial_assess` | **Sonnet** (*not* Opus — owner decision; same-model caveat M2 in §3) | `skip` | human rubric + spot-check + same-model-rescue metric | 1 |
| `artifact_draft_memo` | **Sonnet** | `warn` | Mode-B rubric | 1 |
| `artifact_draft_view` | **Haiku/Flash** (closed vocab) | `warn` | golden set (`from_dict` is the oracle) | 2 |
| `artifact_draft_dcf` | **Sonnet** | `skip` | numeric/oracle golden (Python recompute) + prose rubric | 3 |
| `artifact_draft_thesis` | **Sonnet** | `warn` | Mode-B rubric | 3 |
| `artifact_draft_code` | **Opus** (`claude-opus-4-8`) | `skip`, on a **separate `autonomous_build` row** (distinct attribution + weekly count-cap *outside* `llm_calls`, persisted + fail-closed + tested — §4.8) | human review + oracle gate + diff-aware CI + transitive-taint hard-refuse | 4 |

`research_orchestrate` from the prior draft is **split into `research_fetch` + `research_narrate`** to make the trifecta isolation a property of two governed call sites rather than one module's internals. Generation purposes may be **governance-registered without a golden eval** (the Phase-0 `theme_synthesis`/`theme_seed_cluster` precedent — registered in all 4 surfaces + budgeted, no golden required). STT stays OFF this table (local faster-whisper, $0). Tier discipline matches `directives/cheapest_model_routing.md`: classification/narration → Haiku/Flash; user-facing voice + synthesis + orchestration → Sonnet; Opus once.

---

## 6. First concrete tickets — Wave 1 PRs

Ordered small PRs. Each names the modules it touches + its test + kill/keep checkpoint. **One PR per phase**, cherry-picked onto fresh `main` after each merges. **Nothing renders without the row, so the migration ships first.** The prior draft's W1-5 bundled a shared-primitive change, the tier passthrough, the two-pass orchestrate, AND an HTTP route in one diff — violating the plan's own "one PR per phase" rule and putting the whole risk surface in a single review. It is **split into W1-5a…W1-5d** below.

| # | PR | Touches | Test | Kill / keep |
|---|---|---|---|---|
| **W1-1** | `research_tasks` + `research_proposals` (+`tainted_by_proposal_id`) + `research_hot_flags` migrations | `alembic/0120`–`0122` (pick `down_revision` at rebase) | migration up/down round-trip; `resolve_db_path` smoke | **Keep** if `alembic upgrade head` clean on a fixture DB + no FK `REFERENCES`. |
| **W1-2** | `wondering_detect` purpose: trust-zone+regex pre-gate + 4-registry + budget seed | `src/research/detect.py`, `cli.py:101` (`LLM_MODELS`), `run_llm_evals`, `evals_panel`, `prompt_versions`+`coverage`, `alembic` budget seed, `evals/golden/wondering_detect.json` | golden set (wondering vs flat observation); **pre-gate fires only on `kind="musing"` + owner-authored, never on `contains_fetched`/quoted text**; 3 sync guards green | **Kill** if the golden set can't separate "NU NPL ticked up" (false) from "do NU's margins still hold?" (true) at usable precision — the asymmetry is the whole point. |
| **W1-3** | Detection tap (fire-and-forget) + `research_tasks` write + inert chip + **run-button feature flag (default OFF)** | `src/capture/ingest.py`, `src/ask/engine.py` (`respond_turn`), `src/research/proposals.py` | tap fires off-path; positive verdict writes a `proposed` row; **never auto-runs**; run affordance hidden behind flag | **Keep** if a test request produces a chip and *zero* research spend, and the run button does not render. |
| **W1-4** | Budget tier resolver | `src/research/tier.py` (reuses `_materialized_weights`, `timeseries/primitives.py`, `research_hot_flags`) | deterministic tier table; disk-only (no live tracker, no LLM) | **Keep** if tiers resolve from disk only and the $-cap is set *before* any call. |
| **W1-5a** | `call_llm_with_web(max_budget_usd=…)` kwarg + passthrough (shared-primitive change, **in isolation**) | `src/llm/cli.py:876/951` | **entry-tier cmd contains `--max-budget-usd 0.15`, not `2.0`**; per-run cap hard-terminates a runaway; **regression test: existing news-structurer caller still works** | **Kill** if the per-run cap doesn't hard-terminate, or the existing web caller regresses. |
| **W1-5b** | Tier resolver → subprocess passthrough wiring | `src/research/tier.py`, `src/research/orchestrate.py` (cmd assembly) | each tier constructs the matching `--max-budget-usd`; **route helper raises if `budget_usd` can't be passed through** | **Keep** if no tier can run at the wrong cap; **Kill** if any tier silently falls back to `2.0`. |
| **W1-5c** | Two-pass split: `research_fetch` (web, no writer) + `research_narrate` (no web, writer) + `artifact_draft_memo` + spotlight quarantine | `src/research/orchestrate.py`, `src/llm/untrusted.py` (reuse), budget seeds | fetch→quarantine→network-less-narrate; memo lands in `llm_artifacts` with ≥1 evidence doorway; **structural test: pass-1 site has no proposal-write symbol in scope; pass-2 cmd has no web tools (K1-gate)** | **Kill** if any process holds web+action-write in one hop (the structural test fails the build). |
| **W1-5d** | `POST /research/<id>/run` route + flag flip | `execution/comments_server.py` (route) | route resolves tier, runs two-pass, returns proposal; **route raises if cap can't be passed (S2-gate); run button renders only when W1-5a's cap test is green** | **Keep** if the run button renders and a tapped run produces a proposal at the correct tier cap. |
| **W1-6** | `research_adversarial_assess` + tone calibration | `src/research/adversarial.py`, registries | `{survives,…}` schema-validated; survives→assert, fails→hedge+Socratic→non-decay lane; **eval measures same-model-rescue rate (M2)** | **Keep** if a refuted claim downgrades to read-only (never surfaces an apply button). |
| **W1-7** | Inbox 6th lane + 4-action footer | `src/dashboard/inbox.py:73/611`, `inbox_rank.py:76/87/112/258`, `acted_span` parity | proposal renders in the lane; four buttons emit; HTMX swap returns `acted_span` | **Keep** if a `pending` proposal renders with all four actions and ranks via `annotate_and_rank`. |
| **W1-8** | Telegram callback dispatch + free-text=steer + audit trail | `execution/capture_poller.py:144` (the stub), `src/capture/telegram.py` (reuse), `capture_audit_log` extension | `research:<id>:<verb>` routes to the **same** `_act_on_proposal` the HTMX route calls; free-text reply = steer; audit logs sha256 not body | **KEEP = Wave-1 done:** the bot request → reviewable proposal works end-to-end in **both** surfaces. |

After W1-8, the slice is complete: **"Ingest this deck: `<URL>`" → tap → reviewable memo proposal you approve/reject in the web inbox and the Telegram thread.**

---

## 7. Open risks + decisions for the owner

1. **`call_llm_with_web` has no per-run budget kwarg today** (`cli.py:876` hardcodes `CLAUDE_WEB_MAX_BUDGET_USD=$2.0` at `cli.py:951`). Tiered caps ($2/$0.50/$0.15) are *impossible* until W1-5a adds the override — until then entry/standard tiers would silently run at $2.0. This is the load-bearing hard-stop for the whole cost+trifecta model. **Decision (settled in this plan):** W1-5a lands the kwarg + passthrough test *in isolation* before any orchestrate/route code; the W1-3 run button stays behind a feature flag that only flips when the entry-tier `--max-budget-usd 0.15` assertion is green. *Do not* ship the detection tap to auto-runnable state before the cap exists.

2. **The DCF `proposed` lane is a real schema build, not a seam** (`dcf_runs` is single-survivor `INSERT OR REPLACE`; `_latest_rows` picks `MAX(id)`). A proposal row would surface as the *live* DCF unless guarded. **Decision:** Wave 3 — new `status` column **+** guard `reprice_runs`/the morning sweep to exclude proposed rows. Sibling staging table is the fallback if the guard proves leaky.

3. **`spawn_task` is unbuilt** (directive-only, nowhere in `src/`). The Wave-4 code leg is the highest build-risk *and* highest safety-stakes leg. **Decision:** defer Wave 4 until Waves 1–3 are dogfooded; treat the scrubbed-fixture-bind + worktree assertion (capability-by-construction, §4.5) **and** the persisted, tested `autonomous_build` count-cap (§4.8) as hard preconditions with their own tests, not afterthoughts.

4. **Migration numbering drift:** the build_plan §3 cites Phase-0 head as `0114`; the real head is **`0119`**. **Decision:** always pick `down_revision` against the live linear head at rebase time (`reference_alembic_number_collisions_parallel_sessions.md`) — never hardcode `0114` or `0120`.

5. **`wondering_detect` precision is the make-or-break eval.** Over-firing trains dismissal (N=1 has no crowd to average a bad signal). **Decision:** the golden set must encode the false-default asymmetry explicitly; if precision is marginal, raise the regex pre-gate specificity rather than loosening the LLM verdict.

6. **The `feedback_triage`/`feedback_items` vocabulary is the owner's separate Cowork framework**, not this repo. **Decision:** confirm Phase 1 ships `wondering_detect` (research loop) only; the generic feedback-to-feature backlog projection to `directives/platform_backlog.md` stays deferred (journaling-doc Phase 4), surfacing here *only* as the code-leg classifier's transitive-taint hard-refuse property.

7. **Frequency governor calibration** (bump-existing vs. spawn-new; reject bumps future threshold) is unproven at N=1. **Decision:** ship the simplest version in Wave 1 (dedup by claim fuzzy-norm, reuse `inbox._fuzzy_norm`), tune thresholds after a week of real dogfood rather than guessing up front.

8. **Same-model adversarial assessment** (`research_adversarial_assess` Sonnet judging `artifact_draft_memo` Sonnet) is adversarially weak. **Decision (owner-accepted):** keep Sonnet for now, but the W1-6 eval must measure whether same-model assessment ever rescues a claim a cross-model judge would refute; escalate the assessor to a different model if that rate is non-trivial.

---

## 8. The enforced safety gates (the prose-to-test conversion)

These are the invariants that must **fail the build** if violated. None is a code-review eyeball; each is an automated test or a build-order gate, and each lands in the named PR. Diffs touching any of these gates take the RED merge path (§4.6).

| Gate | Invariant it enforces | Mechanism | Lands in |
|---|---|---|---|
| **K1-gate** | No process holds web-fetch + proposal-write in one hop (§4.2). | Structural test (AST / import-graph): pass-1 (`research_fetch`) call site has **no proposal-write symbol in scope**; pass-2 (`research_narrate`) `cmd` carries **no web tools / no `--allowedTools` web grant**. | W1-5c |
| **S2-gate** | A run never executes at the wrong dollar cap (§3 tier resolver). | Assertion-test: an `entry`-tier run constructs a `cmd` containing `--max-budget-usd 0.15`, not `2.0`. `POST /research/<id>/run` **raises** if `tier.budget_usd` can't be passed through. Run button feature-flagged until this is green. | W1-5a / W1-5d |
| **K2-gate** | The `autonomous_build` weekly count-cap cannot be silently zero or bypassed (§4.8). | Persisted counter row + fail-closed check + test "11th build this week → blocked"; atomic read-increment under the writer connection; kill the code leg if a restart/race bypasses it. | Wave 4 |
| **S4-gate** | Fetched content cannot launder into a code-change through derivation (§4.3). | Transitive-taint test: "fetched deck → memo → re-utterance → code classifier reads `tainted_by_proposal_id` chain → hard-refuse." Plus the W1-2 pre-gate test (fetched/quoted text never trips `wondering_detect`). | W1-2 (pre-gate) / Wave 4 (transitive) |
| **S3-gate** | The Wave-4 coder cannot reach prod (§4.5). | Capability-by-construction: spawned env's `resolve_db_path` can *only* resolve to the scrubbed fixture; test asserts the prod path raises `FileNotFoundError` from inside the spawned env. `normcase(realpath(...))`-normalized git-dir tripwire assertion before spawn. | Wave 4 |

---

**Bottom line:** Phase 1 is the proposal-only spine — detection automatic, running one-tap, every output an inert row behind a one-tap affordance, mutating artifacts behind a higher bar, code-change dead last behind the full safety gate. The load-bearing change from the prior draft is that the trifecta invariants are now **enforced, not asserted in prose**: the two-pass web/write split (K1), the per-run cap passthrough (S2), the `autonomous_build` count-cap (K2), the transitive fetched-taint firebreak (S4), and the coder's prod-unreachability (S3) are each a structural test or a build-order gate that fails the build on regression, and W1-5 is split into four small PRs so no single diff carries the whole risk surface. Files load-bearing to the fixes: `src/llm/cli.py:876-951` (the missing kwarg + web tool grant + pre-call budget check), `src/alerts/store.py:610` (the enforced FK that justifies the new typed table), and the to-be-built `src/research/orchestrate.py` (where the two-pass split must be structurally enforced, not narrated).