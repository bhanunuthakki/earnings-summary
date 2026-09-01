# Brief Library Scope 1 — Production Implementation Roadmap

**Status:** Phases 0–5 complete; released to the Windows production-shaped runtime and accepted through the live private Tailscale origin.
**Approval recorded:** 2026-08-29, by the product owner in the Codex task.
**Approved visual and interaction reference:** `mockups/brief_library_scope_1.html`
**Approved screenshot:** `mockups/brief_library_scope_1.jpg`
**Requirements source:** `mockups/brief_library_scope_1_global_requirements.md`
**Scope:** Brief Library productionization plus the approved program-wide dropdown, chip, artifact-title, and label changes.

## 1. Approval receipt and boundary

| Artifact | SHA-256 | Approval scope |
|---|---|---|
| `mockups/brief_library_scope_1.html` | `45f23bfed858c8bcf7ae4169e8e18a34a076393d44777a8f6aee98d789c47217` | Quiet faceted controls, one compact report per row, metadata chips, no report count, compact artifact titles, and automatic typeahead on Artifact Type, Company, and Coverage |
| `mockups/brief_library_scope_1.jpg` | `d8b904f3777aea0edb674859aff181470d493a55639775aa23803e648a01e05e` | Approved 1280 × 720 rendered state with the app-owned dropdown surface visible |

The mockup remains isolated prototype code. Production must recompose the approved direction through the registered UI masters; it must not import, copy wholesale, or serve the mockup.

Approval covers the shown Brief Library revision and the explicitly repeated global rules. It does not approve an unrelated Evaluation redesign, invented artifact data, a new persistence plane, or a render-time LLM call.

## 2. User outcome and task hypothesis

**Primary user:** the owner-researcher locating a persisted company artifact.

**Dominant action:** narrow the library by artifact kind, company, and coverage role, then open one exact artifact.

**Information order:** artifact identity → categorical metadata → freshness/availability → open action.

**Observed friction:** native dropdown popovers break the app theme; filters are visually heavy and only partly dependent; three-column cards slow scanning; artifact identity is split across title and prose metadata; and repeated quarter-end text adds no information.

**Smallest expected improvement:** a quiet, keyboard-first facet band above a single compact row list, with the complete artifact identity in a wrapping title and the remaining categories in consistent chips.

## 3. Ratified product and design rules

### 3.1 Program-wide searchable single-select

Every single-select dropdown in the program adopts one app-owned searchable control. This is a shared component change, not a Brief Library exception.

- The trigger and listbox use the same registered surface, border, text, focus, selected, and elevation roles in both themes. No browser- or OS-drawn white popup remains visible.
- Typing filters the open dropdown or the focused dropdown trigger. A surface may nominate one default dropdown to receive typing when no dropdown has focus; Brief Library nominates Company.
- The trigger displays the live typed buffer as soon as typing begins. It does not add a search icon, `Search:` prefix, visible search field, or count to that temporary value.
- Backspace edits the buffer. Escape and outside dismissal abandon the buffer and restore the committed label. ArrowUp/ArrowDown move through results. Enter or Space commits the focused option and returns focus to the trigger.
- The control exposes combobox/listbox semantics, an active option, selected state, result count or empty-state announcements, and deterministic focus return.
- Search matches the user-visible label and approved aliases. Ticker dropdowns match ticker and company name.
- A closed choice with no matching result remains unchanged; typing never creates a new persisted value.

Dynamic cross-filtering is required for related table/library facets. It is not implicitly applied to unrelated action or mutation selects.

### 3.2 Brief Library facets

- Artifact Type, Company, and Coverage are mutually dependent facets.
- Each option set and option count is computed against the other two committed selections.
- A committed value that becomes incompatible clears deterministically, updates the visible rows once, and is announced.
- Exact counts may render only when computed from a complete result universe or supplied as exact server-side facet counts. The current `limit=100` response cannot support an unqualified completeness claim by itself.
- Clear resets all three facets locally and restores the unfiltered complete result set.

### 3.3 Artifact rows, titles, and chips

- Render one report per compact horizontal row; rows stack at narrow widths.
- Remove the top-right report count.
- The title contract is `[TICKER] [Qn yy] [Brief | Pre-Earnings | Post-Earnings]`, for example `BKNG Q2 26 Post-Earnings`.
- Titles wrap naturally and are not ellipsized. The title may consume vertical space without increasing every row to a card-like height.
- Do not repeat `quarter ended …` when the title already carries the exact fiscal quarter.
- Artifact kind, Coverage Role, generated/report date, and availability/freshness render as chips when present.
- Artifact-kind and Coverage Role colors use a closed, registered mapping. Status uses the existing status semantics and a non-color cue. Consumers do not invent local chip colors.
- `Open` uses the existing exact artifact/readout doorway. Filtering and opening remain deterministic and LLM-free.

## 4. Current production truth and capability gaps

### 4.1 Current Brief Library

- `src/pipeline/work_os_research.py` emits three native `<select>` controls and a three-column card grid.
- `src/pipeline/work_os_shell.py` rebuilds Company options when Coverage changes, but Artifact Type and Coverage do not receive the same dependent option/count treatment.
- `/api/work-os/briefs` reads the persisted Full Brief index and accepts `ticker`, `coverage_role`, `status`, `cursor`, and `limit`; it does not provide a unified artifact-kind facet contract or exact facet counts.
- Post-Earnings Readouts enter the library from portfolio hydration, not from `/api/work-os/briefs`.
- The approved mockup shows Pre-Earnings artifacts, but the current production library path does not prove a Pre-Earnings inventory. Production must show them only after a canonical persisted reader and exact route are wired.
- `ReportArtifactRef` currently fixes `artifact_kind` to `full_brief` and does not carry an exact fiscal-quarter field. A report date is not a safe substitute for fiscal quarter.

### 4.2 Current shared controls

- `src/ui/controls.py` styles native `<select>` elements and supplies `.k-menu`, but it does not yet own one complete searchable single-select markup/runtime contract.
- `src/ui/design_registry.py` registers chips and menu styling, but not the approved searchable single-select behavior and its states.
- Current single-selects are spread across research, Copilot, Explore, portfolio, advisor, journal, triage, operations, lifecycle, report, and governance surfaces. Replacing only the three Brief Library selects would violate the approval.
- Existing multiple-selection listboxes in Explore/Key Metrics are a distinct interaction shape. They must be inventoried and given the same app-owned visual substrate, but a single-select migration must not silently change their multi-selection semantics.

## 5. Data-truth register

| Visible value | Production authority | Truth state and required behavior |
|---|---|---|
| Ticker and company name | tracked-company projections used by portfolio hydration and Research Companies | Live/derived. Deduplicate by normalized ticker; preserve exact coverage role and disclose unavailable company names. |
| Full Brief identity | immutable report artifact index in `src/report/artifacts.py` | Persisted. Keep stable `artifact_id`, manifest/body hashes, report date, reader mode, and route. |
| Post-Earnings identity | persisted `llm_artifacts` quarter index surfaced by portfolio hydration | Persisted. Preserve exact `artifact_id`, ticker, fiscal period, generated time, route, and coverage role. |
| Pre-Earnings identity | no complete Brief Library read contract proven at roadmap baseline | Unavailable until a canonical persisted index/route exists. Do not render illustrative mockup rows as production data. |
| Artifact kind | typed item discriminator in a unified library projection | Derived from canonical purpose/kind, never filename prose. |
| Fiscal-quarter title segment | exact fiscal period carried by the artifact projection | Required for the compact title. If absent, show an explicit period-unavailable state; never derive quarter from report date. |
| Coverage Role | active tracked-company coverage, with the Full Brief's stored role as fallback | Derived for the current library relationship. Unsupported active roles remain `unknown`; archived coverage falls back only where the immutable Full Brief carries a stored role. |
| Generated/report date | artifact metadata | Persisted. Present as a chip only when exact; distinguish report date from generation time. |
| Availability/freshness | artifact resolver plus source/body presence and freshness policy | Derived. Preserve available, stale, degraded, missing-body, or unavailable states; never display `Available` merely because an index row exists. |
| Facet option counts | complete unified result universe or exact server facet summary | Derived. Hide or qualify counts when completeness is unknown. |

All static rows and counts in the mockup are illustrative.

## 6. Interaction register

| Control | Intent | Mutation | Success | Empty/failure | Accessibility |
|---|---|---|---|---|---|
| Artifact Type | Restrict visible artifact kinds | Read-only local/query state | Compatible rows and other facets update once | `No matching options`; committed selection remains until explicitly cleared or made incompatible | Shared combobox/listbox keyboard contract and live result announcement |
| Company | Restrict to one ticker | Read-only local/query state | Ticker/company matching opens automatically while typing | No result leaves prior committed company unchanged | Company is the surface default when typing begins outside an editable field |
| Coverage | Restrict by Coverage Role | Read-only local/query state | Compatible companies, kinds, counts, and rows update | Unknown/unavailable roles remain explicit | Same searchable behavior as Artifact Type and Company |
| Clear | Remove all facet constraints | Read-only local/query state | All facets and rows reset atomically | Disabled when no committed filter exists | Native button semantics and announced result count |
| Open | Open the exact persisted artifact | Navigation/overlay only | Existing Full Brief reader or Post-Earnings peek receives the stable identity | Disabled or explicit unavailable state when body/route is absent | Exact accessible name includes ticker, quarter, and kind |
| Program-wide action selects | Choose a closed value for an existing form/action | No new write; existing submit/action remains the only writer | Search commits the same typed value the native select previously emitted | No match does not create or submit data | Same keyboard contract; existing validation, confirmation, and receipts remain intact |

## 7. Exhaustive implementation boundary census

| Boundary | Disposition | Exact seam / proof |
|---|---|---|
| Canonical vocabulary | **Change** | Ratify Full Research Brief/short `Brief`, compact artifact title, and shared searchable single-select terms in `DEFINITIONS.md`; preserve existing Pre-Earnings Brief, Post-Earnings Readout, and Coverage Role meanings. |
| Interaction contract | **Change** | Add the program-wide typeahead, dismissal, focus, selection, announcement, and surface-default rules to `directives/interaction_contract.md`. |
| Visual contract | **Change** | Record the app-owned searchable single-select and closed category-chip variants in `directives/design_language.md`. |
| Shared control master | **Change** | `src/ui/controls.py`: add one typed emitter/style/runtime contract over `.k-menu`; retain no consumer-owned dropdown skins. |
| Tokens | **Keep unless a missing semantic role is proven** | `src/ui/tokens.py`; reuse surface, paper, border, accent, status, focus, and popover elevation tokens. |
| Design registry and guards | **Change** | `src/ui/design_registry.py`, `src/ui/conformance_scan.py`, registry/conformance tests: register states and add a shrinking native-single-select migration allowlist, then a zero-unapproved-native-select guard. |
| Brief Library shell | **Replace** | `src/pipeline/work_os_research.py`: native selects → shared controls; compact semantic row/list shell and truthful loading/empty/degraded states. |
| Brief Library runtime | **Replace** | `src/pipeline/work_os_shell.py`: one facet state model, dependent options/counts, typeahead through the shared controller, stable open actions, no duplicated control runtime. |
| Brief Library styles | **Replace** | `src/pipeline/work_os_styles.py`: three-column card grid → registered compact list/row recipe; layout only beyond the shared masters. |
| Library read contract | **Change** | `execution/comments_server_content_routes.py` and the owning typed adapter: one unified response or exact coordinated projections for Full Brief, Pre-Earnings, and Post-Earnings items plus complete facet summaries. |
| Artifact models/index | **Change only where evidence requires it** | `src/report/artifacts.py` and earnings artifact stores: carry exact artifact kind and fiscal period without overwriting immutable identity or inferring quarter from dates. Version any durable index schema change. |
| Request-scoped DB | **Keep** | Continue `context.get_read_db()`; no extra connection per facet or row. |
| Persistence/migrations | **No database migration expected** | Prefer existing immutable artifact stores and a versioned rebuildable index. Add a DB migration only if discovery proves the canonical fiscal-period/kind identity cannot be exposed otherwise. |
| LLM/jobs | **None** | Library load, filtering, counts, titles, and opening remain deterministic; no new scheduled job or render-time generation. |
| Operations surface | **No-surface-change disposition** | This work adds no operation or operator action. If an index rebuild operation is later required, route it separately through `directives/operations_governance_surface.md`. |
| Tests | **Change/Add** | Shared-control unit/contract tests; facet truth and compatibility tests; Brief Library payload/DOM tests; conformance guard; keyboard, responsive, console, and network browser evidence. |
| Prototype assets | **Keep isolated as approval evidence** | `mockups/brief_library_scope_1.*`; never imported by production. |

### 7.1 Program-wide single-select migration inventory

The baseline literal-source census found single-select consumers in:

- Research/Work OS: Brief Library (3), Fact & Metric Playground ticker, Copilot company/category.
- Explore/portfolio/advisor: visualization transform/cadence/DCF driver, portfolio scenario, advisor memo ticker/horizon, sizing action, allocation conviction, positioning posture.
- Journal/triage/operations/lifecycle: link target, reclassification, kind/status, triage route, snooze reason, mobile proposed action, position outcome, note kind.
- Reports/governance: report intent/note kind and analytical-dashboard budget mode.

The Explore and Key Metrics multi-select listboxes remain a separately tested multi-selection variant. The migration census must be regenerated from source immediately before implementation; the list above is a roadmap baseline, not a permanent hand-maintained registry.

## 8. Delivery roadmap

### Phase 0 — Canonical contract and generated census

1. Ratify the vocabulary and behavior in `DEFINITIONS.md`, `directives/interaction_contract.md`, and `directives/design_language.md`.
2. Generate the live native-single-select and custom-combobox inventory, including production import reachability and current keyboard behavior.
3. Define the closed chip mapping for artifact kinds and every applicable Coverage Role. Use semantic roles; do not hard-code the mockup's RGB values.
4. Add failing registry/conformance tests for an unregistered local dropdown implementation.

**Exit gate:** one approved shared contract, complete current census, and red tests proving local/native drift is detected.

### Phase 1 — Shared searchable single-select master

1. Add one shared markup/CSS/runtime primitive with typed option identity, label, aliases, selected state, disabled state, and optional exact count.
2. Implement the full keyboard, focus, dismissal, live-buffer, announcement, and default-target behavior.
3. Cover light/dark themes, long labels, no matches, disabled options, mobile positioning, viewport collision, reduced motion, and JavaScript failure.
4. Register the primitive and make consumer CSS limited to layout/width.

**Exit gate:** deterministic component tests and browser proof at desktop and narrow widths; no native popup appears in the exercised states.

### Phase 2 — Unified Brief Library truth contract

1. Define a versioned `BriefLibraryItem` union for Full Brief, Pre-Earnings, and Post-Earnings identities.
2. Carry exact fiscal period, artifact kind, creation-time Coverage Role, generated/report time, availability/freshness, stable route, and source/body presence.
3. Return exact compatible facet summaries or a complete bounded result universe. Cursor/limit semantics must not produce misleading counts.
4. Keep missing Pre-Earnings capability explicit until its canonical store and reader route are proven.

**Exit gate:** schema tests prove every visible field's authority, exact counts under compound filters, stable pagination semantics, and explicit degraded states.

### Phase 3 — Brief Library production composition

1. Replace the three native selects with the shared searchable controls.
2. Implement mutually dependent Artifact Type, Company, and Coverage facets with Company as the default typing target.
3. Replace the three-column card grid with one compact row per artifact.
4. Apply the compact title formatter, wrapping title, registered chips, exact doorway, no report count, and no redundant quarter-end phrase.
5. Preserve loading, empty, unavailable, stale/degraded, and clear-filter states.

**Exit gate:** all Scope 1 acceptance rows pass against production-rendered HTML and typed fixtures; browser evidence matches the approved hierarchy without importing prototype code.

### Phase 4 — Global adoption

1. Migrate read/filter/navigation single-selects first, including Copilot, Fact Playground, advisor ticker, and portfolio scenario controls.
2. Migrate action/mutation single-selects without changing when or how their existing writes occur.
3. Migrate or explicitly register the multi-select variant while preserving multi-selection semantics.
4. Apply the approved artifact title formatter and category-chip mappings to every artifact doorway, list, card, and reader header that shows the same concepts.
5. Remove obsolete native-select styling and migration allowlists only after the generated census reaches zero unapproved single-selects.

**Exit gate:** the conformance scan proves no unapproved native single-select or locally skinned dropdown remains in production imports; behavioral tests prove existing writes still require their original explicit submit/action.

### Phase 5 — Release and live acceptance

1. Run targeted tests during each phase, then the repository's merge-facing validation and `python scripts/check_design_sync.py`.
2. Verify desktop, 720px, and 390px layouts; keyboard-only operation; focus return; overflow/collision; empty/degraded states; console errors; and request counts.
3. Verify against an explicit disposable migrated Mac database only. Do not create or use `/Applications/earnings-summary/data/portfolio.db`.
4. Before activation, verify populated artifacts on the Windows production-shaped host and hydrate from Mac through the exact live Tailscale Serve HTTPS origin.

**Exit gate:** complete automated and browser receipts, live read-only hydration proof, and no unresolved critical truth or accessibility gap.

## 9. Acceptance trace

| Approved requirement | Deterministic proof |
|---|---|
| No white/inconsistent dropdown background | shared-control token tests; dark/light browser screenshots; no native single-select in production-import census |
| Typeahead works for Artifact Type, Company, Coverage, and every program dropdown | shared-control keyboard suite plus per-surface adoption census |
| Trigger becomes typed text with no marker | DOM assertions during printable-key and Backspace sequences |
| Company auto-triggers when Brief Library has no focused dropdown | page-level keyboard test excluding editable targets |
| Facets respond to the other selections | complete cross-product fixture tests and exact option/count assertions |
| Incompatible retained values clear deterministically | state-transition unit and DOM tests with one render/fetch assertion |
| One compact report per row | production structure test and responsive screenshots |
| No top-right report count | negative DOM assertion |
| Artifact and coverage metadata use registered colors | registry variant test and conformance scan; non-color label assertion |
| `quarter ended` is removed from compact rows | negative text assertion |
| Title is `[Ticker] [QX 26] [type]` and wraps | formatter tests for all three kinds, exact fiscal-period fixtures, and narrow-width screenshot |
| No invented Pre-Earnings or exact counts | degraded/absent capability tests and pagination completeness tests |
| Existing actions do not mutate on dropdown selection | mutation-surface tests preserve explicit submit/action and receipts |

## 10. Release decomposition and rollback

Land the work as small, independently reversible changes:

1. **Contract + shared primitive** — definitions/directives, component, registry, tests.
2. **Unified library read model** — typed projections and exact facet truth; no UI cutover.
3. **Brief Library cutover** — production composition/runtime/styles and Scope 1 acceptance.
4. **Global read/filter adoption** — non-mutating dropdowns and artifact labels/chips.
5. **Global action adoption + native cleanup** — mutation selects, multi-select disposition, zero-drift guard.

| Failure | Rollback action | Truth preserved |
|---|---|---|
| Shared control keyboard/focus regression | Revert the shared-control adoption commit for affected surfaces; retain the approved contract and tests | Existing selected values and writes remain unchanged |
| Unified inventory omits or duplicates artifacts | Keep the old read path active, disable exact counts, and revert the adapter cutover | Immutable artifact stores and identities are untouched |
| Brief Library rendering regression | Revert the page cutover as one unit; do not serve the mockup | Existing library remains available through the prior production renderer |
| Mutation dropdown changes write timing/value | Hold that adoption batch and restore its prior control until parity passes | Existing submit/confirmation/idempotency behavior remains authoritative |
| Live Windows hydration differs from fixtures | Do not activate; render explicit unavailable/degraded state while investigating | No Mac fallback database and no invented values |

No rollback may restore a known misleading count, infer a fiscal quarter from a report date, import prototype fixtures, or add a render-time LLM fallback.

## 11. Roadmap record

- **Current phase:** Complete — production activation and live hydration accepted.
- **Completed locally:** canonical definitions/contracts; registered Searchable Single-Select; generated standalone-document adoption guard; unified `brief_library.v2` projection with exact related-facet summaries; exact pre/post artifact routes; compact production rows, titles, and chips; standalone and Work OS adoption; generated token/control mirrors; design registry/conformance receipts; workspace goldens; external-Chrome visual acceptance against an explicit disposable database at desktop and 390px widths.
- **Browser acceptance receipt:** desktop and mobile layouts, all three dropdown typeaheads, typed-buffer replacement, themed menus, exact counts, dependent facet updates, compact wrapping rows, Full Brief reader opening, exact Pre-Earnings artifact opening, and zero browser console warnings/errors were exercised on 2026-08-29. Browser inspection found and fixed two shared-control defects (hidden nonmatches remaining visually present; mobile menu specificity collapsing the option sheet) and one Brief Library drift (mobile action stacking below the row instead of matching the approved two-line compact recipe).
- **Live acceptance receipt:** release commit `4b184dbe401eaaffbf6ec744fa388aa6820f2e3d` was activated in detached-head mode in the clean Windows runtime checkout on 2026-08-30. The pre-existing modified `dcf/BN.xlsx`, `dcf/MELI.xlsx`, and `dcf/NU.xlsx` workbooks were preserved. `es-dashboard` was started successfully, Windows-local `/healthz` returned `{"status":"ok"}`, and `/api/work-os/briefs?limit=100` returned `brief_library.v2` with 100 items at the requested limit.
- **Mac-through-Tailscale receipt:** live `tailscale serve status` reported the private tailnet-only origin `https://the owner-thinkpad.tail61adb2.ts.net` proxying to `http://127.0.0.1:7421`. From Mac Chrome, the production Brief Library rendered compact rows and colored category chips; Artifact Type typed `post` to the single `Post-Earnings 11` option; Ticker typed `me` to exact MELI and META options; Coverage typed `port` to `Portfolio 1`; committing Post-Earnings → MELI → Portfolio reduced the result to `MELI Q2 26 Post-Earnings`; and `Open artifact` opened exact persisted artifact `#3508`.
- **Implementation status:** Released and live-accepted. The full repository matrix was delegated through the documented `FAST_PUSH=1` path; the pre-push fast gate passed 192 tests, while the feature-focused final suite passed 262 tests. Design sync, 130-surface conformance, Ruff, strict Pyright on the touched core modules, workspace goldens, desktop rendering, and 390px rendering all passed.
- **Production authorization:** The owner authorized implementation and Windows activation in this task. No production database write, public listener, Funnel exposure, poller restart, or unrelated runtime mutation was performed.
- **Tracking:** The dated roadmap under `directives/roadmap_2026_08_consolidated.md` is historical and superseded by Linear. If this work is reconciled into Linear, create or update independently shippable issues for the phase decomposition above and link this document plus the approved artifact hash; do not append active work to that historical directive.
