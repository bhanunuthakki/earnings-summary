# Directive: The Instrument Paradigm — unifying interaction model + structural fixes (2026-06-12)

Canonical plan from the 2026-06-12 usability review (42-agent workflow: 12 feedback
clusters × diagnose→design→adversarial-verify, + web research, + 4 cross-cutting
synthesizers + a coherence critic). This directive supersedes ad-hoc UX fixes: the
feedback the owner gave is not a bug list, it is one missing thing — a coherent
interaction spine. Read this before touching any surface.

Model rule (per `GEMINI.md` → Session & Agent Model Selection) is applied per session below.

---

## 1. The Instrument Paradigm (the spine)

This product is **one instrument, not a collection of pages.** Every thing it surfaces —
an inbox item, a stat, a candidate, a provenance issue, a signal — has a stable identity
stored once at write time, and every surface is a *lens* over that identity, assembled
from one component kit and governed by one dismissal contract. Three laws, then their
corollaries:

**Law 1 — Identity over source.** A surfaced thing's category, label, body, rank, and
links derive from its stored *semantic identity* (a discriminator written at ingest:
`semantic_kind` / `signal_type` / anchor-type / `fact_ref`), resolved through ONE shared
resolver — never sniffed from the source table it was UNION-ed out of, and never from a
headline regex at render time. Machine-authored reading (advisor/synthesis memos) ranks at
synthesis weight *by identity*, regardless of which table it echoed through. No
internal-format string (`[advisor memo #N · kind]`, raw enum `observation`) ever reaches a
user-facing label or body.

**Law 2 — Every datum is a doorway.** Any number, count, KPI, cell, or stream item with a
deeper view is rendered as an `<a>`/`<button>` carrying exactly ONE shell-handled action
attribute — `data-peek-url` (peek its backing collection in place) or
`data-ask-q`/`data-fact-ref` (open it as a chart/series in Ask). A `title=` tooltip is a
*supplement*, never the only depth. An inert `<span>` whose payload is buried in a tooltip
is forbidden. A clickable cell emits a stable `fact_ref` as its anchor and Ask payload — a
human label is a *display*, never a *handle*.

**Law 3 — Every surface is a dismissible layer; every section is one labeled instrument.**
Every transient surface (drawer, peek, popover, dock, sidebar, citation) is an instance of
ONE Overlay primitive registered in a single open-surface stack, so close-(×) + Escape +
scrim click-out + focus-trap/restore are guaranteed by the abstraction. Every in-shell
panel is an instrument under an already-labeled tab: the nav owns the title (a panel never
re-prints its own section name), and its title + filters + actions collapse onto at most
ONE operating band before content.

**Corollaries (the operating principles):**
- **One item model, many lenses.** Inbox, cockpit, diet, discovery, provenance, Ask are
  lenses projecting the same identity, not independent record types. Lenses differ only by
  filter/ordering/decoration. Reads are SELECT-ORDER-LIMIT over an index, never render-time
  re-aggregation, full-table scans, or network calls on the render path.
- **One vocabulary, opt-out enforced.** Every CSS-emitting surface composes
  `palette_css + controls_css + layout-only CSS`. Conformity is a property of *rendered
  output*, enforced by opt-out denial of primitive literals over ALL auto-discovered
  surfaces — not an opt-in allowlist of swept files.
- **One render boundary per content-kind.** Exactly one canonical renderer per shape:
  `ui.prose.render_prose()` for any stored body/narrative/memo/note; `panel_toolbar()` for a
  panel header; `prov_row()`/`prov_drawer()` for any data-quality row; `ticker_label()` for
  ticker+name. New surfaces import the boundary; they never reinvent it.
- **Closed under no-fit, explainable by construction.** Any classifier is closed under "no
  fit" (always an explicit `needs_triage` terminal; never silently flatten an unmappable or
  conditional directive into an inert note). Any ranked surface scores by a weighted sum of
  typed, dated signals through a source-weight registry (never an equal-weight count),
  carries its `score_why`, and derives its severity/color map from the live enum (guarded by
  a contract test) so writer↔reader drift can't mute a halt.

---

## 2. Rules to codify (so the class of miss can't regress)

These land in `directives/design_language.md` and `GEMINI.md` as part of the sessions that
implement them — each enforced by a guard test, not left as prose.

1. **Identity over source** (design_language §Streams + `test_inbox_rank`): stream
   item category/label/body/actions come from a shared `inbox_label()`/categorizer reading
   `source/source_ref/context`; no source-table string or raw enum surfaces as label/body;
   machine-authored reading ranks at synthesis weight.
2. **Interactive numbers are doorways** (design_language §4 + `no-inert-stat` guard): a
   number/count/KPI with depth is an `<a>`/`<button>` with exactly one `data-peek-url` /
   `data-ask-q` / `data-fact-ref`; never an inert `<span>` whose only depth is a tooltip; a
   section's primary picker renders matches as in-section furniture, not a `.k-menu` overlay.
3. **Universal surface dismissal** (design_language §3 Chrome + `test_overlay_dismissal`):
   every transient surface is `CCOverlay.register`-ed and dismissible by the triad (× +
   Escape + scrim click-out) with focus-trap/restore; non-modal phrasing popovers
   (cite-marks) get Escape-only; gesture-spawned sidebars declare `scrim:false`; Escape
   resolves by stored modality+priority, not recency.
4. **One operating band per panel** (design_language §6.1 + band-discipline guard): a panel
   under a labeled tab gets at most one chrome band — `panel_toolbar()` (title omitted when
   identical to the active sub-tab; filters+actions on the same flex row); never stack a
   title band over a filter band.
5. **Opt-out token conformance** (design_language §2/§7 Enforcement + `test_ui_controls`):
   raw hex (incl. `var(--x,#hex)` fallbacks and gradient-internal hex), off-scale
   font-size/border-radius px, font-family literals, and legacy alias names are forbidden in
   ALL CSS-emitting modules and inline `style=`, enforced by an opt-out guard that
   auto-discovers surfaces via filesystem diff; an unregistered surface or undocumented
   escape fails CI. **Font-family is denied to exactly `{--mono, --sans}` (+ generic
   keyword fallbacks)** — the "too many fonts" complaint gets first-class enforcement, not
   just size/color.
6. **Prose render boundary** (design_language §Rendered-prose + `test_ui_prose_boundary`):
   any stored analyst/LLM body/narrative/memo/note rendered to HTML passes through
   `ui.prose.render_prose()` (inline variant for table cells); bare `escape()` of a prose
   field and any locally-defined markdown renderer are forbidden — EXCEPT deterministic
   non-markdown fields (attribution narrative, judge rationale) which stay `escape()`d.
7. **Closed-under-no-fit classifier + steerable computed sections** (`report_comments_and_chat.md`
   + GEMINI.md): the comment classifier always includes `needs_triage`; every commentable
   surface (incl. computed panels like peers/charts) carries a structured anchor type and a
   persisted override artifact the routers can mutate.
8. **Provenance is actionable + drift-proof** (design_language + GEMINI.md +
   `test_provenance_severity_contract`): every data-quality surface renders through
   `prov_row`/`prov_drawer` (severity tick + relative stamp + drill-down + ≥1 inline action:
   resolve/refresh/diagnose/`/source`); any severity color map derives from the live
   `models.validation.Severity` enum, contract-tested.
9. **Identity at write-time / typed signal at ingest** (GEMINI.md): a user-facing stream is
   one indexed query over a shared identity spine (rank+dedupe materialized, not Python); a
   forward-dated item is a queryable row with `event_date`, not LLM prose; a non-decaying
   DIET signal never enters the urgency-decay scorer or the materiality veto built to
   suppress non-thesis news.

**Coordination note (codify in GEMINI.md):** `directives/design_language.md`, `src/ui/controls.py`,
the `test_ui_controls.py` guard family, the alembic head, and `inbox_rank.py`/`inbox.py` are
shared seams touched by many sessions. Edits land as coherent *sequenced* PRs with a
heading→session ownership map (§6 below), never concurrently — the documented "3-file
control-wiring conflict among siblings" is the default failure mode otherwise.

---

## 3. Your feedback → root cause → structural fix

| # | Your feedback | Root cause (not the symptom) | Structural fix |
|---|---|---|---|
| 1 | Advisor memo tops the inbox; `[advisor memo #1 · next_dollar]` label is poor/non-standard; want dismiss/journal/thesis/open-company chips | Inbox ranks/labels by **source table**, not semantic identity. A `next_dollar` memo reaches the inbox only as an `analyst_notes` `observation` row; the synthesis-demotion fix only fires for the `ledger` kind, so the note scores at WATCH severity and floats to top, with the raw internal string in its body and no action affordances. | Law 1. One identity-aware `inbox_label()`/categorizer reads `source/source_ref/context`; advisor memos demote to synthesis weight by identity; clean body; `.k-btn/.k-chip` dismiss + open-memo chips via the existing `/api/notes` archive endpoint. Zero schema. (S3) |
| 2 | A comment on *peer selection* was filed as a memo; categorization too rigid | The comment classifier is **forced-choice with an inert hard fallback** (`return "ask_question"`), the peers panel carries **no commentable anchor type**, and the notes mirror **collapses** unmappable directives to `observation`. | Principle "closed under no-fit": add `needs_triage` terminal + `curate_peers` intent + a `peer_comp` anchor on the peers panel + a persisted peer-override artifact the router mutates. (S5) |
| 3 | Too many fonts/colors/weird sizing; design language must enforce font/color/gradient/shape conformity everywhere | Enforcement is **opt-in**: the guard imports ~22 of ~41 CSS-emitting modules and regexes only raw hex. The shell itself ships a legacy-alias `:root` (12 names, 60 rules), raw-hex gradients, off-scale glyphs — and passes green. | Law "one vocabulary, opt-out enforced": invert the guard to opt-out denial over filesystem-auto-discovered surfaces (hex/gradient-hex/off-scale-px/**font-family**/legacy-alias, dimension-scoped, EXCEPTIONS seeded from §1); unfork the shell namespace; add `.k-well/.k-pill` so the 4 reinvented pill systems are deleted. (S1) |
| 4 | Search box should be a bar embedded in the Holding section | The holding picker is a transient floating `.k-menu` combobox detached from its section (Law-3 violation: a section's primary picker is furniture, not an overlay). | Anchor/embed the results to the Holding section as persistent furniture. (S9) |
| — | Landing stats should click through (AMZN RPO → RPO chart in Ask; "3 new docs" → docs list) | Law 2 violated: stats are inert `<span>`s; the two click-through rails (`data-ask-q`, `data-peek-url`) are not shell-wide; the docs count `SELECT`s then discards the rows; no `/api/peek/documents` route. | Shell-global `data-ask-q` delegate (reuse goAsk); re-type stats as doorways (KPI chip → `data-ask-q` *relative-window* phrasing; count → `data-peek-url`); add `/api/peek/documents`. (S9) |
| 5 | Discovery filter takes too much vertical / too little horizontal; list too crowded; reassess ranking; add 13F + investor-research sources with maintained weighting | Two bands stacked (title block over filter block) + tall multi-line rows + print-all-500; ranking is an **equal-weight integer count** over a single-Float+blob schema that structurally cannot express weighting or hold an external-source rating. | Law 3 + "weighted typed signals": `panel_toolbar()` one band, `.p-table/.p-pill`, top-N cap, evidence behind peek; `discovery_signals` typed child table + `discovery_sources` weighted roster + `score_json`; EDGAR-13F + investor-research source class with a clamped top-of-funnel weight. (S6) |
| — | Inbox dismiss/action chips (journal, thesis, open company) | (folded into #1) | S3 ships dismiss + open-memo; "record in journal / update thesis" route to the company/Memos surface (portfolio-level memos have no company target — disclosed, not silently dropped). |
| — | Provenance section illegible; consolidate with drilldowns/collapses; make it actionable (click issues, refresh, diagnose, read evals) | No shared data-quality primitive (8 bespoke builders), a **dead severity map** (writer emits `halt`/`warn`; reader expects `error`/`warning`, so `halt` renders muted grey), and build-time-static tables with no action affordances. | Principle "provenance is actionable": shared `prov_row`/`prov_drawer`, fix the severity-enum drift in BOTH sort and renderer (contract-tested), per-row resolve, one `provenance_panel` assembler, `/source` deep-links. Keep the working Coverage matrix prominent. (S10) |
| — | Navigation everywhere (close/back/click-out/close-popup) as a rule; slick + delightful | No shared Surface abstraction — five hand-rolled `close*` fns, an enumerated Escape switch, a cross-document `window.__close*` handshake. Each surface re-derives dismissal; many lack it. | Law 3: one `CCOverlay` primitive + open-surface stack with explicit modality+priority; migrate all surfaces; codify §3 Chrome behavior. Back-button deferred (collides with hashchange); **motion/"delightful" polish is an explicit S4 deliverable** (transition tokens on open/close), not just affordance presence. (S4) |
| — | Markdown formatting leaking into rendered prose | **Three divergent markdown renderers** + ~7 surfaces injecting bodies via bare `escape()`. | Boundary "one render per content-kind": `ui/prose.py::render_prose()` (lift the proven `_shared` renderer), inline variant for cells, guard test. Wire genuinely-markdown surfaces incl. the **7th site `ticker_command_center.py:1145`** the critic caught; exclude deterministic attribution/judge fields. (S2) |
| — | Streamline discovery (too crowded) | (paired with #5) | **Owner decided (2026-06-12): BOTH** — raise the *entry* threshold so weaker names never enter the queue AND cap the *render* to a ranked top-N with chip filters + collapsed evidence. (S6) |
| — | Ask: redundant horizontal "Ask" bar; mismatched fonts; stale "e.g…" placeholder after a question | Title ownership ambiguous (panel re-prints its own `<h2>Ask</h2>` under an already-labeled tab); `.ask-inputrow input` carries a per-element `--fs-section` override vs `.k-btn` `--fs-body`; placeholder never reset after submit. | Law 3 (nav owns the title — drop the `<h2>`, shell injects single-sub-tab titles); delete the per-element font override + adopt `.k-btn`; reset placeholder to "Ask a follow-up…" after submit. (S8) |
| — | Alerts should be an information-diet curation layer (sell+buy-side ratings, model changes, investor days + takeaways, exec/investor podcasts) | One table + one decaying scorer + a materiality veto force a **thesis-breach alerter and an information-diet curator** (inverse products) through one pipe; informative-but-not-breaching signal is vetoed away; there's nowhere to store `signal_type`/`event_date`/source-entity. | Principle "typed signal at ingest": a `signals` typed table feeding two lanes — decaying push ALERT (thesis-breach) and non-decaying pull DIET; investor days are `event_date` rows; **takeaway summarization (investor-day/podcast → note) is an explicit owner**, not just a calendar entry. (alerts substrate + S11) |

---

## 4. Session plan — 12 sessions, 4 waves

Foundation-first: land the contracts/primitives downstream work binds to, before the
sweeps that consume them and before the substrates that add schema. **Models per the rule:
Opus only where genuine interaction-design judgment lives; Sonnet for spec'd implementation
against a locked contract; Haiku for mechanical long-tail.**

### Wave 1 — Foundation (contracts + primitives everything binds to)
| # | Session | Model | Deps | One-line scope |
|---|---|---|---|---|
| S1 | Design-token conformance: opt-out guard + namespace unfork + control-kit primitives | **Opus** (then Sonnet for the alias rewrites) | — | Invert guard to opt-out over auto-discovered surfaces (incl. **font-family → {--mono,--sans}**); unfork the shell `:root`; add `.k-well/.k-pill` + `panel_toolbar()/.k-toolbar` + `panel_section_title()` to `controls.py` in ONE edit (merged home for design + layout-density + ask-title primitives); guard lands RED. |
| S2 | One prose boundary + guard | Sonnet | — | `ui/prose.py::render_prose()` (+ inline variant); collapse the 3 renderers; wire genuinely-markdown surfaces incl. `ticker_command_center.py:1145`; exclude attribution/judge fields; `test_ui_prose_boundary`. |
| S3 | Inbox semantic-identity: rank/label/act by identity | Sonnet | — | `InboxItem.semantic_kind` from note source/ref/context; one `inbox_label()`; demote advisor memos by identity; clean body; `.k-btn/.k-chip` dismiss + open-memo. Zero schema. **Design `_categorize` to anticipate the diet-scoping (alerts) and spine-collapse (S12) so they extend, not re-cut it.** |
| S4 | Dismissible Overlay primitive + open-surface stack | **Opus** | S1 | `CCOverlay` (one Escape + one click-out, modality+priority stack, focus trap/restore) on `.k-overlay/.k-scrim`; migrate 5 shell overlays + dock + iframe chat/comments; cite-marks Escape-only; **motion/transition polish for "delightful"**; cut Back-button v1; §3 Chrome contract + guard. ~3 PRs. |

### Wave 2 — Substrates (schema + judgment, rendered through the Wave-1 kit)
| # | Session | Model | Deps | One-line scope |
|---|---|---|---|---|
| S5 | Open the comment taxonomy: peer-curation + `needs_triage` + steerable peers | **Opus** | S1 | `needs_triage` terminal (replace the `ask_question` hard fallback); `peer_comp` anchor + `curate_peers` intent appending to `competitive_watchlist` + a new `peer_exclude`/`peers_hidden`; route triage to the existing data-fixes backlog. **Model the conditional ("remove UNLESS better peers") as intent, not just logged text.** |
| S6 | Discovery: weighted multi-signal ranking + EDGAR-13F/investor source class + panel rebuild | **Opus** (contract: Signal type + `scoring.py` weight/decay + 3 migrations + roster) then Sonnet (miner + panel) | S1 | `discovery_signals` + `discovery_candidates.score_json` + `discovery_sources` roster **seeded from the §5 two-tier roster (crossover + multi-cycle growth tiers)**; weighted+dated scoring replacing the count, encoding the **action-type asymmetry** (new-position weight ≫ add weight for low-turnover long-only funds; hedge-fund moves higher-frequency); **clamp** so an investor-only name can't top the queue on investor weight alone; **BOTH raise the entry threshold AND cap the render top-N** (owner decision); EDGAR 13F-HR direct (free path; FMP form13F gated); panel on `panel_toolbar` + `.p-table/.p-pill` + chip filters + collapsed evidence; weight-edit surface; recalibrate weights quarterly vs realized forward returns. |
| S10 | Consolidated provenance console: shared `prov_row`/`prov_drawer` | **Opus** | S1 (seq. after S4 for the iframe Sources tab) | PR1: primitive + severity-enum fix in sort AND renderer + contract test + `resolved_by/resolution_note` + resolve writer. PR2: `provenance_panel` assembler, collapse the 8-tab strip, `/source`-link the report Sources tab, regenerate `pane_sources` golden. Keep Coverage prominent. |
| S7 | Universal design-token conformance sweep (drive S1 guard green) | Haiku/Sonnet | S1 | Mechanical rewrite of `evals_panel`/`analytical_dashboard_html`/`research_cockpit`/`portfolio_panel`/`chrome.py` onto `.k-well/.k-pill` + canonical tokens; rewrite design_language Appendix A into the Enforcement section. No design judgment (S1 made the calls). |
| — | Information-diet curation substrate | **Opus** (substrate) | S1, S3 | `signals` typed table (signal_type/event_date/weight/cadence) feeding ALERT + DIET lanes; backfill `news` as `general_news`; route `yf_grades` into the typed `consensus_rating` lane (sell-side, free); investor-day feed reconciled with `expected_earnings`; guard that diet rows never enter the decaying scorer. **Buy-side ratings + estimate revisions are fast-follows (no free data path) — disclosed, not promised.** |

### Wave 3 — Dependent UI + click-through (ride the Wave-1 primitives)
| # | Session | Model | Deps | One-line scope |
|---|---|---|---|---|
| S8 | Ask panel title-ownership + control-row sizing | Sonnet | S1 | Drop `<h2>Ask</h2>` (shell injects single-sub-tab titles); delete the input `--fs-section` override + adopt `.k-btn`; reset placeholder after submit. |
| S9 | Cockpit doorways + embedded holding search bar | Sonnet | S1, S4 | Shell-global `data-ask-q` delegate (scope-excludes `#explore`); re-type cockpit stats as doorways (KPI → relative-window `data-ask-q`; docs count → `data-peek-url`); add `/api/peek/documents`; anchor the holding combobox to the Holding section. **Owns capturing the render-latency baseline that gates S12.** |
| S11 | Comment-taxonomy + curation follow-ons | Sonnet | S5, diet substrate | Deferred surfaces: triage panel/route, journal-silo redesign, and the diet layer's second leg — `model_revision` + `media_appearance` feeds + **investor-day/podcast takeaway summarization** (LLM-web, Opus-summarize) — once their data paths are real. |

### Wave 4 — Deferred DB unification (gated on profiling)
| # | Session | Model | Deps | One-line scope |
|---|---|---|---|---|
| S12 | Signals spine + `fact_ref` identity | split: Sonnet (`fact_ref` half, ship FIRST) then **Opus** (spine) | S1, S3, S6, **profiling** | FIRST: thread `kpi_definition_id` through `KpiLedgerRow`, emit `data-fact-ref` alongside `data-anchor-key` (degrade to name), grounding PK fast-path, one `fact_ref` column on `analyst_notes`. SEPARATELY: fix measured inbox offenders (batch the N+1; move `live_position_weights`/`_thesis_tones`/`_news_meta` off the render path) and MEASURE. Build the full 8-writer `signals` spine (prefer SQLite triggers; parity-gated cutover; scoped to the 5 inbox kinds) ONLY if a profiled render still misses target. |

---

## 5. Discovery & curation data sources (research)

**13F access (verified):** the FMP `form13F` MCP endpoint is **Ultimate/Enterprise-gated**
and this repo runs `FMP_TIER=free` — it will 402 like `/stable/earnings` did. The free,
pattern-matched path is **SEC EDGAR 13F-HR direct**: poll `data.sec.gov/submissions/CIK….json`
per tracked manager (reuses the existing 8-K/13D ingestion rung), fetch the INFORMATION
TABLE XML, diff vs the prior quarter, map CUSIP→ticker (seed from local FMP `profile.json`
+ SEC `company_tickers` + SEC's quarterly 13F CUSIP list). WhaleWisdom/sec-api.io are
buy-not-build fallbacks. **Caveats (why moves are top-of-funnel, never a trigger):** 45-day
lag, longs-only (shorts/net view invisible), non-US/sub-$100M managers don't file. **ARK is
the one zero-lag source** (publishes holdings daily via CSV).

**"Tuscaloosa" resolved (owner, 2026-06-12):** = **Whale Rock** + the owner also meant
**Appaloosa (David Tepper)**. Both are in the roster below. The owner also directed:
**expand the roster toward multi-decade, multiple-market-cycle-tested growth investors**
("invested in growth through decades") — done below. ("Sea Lane" still unidentified — drop
unless the owner supplies an entity/CIK.)

**Investor roster — current-cycle crossover tier (rated top-of-funnel weights, 0–1):**
Altimeter 1.0 · Atreides 1.0 · Whale Rock 0.9 · Light Street 0.85 · Coatue 0.8 · Lone Pine
0.8 · D1 0.75 · Baillie Gifford 0.65 (partial 13F) · Durable 0.6 · Viking 0.6 · Tiger Global
0.6 · Maverick 0.6 · Tremblant 0.55 · Dragoneer 0.55 · Alkeon 0.55 · Greenoaks 0.5 ·
Addition 0.45 · ARK 0.4 (daily data, low conviction-quality) · ICONIQ 0.4 · Tybourne 0.3
(wind-down). Weight by concentration × conviction/persistence × track-record × overlap.

**Investor roster — multi-decade, multi-cycle growth tier (added 2026-06-12; selection lens
= survived dot-com 2000–02 + GFC 2008–09 + 2022 while staying in growth/quality-compounders):**

| Firm / principal | Cycles survived | Style | 13F | Weight | Why signal |
|---|---|---|---|---|---|
| Appaloosa / Tepper | 1993–; dot-com, GFC, 2022; ~25% ann. since inception | Hedge, concentrated (~31 names), big AI/hyperscaler tilt (AMZN/MU/GOOG/TSM) | Yes | **0.85** | Best living risk-taker; AI-infra book overlaps the universe directly |
| Sands Capital ⬆ | 1992–; beat R1000G nearly every yr | Long-only Select Growth 25–35, high conviction | Yes | **0.80** (↑ from 0.7) | Multi-cycle lens *strengthens* it — a 30-yr concentrated growth shop, not a 2020 crossover |
| Fidelity Contrafund / Danoff | mgr since 1990; 10,423% cum. | Long-only; concentrated at top (Meta+Nvidia ~22%) | Yes (FMR) | **0.70** | Greatest single-mgr growth record alive. **FLAG: Danoff retires end-2026** — re-weight at handoff |
| WCM Investment Mgmt | 1976–; moat-trajectory + culture | Long-only concentrated quality-growth, low turnover | Yes | **0.70** | Distinctive moat-*trajectory* discipline; low turnover = high signal-per-add |
| Loomis Sayles Growth / Hamzaogullari | team since 2010 (30+ yr career); 17.4% 10-yr | Long-only 30–40 names, ~7% turnover (lowest here) | Yes | **0.70** | A NEW position from a 7%-turnover shop is a loud signal |
| Polen Capital (Focus Growth) | 1989–; 15% CAGR '89–'20 | Long-only ~25 high-ROIC names, low turnover | Yes | **0.65** | Textbook concentrated quality-growth, 35-yr record |
| Akre Capital (Neff, CIO) | 1989–; three-legged-stool compounders | Long-only ~20 names, very low turnover | Yes | **0.65** | Tiny conviction-weighted book = every move matters; succession executed |
| Edgewood Management | 1974–; composite to 1992 | Long-only **fixed 22-stock** large-cap growth | Yes | **0.65** | Sell-one-to-add-one cap → highest structural signal-per-move on the list |
| Jennison Associates | 1969–; 11.8% since inception | Long-only large-cap growth sleeves | Yes (PGIM) | **0.60** | Pioneer growth DNA. **FLAG: founder Segalas died 2023**; team-based now, big book dilutes |
| MS Counterpoint Global / Lynch | Lynch since 1998; huge '20, brutal '22, rebounded | Long-only disruptive/higher-beta | Yes (Morgan Stanley) | **0.55** | Strong disruptor idea-gen; treat as where-are-they-fishing, not conviction-anchor |
| SGA (Sustainable Growth Advisers) | 2003–; pricing-power quality growth | Long-only concentrated global quality-growth | Yes (Virtus) | **0.50** | Clean screen + concentration; shorter record → second-tier |
| Brown Capital (Small Co) / E. Brown | 1983–; GARP coiner | Long-only **small-cap** exceptional-growth, low turnover | Yes | **0.45** | Great record but small-cap → new-idea sourcing more than universe overlap |
| Capital Group / Growth Fund of America | 1973–; every cycle | Long-only multi-manager, very large | Yes (Capital World/Research) | **0.45** | Deepest pedigree but multi-manager + huge book → *confirmation*, not a trigger |

**Ruled out:** T. Rowe Blue Chip Growth (Puglia gone → no multi-cycle PM premium); Glenn
Greenberg/Brave Warrior, SQ Advisors/Simpson (dormant), Sequoia/Ruane Cunniff, Gardner Russo
(all **value/quality, not growth** — anchor a *separate* quality/value roster later if wanted);
Wellington (13F is the whole 1,900-position firm — can't isolate a growth sleeve).
**Correction to research notes:** "SQ Advisors / Chieftain (Greenberg)" was two different
people — SQ = Simpson (dormant), Chieftain/Brave Warrior = Greenberg (value); both excluded here.

**Signal nuance (long-only veterans vs hedge funds):** the multi-cycle long-only houses run
far larger, lower-turnover books — **weight their NEW initiations heavily and their
incremental adds lightly**, and treat the giant multi-manager houses (Capital Group,
Wellington) as *confirmation* not *triggers*. The crossover hedge funds are higher-frequency
(timing/momentum) signals; the long-only veterans are lower-frequency, higher-durability
(durable-compounder surfacing) signals. The `scoring.py` weighting must encode this
action-type asymmetry (new-position weight ≫ add weight for low-turnover funds).

**Rating model:** an `investor_registry` table (cik, name, base_weight, style_tags,
last_calibrated_at). Fold into discovery as a clamped weighted factor —
`investor_term = Σ over funds holding the name [base_weight × move_magnitude × recency_decay
× action_sign]` — that can lift/re-rank a name but cannot dominate the fundamental screens.
Corroboration (2+ funds initiating same name same quarter) gets a super-linear bump. Each
move lands in the candidate's self-explaining evidence ("Whale Rock NEW 3.1% Q1'26; Light
Street +1.4%"). Recalibrate base_weights quarterly against realized forward returns of each
fund's new buys (fold into the model-eval/attribution loop).

**Curation feeds for the information-diet layer:** sell-side ratings/PT (FMP analyst MCP +
the `yf_grades` rung you already run — *free*); estimate/model revisions (FMP analyst —
*paid-gated, fast-follow*); buy-side "ratings" = the 13F layer + ARK daily (*free*);
investor-day calendar (extend the weekly Playwright IR-events scrape; Wall Street Horizon
optional paid); post-event takeaways (FMP transcripts + IR deck + Opus-web summarize);
podcasts (curated RSS allowlist — BG2, Invest Like the Best, Acquired, Logan Bartlett, In
Good Company, Odd Lots, Stratechery/Sharp Tech — filtered by the existing ticker/entity
matcher, Opus-summarize on a hit). All ride the `news`/`signals` tables via new
`source_feed` tags + the `source_calls` ledger — no new framework.

---

## 6. Coordination grid (lock BEFORE Wave 1 starts)

- **`design_language.md` heading → session owner** (serialize doc edits): S1 owns
  §2/§6.1/§7 + Enforcement; S2 owns Rendered-prose; S3 owns Streams/Identity; S4 owns §3
  Chrome behavior; S5 owns Comments-closed-under-no-fit; S9 owns §4 Doorways; S6 owns the
  Discovery rule; the diet substrate owns Diet-vs-alert.
- **`controls.py` is the hottest seam** — S1 lands first and owns the file + docstring
  enumeration; S2 (`.prose`), S4 (`.k-overlay/.k-scrim`), S10 (`prov_row/prov_drawer`), S12
  (`fact_anchor_attrs`) APPEND and rebase on S1. Never two `controls.py` sessions concurrent.
- **Signal-taxonomy arbiter:** three clusters reach for a `signals`/typed-signal substrate
  (discovery_signals, diet signals, the inbox spine) with overlapping names. ONE owner
  decides the taxonomy (is a 13F move a discovery signal, a diet signal, or a spine signal?)
  and reconciles names BEFORE any migration is numbered. The S12 spine is the single owner
  of cross-surface consolidation, or none attempts it.
- **Alembic head is `0093_analyst_notes_links`** today (the per-cluster verdicts say "~0092"
  — stale). Every schema session picks its number + `down_revision` at REBASE time on the
  live head; collisions are silent.
- **`inbox_rank.py`/`inbox.py`** are rewritten by S3, the diet substrate, and S12 — S3 lands
  first and must anticipate both later edits.
- **Report iframe** (`workspace_chat/comments/sources/_shared`) serialized S4 → S2-report-
  wiring → S10-PR2; the last owner regenerates `pane_sources.html` golden and re-checks S7
  didn't re-invalidate it.
- **Latency baseline** for the S12 gate must actually be captured during Waves 1–3 —
  assigned to S9 as a deliverable, or the "profile first" gate is unenforceable.

---

## 7. Residual gaps / explicit descopes (nothing silently dropped)

- **"Record in journal" / "update thesis" inbox chips** → routed to the company/Memos
  surface, not net-new write paths (deliberate descope). Portfolio-level memos (ticker=None)
  have **no company to click into** — structural, disclosed.
- **Back-button dismissal** for overlays cut from v1 (collides with hashchange); for *panels*
  it already exists via hashchange. Whether overlays should ever join history is unresolved —
  tracked, not built.
- **"Delightful"** = motion/transition polish on open/close, an explicit S4 deliverable
  (affordance presence ≠ felt quality).
- **Buy-side ratings + estimate revisions** ship as fast-follows (no free data path) — the
  diet substrate must not promise data it can't fetch.
- **"Reassess ranking"** is read as "weighted typed signals." If the crowding is partly
  *wrong names* (screen/adjacency miscalibration), weighting won't fix it — flagged for S6.
- **Discovery "too many companies"** — RESOLVED (owner, 2026-06-12): do BOTH — raise the
  entry threshold (fewer candidates generated) AND cap the render top-N. S6 owns both.
- **Ask font mismatch** beyond the input row (`.ask-turn-*`/`.vx-row` sublabels): S8 fixes the
  input row; the other tiers are asserted legitimate role distinctions — confirm with the owner.
- **Peer *selection* (vs steering)** — S5 (row 2) made the peer panel steerable + added a
  quality override, but the *generator* is still the FMP sector/cap screen (`load_peer_comp`),
  the actual root cause of the "shit peers" complaint. The fix — an LLM `peer_selection` call
  for business-model comparables — is **spec'd, not built**: `directives/peer_selection_llm.md`,
  one dedicated session (owner decision 2026-06-13).

## 9. Status (2026-06-12)

- **Wave 1 SPAWNED** as 4 chips: S1 (Opus, keystone), S2 (Sonnet), S3 (Sonnet), S4 (Opus,
  dep S1). S2/S3 dependency-free; S4 lands after S1; all four are zero-schema.
- **Owner decisions folded in:** Tuscaloosa = Whale Rock + Appaloosa added; roster expanded
  with the multi-cycle growth tier (§5); discovery = both (entry bar + render cap).
- **Before Wave 2:** S6's `discovery_sources` roster seeds from §5's two tiers; the diet
  substrate + S6 + S10 each rebase migrations on the live head and reconcile the signal
  taxonomy with the single arbiter (§6).

### Program close-out — S12b gate resolved (2026-06-13): **signals spine NOT built.**

Waves 1–3 all merged. S12-first-half (`fact_ref` deep-links) shipped (#527). S12b — the
profiling gate — ran the cheap inbox fixes, re-measured, and resolved the gate. **The
8-writer signals spine is formally not built**, and the Instrument-Paradigm program closes
here. The identity-at-write-time rule it would have generalized is already codified
(design_language §Streams + GEMINI.md, enforced by `inbox_rank`'s shared resolver); the
spine was always conditional on profiling, and the profile says it would fix the wrong thing.

**What S12b did (the cheap inbox fixes, measured against prod's 726k-fact DB):**
- Batched the per-alert queued-action N+1 (`list_queued_actions_for_alerts`, one `IN`-query):
  ~31ms → ~10ms for 3 alerts, and now O(1) in alert count instead of one connection-open each.
- Moved `live_position_weights` OFF the render path: the morning reconciler (stage 0c,
  `sync_position_lifecycle`) materializes `data/portfolio_weights.json` from the live
  snapshot; the inbox render reads that disk cache, never the tracker. Eliminates the
  **~507ms cold connect-refusal spike** the first render of every process/5-min window paid.
  Offline reconcile preserves last-good (no blanking on a transient outage).
- Memoized `_thesis_tones` (a full `thesis_evaluations` scan) + `_news_meta` per (path,
  mtime): ~24ms → ~0.9ms on repeated renders, auto-busting on any DB write. No schema, no
  spine — a scoped two-read cache.
- Extended `execution/render_latency_baseline.py` with `inbox (home rail)` and
  `boot (GET / full inline)` panels — the S9 harness measured only the cockpit grid and
  silently omitted the inbox rail GET / also renders inline.

**Re-measure (same harness/DB; machine noisy, so the deterministic floors are what matter):**
- inbox (home rail): **~80–92ms** — well within budget; the named offenders are gone.
- boot (GET / full inline): **~1.7–1.9s p50** — STILL misses the sub-500ms target.
- **The residual offender is NOT the inbox.** It is `research_cockpit._eval_fundamentals`:
  a **~1.2s** (min 1135ms, deterministic) `ROW_NUMBER()` double-scan of all 726k
  `financial_facts` rows inside `build_cockpit_rows`. `tier_coverage_summary` is 21ms; the
  inbox is ~80ms. Even a perfect inbox/spine cannot pull boot under ~1.2s while that scan
  is on the render path.

**Decision rationale.** The gate ("build the spine only if a profiled render still misses
target") is satisfied in the letter but not the spirit: the render misses target, but the
spine targets the *inbox* (rank/dedupe over UNION'd source tables), and the inbox is already
cheap (~80ms). The spine would save tens of ms and never touch the 1.2s scan. So it is the
wrong fix — **not built.** Per the directive's "still misses → STOP, recommend, hand to
owner" branch: the real latency lever is a **separate, owner-go-decision follow-up** —
precompute per-ticker `rev_yoy` / `fcf_margin` (the eval-table fundamentals) in the morning
pipeline the same way weights now are, OR add a covering index / narrow the `_eval_fundamentals`
scan. High confidence (the 1.2s floor is deterministic and reproduces across runs). This is
out of S12b's named scope (inbox offenders only) and is handed off, not built.

**Hand-off COMPLETED (#539, 2026-06-13).** The recommended fix shipped: `_eval_fundamentals`
now precomputes per-ticker `rev_yoy`/`fcf_margin` to `data/cockpit_fundamentals.json` once
per morning-pipeline run (new Stage 0d, mirroring the `portfolio_weights.json` pattern); the
render reads the cache with a graceful live-DB fallback when absent. Result: **cockpit
1528→122ms p50, boot `GET /` 1847→340ms p50 — sub-500ms, the program target is met.** Every
pillar of the Instrument-Paradigm program is delivered and the boot-latency goal is achieved
WITHOUT the signals spine. **Program COMPLETE.**

**Remaining optional threads (owner-go only, none auto-spawned):** (1) buy-side ratings +
estimate-revision diet feeds — blocked on paid FMP-Ultimate access (decision: buy vs leave
scaffolded); (2) back-button/history dismissal for overlays — cut from S4 v1, small
follow-up; (3) peer *generator* — LLM business-model comparables (spec `peer_selection_llm.md`);
a sibling chip already shipped the generator (#530) + consumer (#523), so verify before
respawning; (4) the full signals spine stays available if a FUTURE profiled hot path (not
today's) genuinely needs cross-surface stream consolidation.

---

## 8. Highest-leverage first moves (the critic's ranking)

1. **S1** — opt-out conformance guard + namespace unfork + control-kit primitives. The
   keystone: 4 of 12 clusters bind to it; directly answers "too many fonts/colors/sizes";
   the guard makes every other "codify the rule" enforceable. Highest leverage by far.
2. **S2** — one prose boundary. Most viewing-pervasive defect; cheap (lift the proven
   renderer); dependency-free; permanently unregressable.
3. **S3** — inbox semantic-identity. The literal #1 complaint; zero-schema; seeds the
   unified item model S12 generalizes.
4. **S12-first-half** — `fact_ref` deep-links. Cheapest realization of "every datum is a
   doorway"; one column; reuses the peek primitive; decoupled from the deferred spine.
5. **S9** — cockpit doorways + `/api/peek/documents`. The most tangible delivery of the
   click-through request, on the screen the owner looks at daily.
