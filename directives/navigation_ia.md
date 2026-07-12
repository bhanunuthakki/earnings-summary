# Navigation & Ritual Specification — the investment-partner IA (v2, post-adversarial)

**Status: DRAFT v2 for owner sign-off.** v1 (2026-07-11) transcribed the external UX
feedback into a spec; it was then put through a four-lens adversarial loop (behavioral/DB
evidence · per-move product attack · migration red-team · minimal-alternative), and the
load-bearing agent claims were independently re-verified against `data/portfolio.db` and
the code. This v2 is what survived. Per repo rules, nothing here governs until the owner
approves it.

Companion to [`directives/design_language.md`](design_language.md) (visual system,
canonical, CI-enforced). That document governs how a surface looks; this one governs where
a surface lives and when it is walked.

---

## 0. The adversarial verdict — read this first

**The feedback's diagnosis ("IA is the principal weakness") is refuted by production
data.** The bottleneck is *frequency and interruption*, not findability:

- Every learning loop fired **exactly once — 2026-07-02**, the day the previous UX fix
  program was demonstrating itself: all 26 decision gradings, all 46 reconcile items, all
  queued-action dispositions. Nothing since (Sunday 07-05 passed with zero reconcile
  activity). *(verified: `decisions.outcome_at`, reconcile stamps)*
- **The falsifying datum:** the only two organic post-audit decisions (AGX add 07-04,
  BKNG add 07-06) sat with **NULL conviction and NULL falsifier for days while the Home
  open-loops band pointed at them with a lit, one-click doorway**
  (`open_loops.py` → `#decisions_record`). The owner was standing on the porch of the
  exact surface and didn't walk through. Surface placement is not what's blocking the
  ritual. *(verified: decisions 96/97)*
- Organic volume on the surfaces the reorg would re-home: 8 captured musings in 6 weeks;
  1 Tenet ever (rejected); `investor_calibration` = 0 rows; `coach_pings` = 0 rows; Ask =
  5 sessions ever (~2 turns each, all portfolio-scope). *(verified)*
- **The steelman that survives:** on 07-02 the owner binge-cleared a *bounded, finite
  packet* in one sitting. The owner demonstrably executes rituals presented as a packet
  with an end — and ignores standing drips. And Telegram is the only channel with recent
  organic use (captures 07-10), while the standup push channel has self-suppressed **9 of
  its last 12** briefs via its own LLM judge. *(verified: `standup_messages`)*

**Consequently the program inverts:** the *behavioral* interventions (a pushed, finite
Sunday packet over Telegram; point-of-intent nudges) are the flagship; the IA changes are
a small, honest hygiene pass ("a cleaner building") shipped cheaply and without
pretending they change behavior. Several v1 moves are rejected outright (§2.2).

---

## 1. The loop (unchanged from v1)

Orient → Understand → Decide → Remember → Revisit → Learn → Apply. Every primary
destination is one mode; every transition preserves context; every surface keeps the seven
epistemic distinctions visible (stated belief / system inference / evidence /
confidence+freshness / invalidators / what-changed / learned-later).

---

## 2. Information architecture (v2)

### 2.1 Adopted — the shell hygiene pass

| Move | v2 form | Why it survived |
|---|---|---|
| **Review section (new, id `review`)** | Re-parent **three existing panels unsplit**: `musings` (Ledger, **landing**), `triage`, `journal`. **Do NOT carve Weekly/Beliefs out of the Ledger page** — its one-page form gives the Sunday ritual completion semantics (scroll-to-bottom = done; jump-chips already carry pending counts, `ledger_panel.py:1092`), which matches the owner's verified binge-clear pattern. Five cadence tabs would replace "am I done?" with "did I visit all five?". | The burial is real and verified: while a holding is open, the Companies sub-row — the Ledger's only nav handle — is suppressed (`command_center_shell.py:45,1811`). Re-parent fixes that; the split was the friction. Landing = `musings`, statically (conditional "Weekly-when-waiting" landing is not expressible in `_THEMES` tuple order — needs logic that doesn't exist; dropped). |
| **Ask demoted — hide the button, keep the panel** | Keep the `ask` theme + `explore` panel **in `_THEMES`**; hide only its top-nav button via a new hidden-sections set in `_render_section_nav`. (**Not** `_UTILITY_SECTIONS` — `_render_system_button` returns on first match and supports exactly one utility section, `command_center_shell.py:534-550`.) `#ask`/`#explore` keep resolving to the full panel. | Data supports demotion (5 sessions ever); but v1's dock-only form was **wrong** — `goAsk` (every `data-ask-q` doorway), `goView` (palette saved-views), and the dock's own ⇗ pop-out all hard-target `#explore` as a full panel (`command_center_shell.py:1990,2000`; `ask_dock.py:279`), and the 400px dock cannot host the ViewSpec canvas, builder, or saved-view management (`explore_panel.py:340-342,661-663,884-901`). v1's "zero capability loss" claim is retracted; this form actually achieves it. |
| **Portfolio lands on Synthesis** | Reorder the `portfolio_synthesis` tuple to first — **after porting the tracker-offline card into it** (the panel currently degrades silently to equal-weight when the tracker is down: "no offline card here, Performance carries it", `portfolio_panel.py:975-976`). **Keep the label "Synthesis"** — renaming it "Allocation" beside sibling "Positioning" creates a near-synonym collision. | The panel is stronger than v1's critics assumed: 3 of 4 components are live/deterministic (thesis-health rollup, sector exposure, next-dollar distribution); the LLM memo is last, not the page. Known asymmetry, accepted and documented: `#portfolio` (a live panel id, **not** an alias — v1 cited a nonexistent alias) keeps deep-linking to Performance while the nav button lands on Synthesis. |
| **Home relabeled "Today"** | Label only; section id `home`, panel id `overview` unchanged. | Free; matches the briefing direction (§4). |
| **Companies keeps Holding · Discovery · Diet** | After the three ritual panels leave, Companies is coherent: your names / new names / signal-on-your-names. | — |

### 2.2 Rejected (with the evidence that killed them)

| v1 move | Verdict | Why |
|---|---|---|
| **Diet dissolution** | **REJECTED** | Diet and the inbox are CI-guarded *inverse products*: a signals diet row is never converted into an InboxItem / never enters the urgency-decay scorer (`design_language.md:597-631`, `tests/test_signals_diet_guard.py`). Dissolving the non-decaying PULL lane into a decaying push stream violates the invariant or spawns a second un-curated stream inside Today. Only the per-name slice may later be absorbed into a company Updates tab — as content, with the guard intact. |
| **Workspace group renames** | **DROPPED (whole phase)** | Overview→Thesis self-shadows (group "Thesis" ⊃ subtab "Thesis", `workspace_html.py:435,567`) and lies for eval-flavor names that have no owner thesis. Position→Decision creates "Decision"⊃"Decisions" AND a one-letter collision with the exited-name standalone "Decisions" tab v1's table missed (`workspace_html.py:577-578`). Quarter→Updates is blander until per-name Diet content actually lands there. Research→Business survives on merit but a solo user's muscle memory + golden churn (`test_workspace_tab_groups.py` hardcodes labels; `GOLDEN_REGEN` on 2 shell fixtures) buys nothing behavioral. Revisit only if the product is ever shown to others. |
| **Review's 5 cadence modes** | **REJECTED** | See §2.1 Review row — split kills the completion semantics that match observed behavior. |
| **`decisions_record` split now** | **DEFERRED** | The Learning half would compose over empty tables (`investor_calibration`=0, `coach_pings`=0) — shelf space for data that doesn't exist. Interim: a "Record / Learning" **anchor-band** inside the existing panel (Provenance-console precedent). The split stays cheap later: `compose_decisions_page` is genuinely composable (`allocation_decisions_panel.py:1021-1063`), but carries four mechanics v1 missed — parameterized `_EDITOR_JS` refetch (`:1837` hardwires the panel URL), the `calibration_coach` lazy-import cycle (`:538-557`), CSS `REGISTERED` registration, and `open_loops.py:143` doorway retargets. |
| **Today briefing as flagship** | **DEMOTED to phase 3** | Two of its five bands have no substrate: no visit tracking exists anywhere (the only grep hit for `last_visit` is this spec), and "one pattern noticed" would mine 0 calibration rows + ~2 owner decisions — it would repeat the same seed-derived aside within a week. Ship what exists (§4). |

---

## 3. The flagship: interruption, not architecture

The verified failure mode is *unprompted rituals don't happen; bounded pushed packets get
cleared; Telegram gets answered.* Three interventions, all consistent with
DERIVE-DON'T-ASK (system assembles everything; owner supplies only verdicts):

1. **The Sunday packet (Telegram).** A scheduled Sunday digest that walks the reconcile
   queue, proposed Tenets, and research proposals as a *finite sequence of bot messages,
   each answerable with a one-tap verdict* (ratify / rewrite / drop / defer). Assembly is
   deterministic (the same reads `open_loops.py` already counts — no LLM leg required for
   the walk itself; an optional LLM pre-draft of each verdict with a one-line reason turns
   the half-hour into five minutes of confirms). Ends with an explicit "packet clear"
   receipt — bounded, like the 07-02 session the owner actually executed. The web Review
   section is the packet's landing page for anything needing a bigger screen.
2. **Point-of-intent nudge.** When a decision is created with NULL conviction or
   falsifier, send a same-day Telegram follow-up: *"You logged BKNG add — one line:
   conviction? what would prove you wrong?"* This attacks the exact observed failure
   (decisions 96/97) at the moment of intent instead of waiting on a dashboard visit.
3. **Un-gag the standup channel.** The proactive briefing has delivered 3 briefs ever and
   self-suppressed 9 of 12 since — its LLM quality judge is tuned to silence
   (`src/standup/run.py`, `standup_messages`). Re-tune the judge (or add a
   deliver-with-caveat tier) before building any *new* push surface; a gagged channel is
   indistinguishable from a missing one.

Scheduling discipline: both scheduled legs follow the per-item degrade pattern and
register their windows in `directives/llm_quota_scheduling.md` (repo rule for every new
scheduled job with an LLM leg).

---

## 4. Today — ship what has substrate

Recompose the existing server-inlined Home (`render_overview_panel`) in this order, all
additive:

1. **Open loops** (exists — keep first).
2. **Continue where you left off** — CCState already tracks section/tab/ticker; render the
   last workspace/thread as a doorway. *(substrate exists)*
3. **What's coming** — `upcoming_html` already renders in the rail; hoist it. *(exists)*
4. **Since you last looked** — gated on the last-visit primitive. First version uses the
   **client-side localStorage mark pattern the inbox already ships** (`ix-last-seen:*`,
   `dashboard/inbox.py:1041`) — no migration. Honest scope note: the stamp is the cheap
   part; the real work is the aggregation builder (dated queries across signals,
   decisions, falsifiers, prices). A server-side stamp (alembic, with the 0141
   stamped-DB guard) only if multi-device staleness actually bites.
5. **"One pattern noticed"** — deferred until `investor_calibration` has rows.

---

## 5. Instrumentation — first, not last

The v1 program was unevaluable: no panel-view instrumentation exists, so "does moving the
Ledger increase Ledger use?" could never be answered, before or after. Reorder:

- **Ship visit stamps with (or before) the shell PR** — reuse the existing
  `/api/metrics/panel` POST path (fetch/render timings already flow there) to also count
  panel activations per day. That single addition makes every §7 metric computable.
- The DB-computable relationship metrics (grading rate, falsifier coverage on new
  decisions, proposals incorporated, packet completion) are computable **today** and
  already say the loop isn't walked — they become the before/after baseline.
- Metrics list unchanged from v1 (§7 of v1): weekly-review completion, % of meaningful
  trades preceded by pledge/review, captured-thought incorporation, owner-authored
  conviction+falsifier coverage, matured-decision grading, lesson resurfacing, coach
  changed-a-real-decision (the existing honest-zeros Q3 counter). Home: the
  decisions_record Learning anchor (→ eventual Review Learning panel).

---

## 6. The id/redirect contract (v2 corrections)

v1's rules stand (never move a panel id; label ≠ id; every section name aliases to its
landing panel; workspace ids frozen) **with these corrections from the migration
red-team**:

1. ~~`diet` → `home`~~ — invalid: redirect values must be **panel ids** and lookups don't
   chain; the redirect-guard test (`test_command_center_shell.py:352-353`) fails on alias
   values. Moot in v2 (Diet stays), but the rule is: **redirect targets are panel ids
   only** (`diet` would have been → `overview`).
2. v1 cited an "existing alias `portfolio`→`portfolio`" — **no such alias exists**;
   `#portfolio` is a live panel id. The landing swap therefore produces the documented
   asymmetry in §2.1, not an alias change.
3. New aliases for v2: `review` → `musings`. `_LEGACY_PANEL_REDIRECTS` and the mirrored
   `REDIRECTS` map in `SHELL_JS` must move together (comment at
   `command_center_shell.py:202-203`).
4. **Enumerated test blast radius for the shell PR** (in scope, not a surprise):
   `test_command_center_shell.py` — five-section loop (:96), ask-in-topnav (:140-142),
   ask `data-single` (:272), redirect **set-equality** (:305-329), `ask→explore` (:360),
   explore theme attr (:566-575), the two Portfolio order tests pinning perf<risk<synth
   (:578-601), `_SKELETON_KINDS` set-equality (:616-617), `WARM_PANELS` literal (:763);
   plus the single-subtab→`panel_source` map in `test_ui_controls.py` (~:1012-1035).
   Also update the shell module docstring/`_THEMES` header narration and grep fragments
   for stale wayfinding prose (e.g. `ticker_command_center.py:537` "Companies → Journal").

---

## 7. Phasing (v2)

| # | PR | Contents | Risk |
|---|---|---|---|
| 1 | **Instrument + shell hygiene** | Panel-activation counting on `/api/metrics/panel`; Review section (3 panels re-parented, unsplit); Ask button hidden (theme kept); Synthesis offline-card port + Portfolio tuple reorder; "Today" relabel; aliases; the §6.4 test updates; docstring sweep. | Medium (bounded — enumerated) |
| 2 | **The behavioral flagship** | Sunday packet over Telegram (one-tap verdicts); NULL-conviction/falsifier point-of-intent nudge; standup judge re-tune. Register quota windows. | Medium |
| 3 | **Today briefing** | Continue-where-you-left-off + hoist What's-coming + localStorage last-visit band with its aggregation builder. | Medium |
| 4+ | **Evidence-gated reserve** | `decisions_record` split (when `investor_calibration` has rows); Diet-per-name into a company Updates concept; any workspace renames; server-side visit stamp. Built only if phase-1 instrumentation shows the promoted surfaces being walked and cramped. | — |

Gates per repo rules: `python -m pytest tests/test_ui_controls.py -q` on every frontend
change; `GOLDEN_REGEN` + diff review if any renderer is touched (none is, in phase 1).

---

## 8. Open owner decisions (v2 — reduced from five to three)

1. **Confirm the inverted priority**: behavioral flagship (Sunday packet + nudges) over
   IA reorg — the reorg ships as one hygiene PR, not a five-phase program.
2. **Ask button**: hide it (panel reachable via dock ⇗ / palette / doorways / `#explore`)
   — or keep the button and change nothing? (Data says 5 sessions ever; cost of keeping
   it is one nav slot.)
3. **Sunday packet pre-drafting**: should the packet's verdicts arrive LLM-pre-drafted
   (confirm/override, ~5 min) or raw (owner writes each, ~30 min)? Pre-draft matches
   LLM-maximalist preference but adds a weekly scheduled LLM leg to register.

Resolved since v1 (no longer owner questions): Review landing = `musings` static;
"since you last looked" scope = localStorage-first; Portfolio landing = Synthesis with
offline card, label kept; workspace app-in-an-app de-dup = out of program.
