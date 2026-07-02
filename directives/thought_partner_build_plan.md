# Thought Partner — build plan

**Status:** design agreed via Grill-Me (2026-07-01). Vocabulary in [`DEFINITIONS.md`](../DEFINITIONS.md). Supersedes the influence-flag feature (PR #701).
**Reconciled against `main` @ `9d8cb37` (alembic head `0126_research_proposal_artifact`) on 2026-07-01** — parallel Ledger work (#702–#709) landed a full research-artifact + higher-bar-gate system and `decision_capture`/`drift`; this plan reuses that as the "incorporate-into-research" downstream. `conviction` is already an entrenched term (a 1–5 rating), so the Worldview belief-unit is named **Tenet**, not Conviction.

## 1. Framing

The merged influence flag was a filing action: it classified a Telegram drop by surface form (bare URL → `influence`, sentence → `musing`) at ingest time and did one thing — inject a passive line into the chat prompt. That is the opposite of what the platform should be.

The platform is a **Thought Partner**: a capture is raw material for thinking, not a record to file. The LLM does the work first — **extract → explore (Socratic) → synthesize** — and **storage is the last step**. What persists is not the deck; it's the distilled belief. The durable output is a **Worldview** — an evolving set of **Tenets** about how the owner invests — that grows every time something is fed in and *subtly* conditions how the program reasons about holdings.

Two layers:
- **On My Mind** — the reverse-chron living feed of what's being read/thought, indexed to themes / holdings / positioning, with the action ladder **dismiss · save-for-later · discuss · incorporate-into-research**. Transient, high-volume, LLM-free on the write path.
- **Worldview** — durable **Tenets** distilled *from* the feed. The system proposes revisions the owner approves; contradictions are flagged; solidified Tenets flow into decision reasoning.

On My Mind **absorbs the Wondering flag** (it becomes one signal — a badge — inside a feed item) and **reuses the existing research loop** (`src/research/`) as the "incorporate-into-research" rung. That loop is now much richer than when this plan started: `proposals.py` + `higher_bar.py` gate mutating applies, and there are drafted artifacts (`view_artifact`, `thesis_artifact`, `dcf_artifact`, `code_artifact`) plus `decision_capture.py`/`drift.py`. Nothing there is thrown away; it becomes the downstream an On My Mind item hands into.

## 2. Architecture

### Data model — reuse the spine, add almost nothing

| Concern | Where it lives | New schema? |
|---|---|---|
| On My Mind feed items | `analyst_notes` — thoughts as `kind='musing'`, saved readings as `kind='observation'`, filtered to `source='capture'` | **No.** `item_type` / `ladder` / `themes` / `holdings` / `positioning` / `thread_ref` ride in `context_json` (no CHECK) |
| Action-ladder state | `analyst_notes.context_json.ladder` + existing `status` (dismiss → `archived`) | No |
| Tenets (Worldview) | `insight_notes` with `kind='tenet'` (one-line `INSIGHT_KINDS` edit — `insight_notes.kind` has **no** DB CHECK) | No migration for the kind |
| Belief revision | existing `supersede` chain in `record_insight`; machine-proposed revisions land `status='proposed'` until owner approves | No |
| Provenance | existing `source_note_ids` (the musing/reading ids that formed the Tenet) + the citation-validation gate from `theme_synth.py` | No |
| Tension / contradiction | deterministic **overlap check at distill time** (same `scope_key` slug) surfaced for reconcile; recorded in `meta_json.tensions` | No (no background drift engine in v1) |
| Dedup / staging | existing `raw_capture_sessions` (channel, external_ref) | No |
| Socratic "discuss" thread | existing `chat_session` (`data/report_chats/<T>/<date>.json`) for ticker scope; `ask_turns` for portfolio scope | No |

**Migrations (after head `0126`):** exactly **one required** — `0127` retires the influence kind (backfill `influence` rows → `observation` **before** narrowing the CHECK, because `0125`'s downgrade only narrows when zero influence rows remain). A second optional `0128` seeds the `tenet_distill` LLM-budget row (mirrors `0119`). Pick numbers/`down_revision` at rebase time on the then-current head (parallel-session collision risk — this already bit once: head moved 0125→0126 mid-plan).

### The cheap triage gate (cost control)

Metered LLM spend is confined to **two purposes**, each pre-gated so nothing runs on every capture or every render:
- `detect_wondering` — already behind a **regex + trust-zone** pre-gate; runs only on owner-authored landed *thoughts*. Readings/docs/URLs never enter this path.
- `tenet_distill` — behind a **deterministic $0 triage** (only owner-`incorporated`/`saved` items or theme-clusters past a watermark) + `on_exceed='skip'` + an **owner-tapped** job. Never automatic.

### Safety firebreak (fetched content)

A reading's fetched body (PDF/URL text) *does* reach the LLM in the **discuss/distill** flows — that's the "explore a deck with an LLM" use case. It carries a `contains_fetched` provenance tag and **never** enters the wondering/research-write path, so untrusted content can't trip a research write. This composes with the transitive-taint injection firebreak added in #708. The Tenet anchor stays inside the `llm.untrusted` spotlight wrap.

### Fate of PR #701

**Keep** the plumbing (Telegram document download to `capture/docs/`, `external_ref` dedup, `_extract_url`, the `Update` document fields) — it's transport, reused as the capture mechanism for readings. **Drop** the classification: `ingest_influence` → `ingest_reading` (lands `kind='observation'`, LLM-free, **now with the PII scrub** the reading path is currently missing); remove `load_investor_influences_anchor` + header/cap and its lone `chat_session` consumer (atomic swap — leave chat without the influences block; the Worldview anchor arrives in P3). **Backfill** existing `influence` rows → `observation` in `0127`.

## 3. Phased plan

One PR per phase; each independently shippable; P1–P2 need no migration and reuse existing tables, so visible value lands fast.

| Phase | Goal | PR scope | Migration | Flag |
|---|---|---|---|---|
| **P0 — Retire #701** | Stop building on the rejected ingest-time classification. | Backfill `influence`→`observation`; narrow the CHECK; `ingest_influence`→`ingest_reading` (+PII scrub); drop the influence anchor atomically with its `chat_session` consumer. Tree stays green, vocabulary retired. | **`0127`** (backfill-then-narrow) | none |
| **P1 — On My Mind feed + action ladder** | The front-of-funnel: one reverse-chron feed (thoughts + readings) with the 4-rung ladder, web + Telegram. Absorb the Wondering flag as an inline badge. | `src/onmymind/feed.py` (keyset-paginated read model, `source='capture'` filter, **batched** wondering-task lookup), feed renderer folded into `ledger_panel.py` (relabel Ledger → "On My Mind", keep `panel_id='musings'`), action routes in `comments_server.py`, `om:` Telegram callbacks in `research_notify.py`. Incorporate reuses the tap's task **idempotently per `note_id`**. Aware of #709 `decision_capture` (which already links decisions back to musings). | none | `LEDGER_ONMYMIND` |
| **P2 — Tenet store + review** | The durable Worldview (not yet injected). Owner-typed Tenets are `current` immediately; machine-distilled ones land `proposed` for one-tap approval. Belief revision via supersede chain; tension = distill-time overlap surfaced for reconcile. | `src/synthesis/tenets.py` (CRUD over `insight_notes` kind=`tenet`), `worldview_panel.py` review tab, `/api/tenets` routes, `tenet_distill` distill job behind the $0 triage. | `0128` (budget seed) | `LEDGER_WORLDVIEW` |
| **P3 — Worldview injection** | Close the loop: `load_worldview_anchor` (only `actionable` Tenets) rides into the decision-point prompts as subtle, dated, spotlight-wrapped considerations. | `load_worldview_anchor` in `anchors.py`; wire `compose_anchor_block`'s renamed 5th slot at the decision-point sites first (chat / socratic / workspace), expand later. Tight char cap (~1200), deterministic rendering to preserve the prompt cache. | none | `LEDGER_WORLDVIEW_ANCHOR` |

**Trifecta build-failing tests to keep green through P1:** `test_research_run.py::test_no_function_holds_both_web_and_write` (K1) + the quarantine test, and `test_research_tier.py` S2 $-cap clamp. The incorporate button (P1) and the auto-tap must converge idempotently per `note_id` or one wondering yields two research_tasks (`create_task` has no uniqueness guard).

## 4. What was cut (adversarial pass)

- **7 phases / 4 flags → 4 phases / 2 flags.** The owner is the release manager; "roll back" = `git revert`. Flags kept only where a half-built surface would otherwise be visible.
- **Background drift-detector → distill-time overlap check.** No unbudgeted automated belief-revision engine; contradictions surface at the moment of distillation, deterministically. (`drift.py` from #709 handles *position*-conviction drift, a different thing.)
- **Approval workflow scoped to machine-proposed Tenets only.** A Tenet the owner typed is already approved; only *distilled* ones need the one-tap `proposed`→`current` step.
- **Injection scoped to decision-point prompts first**, not all ~9 governed prompts, to bound cache-bust/token cost.

## 5. Decisions (owner, resolved 2026-07-01)

1. **Belief-unit store — RESOLVED: own store (`kind='tenet'`).** A Tenet (a belief about *how you invest*) is modeled distinctly from a `theme` (a topic you track), a `stance` (a call on one holding), and the entrenched `conviction` *rating* (1–5 confidence on a name).
2. **Injection scope — RESOLVED: decision-point prompts first** (chat / Socratic / workspace), expand later.
3. **Telegram "discuss" hand-off on mobile — ESCALATED to the self-hosting track.** See [`self_host_scoping.md`](self_host_scoping.md) + [`self_host_phase1_laptop.md`](self_host_phase1_laptop.md). Access = Tailscale (private mesh); immediate host = the laptop kept on (closed-lid, never-sleep) via Phase-1; a dedicated N100/VPS is a deferred upgrade. Until Phase-1 lands, the discuss reply degrades to "continue in the web thread" (desktop).
4. **Naming — RESOLVED: `Conviction` → `Tenet`** to avoid collision with the existing `conviction` rating (surfaced during the 2026-07-01 reconciliation).
