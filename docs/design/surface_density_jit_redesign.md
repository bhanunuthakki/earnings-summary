# Surface Density & JIT-Artifact Redesign — the overarching design choices

Status: PROPOSED 2026-07-24 (owner walkthrough feedback, screenshots 1–8 in
`.tmp/walkthrough/`). Extends `directives/design_language.md` (tokens + kit stay
canonical and untouched) at the **composition** level: how a console page is
shaped, where LLM synthesis sits, and when an artifact is a section vs a chip.

## The diagnosis (owner's words, consolidated)

1. **Vertical space is squandered** — long single-column pages of sequential
   sections, no dividers, low horizontal density ("poor user space").
2. **No LLM synthesis over lists** — the program renders its internal state
   (chronological streams, raw tables, diagnostic strings) instead of a
   contextualized read. "Informative, not actionable."
3. **Subsection sprawl without navigation or utility** — "a random assortment
   of functionality"; the user cannot tell what is possible on a page.
4. **Pre-built embeds where JIT chips belong** — narrative artifacts embedded
   as permanent sections (often empty) instead of chips that mint the artifact
   on demand with well-proven prompts + context + data grounding.

The data bones are right. The front end lacks density, context, intelligence,
and just-in-time delegation.

## The seven choices

### D1 — Page model: briefing → grid → chips (three bands, nothing else)

Every console page has exactly three bands:

- **Band 1 · The read.** A short synthesized narrative brief at the top: what
  matters on this page *now*, grounded in the page's own data, with inline
  doorways (`data-peek-url` / `data-ask-q`) into the evidence. LLM-written
  where semantics matter, deterministic where composition suffices; cached by
  input-hash and refreshed on data change, never per-render.
- **Band 2 · The map.** ONE dense multi-column grid (2–4 cols; this is a
  desktop localhost app) of compact tiles — tables, mini-charts, stat blocks.
  Tiles replace sequential sections. The grid *is* the navigation: seeing it
  is knowing what the page can do.
- **Band 3 · does not exist.** Anything that doesn't earn a tile becomes a
  chip (D2). No section may render only prose explaining where its
  functionality lives (the What-if/Compare signpost) — that content is a chip
  on the tile it points from.

**Vertical budget:** a console's bands 1+2 fit within ~2 viewport heights.
Anything deeper must justify itself or move behind a chip/peek.

### D2 — Artifact model: JIT chips, not pre-built sections

An artifact that is narrative, occasionally needed, or expensive is a **chip**:
click → assemble the context pack (deterministic) → run the well-proven prompt
(governed purpose, D6) → render in the peek drawer. Nothing pre-generates on a
schedule unless a hard deadline exists — and per the owner's ruling, even the
earnings-prep memo is on-demand, not pre-built.

Chip contract (extends the Action-UX bar):
- **Label = the deliverable**, not the mechanism: "Earnings prep memo",
  "What-if: trim NU 2%", "Compare vs candidates".
- **Hover = payoff + grounding**: one line on what it produces and which data
  it reads.
- **Result = receipt**: the minted artifact opens in the peek drawer with its
  provenance (inputs, as-of stamps) and lands in the durable store the same
  way alert-driven artifacts do — a chip's output is never chat-ephemeral.

Infrastructure already exists: peek primitive (`/api/peek/*`), ask engine,
artifact-brief pipeline, `llm_artifact_store`. This choice is about *defaulting
to them* instead of to a new `<section>`.

### D3 — List model: no raw chronological list on any decision surface

Every list renders **grouped → deduped → summarized → dense**:

1. **Group by semantic kind** (filings / rating actions / PT revisions /
   news), not by timestamp.
2. **Dedupe at the render seam by content signature** — the walkthrough found
   the same BN 13D/A on two dates, the same FCX name twice in the trigger
   ladder, and duplicate alert cards. Dedup is a *systemic* render-seam
   obligation: every surface keys its rows by identity signature
   (`signature_key_evidence` discipline), latest-wins.
3. **Summarize per group**: count + delta + a one-line LLM synthesis where
   semantics matter ("4 sell-side PT raises on NOW this week, median +6%").
4. **Render the shape the kind wants**: PT revisions → compact from→to
   matrix; filings → grouped block with kind chips; ratings → dense table.
   The full chronological stream stays one click away as the *reading* view —
   it is no longer the default.

### D4 — Empty & degraded states: one line + the chip that fills it

A section with nothing to say renders ONE narrative line plus the chip that
would fill it. Never an empty shell with explanatory prose; never a raw
diagnostic string. Engineering diagnostics ("tracker offline and no risk
snapshot — book Sharpe unknown · …") translate to owner language with a
receipt: "Tracker offline — 3 of 6 legs unscored · details →" where *details*
peeks the raw string. The degraded state must remain visibly distinct from the
happy path (silent-degradation rule), but in the owner's vocabulary.

### D5 — Density rules: every number formatted, every metric anchored

- Multi-column by default on wide viewports; single-column is the exception
  and needs a reason.
- Every numeric goes through a formatter (`_pct` / `_money` / `_num` class of
  helpers). A raw float repr reaching HTML is a defect (guard added in
  `tests/test_allocation_recommendation_panel.py::_no_raw_floats`).
- Every metric renders with a comparator — delta vs prior, distance to bar,
  percentile — because an unanchored number is trivia, not information.
- A column that is constant across all rows ("evaluation", "UNREVIEWED") is
  dead weight: lift it to a group header or a filter chip, don't repeat it.

### D6 — LLM governance for the new synthesis legs

Every Band-1 brief and D3 group-summary is a governed purpose: `LLM_MODELS`
key + `prompt_versions` + budget row (+ eval registries in lockstep, per the
operational-purpose recipe). Cached by input-hash so re-render is free;
degrades per the call-exception policy (transient → deterministic fallback
line that *labels itself* as the fallback; budget/setup → loud). Prompts carry
the context-anchor architecture — these are the "well-proven prompts with
context and data grounding" the owner asked for, not ad-hoc strings.

### D7 — Navigation & utility: headers state the question they answer

- Every tile header = plain-language utility subtitle: "Should I trim
  anything? — DCF gap vs your margin-of-safety bar", not "Trigger ladder".
- Every console gets the grouped-tab top strip (workspace-renderer pattern)
  so the page's capabilities are enumerable at a glance.
- The chips available on a page ARE its capability map — visible, labeled by
  deliverable, never hidden in prose.

## Application map (walkthrough item → treatment)

| # | Surface | Treatment |
|---|---------|-----------|
| 2 | Earnings calendar | Tier the list: **portfolio** > **active valuation** (`research_hot_flags` ∪ names with recent owner decisions/notes — derived, no new flag to maintain) > rest of eval list. Chips (observations, open questions, prep) sit inline in the horizontal space right of the ticker (D5). "Earnings prep memo" = JIT chip (D2), not a scheduled artifact. |
| 4 | What-if / Compare section | Delete the section. "What-if" and "Compare" become chips on the allocation tile (D1 Band-3 rule). |
| 5 | Positioning | One narrative line + "Talk to the coach" / "Encode targets" chips (D4). The degraded-tracker diagnostic renders as an owner-language pill + details peek, never verbatim monospace. |
| 6 | Skill decomposition | Fix the math first (dust-position blow-up in `decompose_alpha` — plausibility gate, see Wave 0). Then render as ONE compact tile: the honest verdict sentence ("indistinguishable from luck at n=21") leads, the split table follows, and the conviction join renders only when conviction data exists (D4). |
| 7 | Portfolio console page shape | The reference implementation of D1: brief band + one dense grid of tiles; constant columns lifted out (D5); utility subtitles (D7). |
| 8 | Ingest stream | D3 in full: group by kind, signature-dedupe (BN repeat), PT-revision matrix + grouped filings block + LLM group summaries; chronological stream demoted to the "reading view" link. |
| 3 | Risk / positioning screen | Organizing frame (owner's words): the **ranked implicit-bets statement** — "what am I positioned for, per cycle" — prose-led (Band 1), each bet backed by a compact chart drawn from `factor_vector_json` / `business_factor_exposures`, with deviation-vs-tenets called out. Spider/radar is a supporting tile, not the frame. |

## Rollout order

- **Wave 0 (bug class, immediate):** `decompose_alpha` plausibility gate;
  render-seam signature dedup on trigger ladder + ingest stream. Pure
  correctness, no design dependency.
- **Wave 1 (reference implementation):** the Portfolio console (screens 3–7)
  rebuilt to D1–D7 — it exhibits every pattern once. Owner reviews the result
  before the pattern propagates.
- **Wave 2:** earnings calendar tiering + inline chips + earnings-memo JIT
  chip (#2/#4).
- **Wave 3:** ingest-stream regrouping (#8); remaining consoles brought under
  the page model; risk implicit-bets frame (#3).

One PR per wave, per the phase-PR cadence.
