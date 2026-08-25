# Approved Mockup Implementation Handoff — Company Desk and Performance & Risk

**Status:** Company Desk implementation in progress on `codex/approved-company-desk`; Performance & Risk implemented on `origin/main`
**Approval recorded:** 2026-08-24, by the product owner in the Codex task
**Baseline reviewed:** `4fa3fe513163203d94b0286724ea259954cfa4e9`
**Project:** Research workspace UX hardening

## 1. Approval receipts

| Surface | Approved reference | SHA-256 | Approval scope |
|---|---|---|---|
| Company Desk | `mockups/company_desk_mockup.html` | `7adc8e5110ab265ff5959390852f4b4e4c4b00de775f77f43654b3a8c70ada01` | Page hierarchy, information density, inline price-action bands, DCF and artifact doorways, exploration layout, contracts/Say-Do interaction, responsive behavior |
| Performance & Risk | `mockups/performance_risk_mockup.html` | `e91926e964e249cb6a52cfc9023750d50f5082cfeba0a7e396a08b5c76a8290a` | One unified performance/risk workspace, period state, allocation projection, policy comparison and edit flow, responsive behavior |

Static mock values are illustrative. Production must use governed data and explicitly render unavailable, stale, partial, conflicting, or unencoded states. Approval does not authorize invented values or a render-time LLM call.

## 2. Outcome and boundaries

The approved mockups are not standalone deliverables. Approval creates a productionization obligation covering:

1. Front-end composition and interaction behavior.
2. Typed payloads and exact data ownership for every visible field.
3. Backend reads, writes, validation, persistence, and request-scoped wiring needed by the page.
4. Replacement or pruning of superseded routes, adapters, renderers, and tests after reachability proof.
5. Operational, release, rollback, and acceptance evidence.
6. Linear reconciliation with the approved reference and this handoff linked from implementation issues.

Out of scope for these approvals: redesigning Evaluation, Portfolio Cockpit, the standalone Full Brief body, or unrelated report sections. Their defects remain separate work unless the approved page directly opens or embeds them.

## 3. Current-to-target map

### 3.1 Company Desk

The current production baseline is an older four-tab decision workbench. The approved target is one decision-first page:

1. Company identity and switcher; DCF, earnings artifact, and Full Brief doorways.
2. Governed price/fair-value snapshot.
3. One owner decision and thesis-status block.
4. Inline Buy/Add/Hold/Trim bands; no unreadable encoded payload drawer.
5. “Why I own this company” and latest-quarter summary.
6. Recent governed updates and owner research questions.
7. Accessible Thesis Contracts / Say-Do tabs with readable four-quarter evidence.

### 3.2 Performance & Risk

The approved target is already present on `origin/main` through the following landed work:

| Capability | Evidence |
|---|---|
| Unified read-only workspace | PR #1327 / commit `be6f61ba` |
| Typed allocation projection | PR #1323 / commits `39eefa31`, `ceb147c0`, `5901f5b2` |
| Portfolio policy read proxy | PR #1335 / commit `fb380bcc` |
| Governed policy editor and read-after-write | PR #1336 / commit `4c856daf` |
| Requirements and acceptance detail | Linear BHA-79 |

Performance & Risk should not be rebuilt from the mockup. Remaining work is acceptance verification and durable document linkage.

## 4. Exhaustive implementation census

Every boundary is classified even when no change is required.

| Boundary | Company Desk disposition | Performance & Risk disposition | Owner / proof |
|---|---|---|---|
| Page renderer | **Replace** four-tab composition with approved one-page hierarchy | **Keep** landed unified panel | `src/pipeline/work_os_research.py`; `src/pipeline/performance_risk_panel.py` |
| Shared visual master | **Change** registered Company Desk grid/tab recipes; no local CSS | **Keep** landed recipes | `src/pipeline/work_os_styles.py`, `src/ui/design_registry.py`, design-sync receipt |
| Browser runtime | **Change** inline bands, DCF and reader doorways, accessible two-tab behavior, readable Say/Do | **Keep** period/policy runtime; verify stale-label and chart state | `src/pipeline/work_os_shell.py`; keyboard/browser tests |
| Company Desk read contract | **Change** add typed DCF route, price-action projection, canonical Say/Do projection | None | `src/pipeline/work_os_company.py` contract tests |
| Request-scoped database reuse | **Change** expose connection-owned sizing review loader | None | `src/advisor/sizing_intent_review.py`; connection tests |
| DCF backend | **Keep** latest governed DCF lookup and `/dcf/<ticker>` route; wire doorway only when available | None | route and DCF snapshot tests |
| Price-action backend | **Keep + wire** owner-ratified `PriceActionBandProjection`; render explicit unencoded state | None | `src/advisor/price_action_bands.py`; Desk contract tests |
| Say/Do backend | **Replace** separate synthetic decision-based endpoint with request-scoped canonical `management_commitments` projection | None | Desk contract; four-quarter test |
| Earnings / Full Brief backend | **Keep + wire** existing artifact descriptors and shared reader | None | artifact/readout route tests |
| Owner questions | **Keep + reconnect** capture and Decision Audit Log doorway | None | existing note route and shell tests |
| Persistence schema | **No new migration**; read existing DCF, sizing checkpoint, commitment, decision, note, and artifact tables | **No new migration** beyond landed policy work | schema census and migration history |
| Background jobs / LLM | **None**; page load remains deterministic and read-only | **None added** | transport absence tests / code review |
| Obsolete route | **Prune** `/api/company/<ticker>/say-do` after consumer removal | None | route inventory test |
| Obsolete execution adapter | **Prune** `execution/get_company_say_do.py` because it produced noncanonical synthetic content | None | reachability scan and deletion |
| Legacy renderer | **Prune** superseded four-tab Company Desk shell | None | source reachability and structural test |
| Tests | **Change/Add** production hierarchy, payload, DCF, bands, four-quarter Say/Do, route inventory, responsive and keyboard assertions | **Keep + rerun** panel, policy proxy, allocation and mockup tests | pytest receipts |
| Documentation | **Add** this handoff and link it from Linear | **Add link** from BHA-79 | repository and Linear readback |
| Operations surface | **No new operation**; one read route removed and one existing read response expanded | **No change** beyond landed policy action governance | explicit no-surface-change disposition |
| Assets | **None** | **None** | mockups are references, not runtime assets |
| Unknowns | Live data completeness for DCF/bands/Say-Do varies by ticker | Live production policy permissions and data freshness require acceptance | fail-closed UI states and live acceptance |

## 5. Durable payload and interaction contracts

### 5.1 Company Desk response

The existing `company_desk.v1` response remains backward compatible and gains:

| Field | Type / units | Source | Empty or error behavior |
|---|---|---|---|
| `position.dcf_url` | relative route or null | Presence of latest governed DCF run | Disabled, non-focusable DCF doorway when null |
| `price_action_bands` | typed `PriceActionBandProjection` | Latest exact-ticker sizing intent and ratified checkpoint payload | “Not encoded” and reason text; never decode or print opaque payloads |
| `say_do.status` | available/unavailable | `management_commitments` table | Explicit missing-source/schema/query state |
| `say_do.quarters[]` | latest four distinct `period_made` year-months | canonical commitment rows | Empty available history is distinct from unavailable source |
| `say_do.commitments[]` | stable row id, made/target periods, KPI, comparator, target, unit, narrative, actual, outcome, evaluation date, transcript source ref | canonical commitment rows | Unknown outcome renders “Tracking”; no fabricated result |

Numeric presentation uses locale formatting with at most two decimal places. Raw hashes, payload JSON, or encoded checkpoint data never render as primary user content.

### 5.2 Interactions

| Control | State transition | Success | Failure / empty | Accessibility |
|---|---|---|---|---|
| Company switch | current ticker → selected ticker; abort stale request | One typed Desk response hydrates page | Prior company remains open and live status explains failure | listbox/combobox semantics and announced status |
| DCF model | unavailable → enabled only with `dcf_url` | Navigate to `/dcf/<ticker>` | Disabled and removed from tab order | `aria-disabled`, focus state |
| Buy/Add/Hold/Trim | loading → ratified/partial/unencoded/unavailable | Inline governed thresholds | “Not encoded” plus reason; no payload drawer | text labels do not rely on color |
| Contracts / Say-Do | selected tab and panel update | Local switch; no network request | Available-empty or explicit unavailable message | roving tab index; Left/Right/Home/End |
| Full Say/Do | disabled → enabled with Full Brief | Opens shared reader at `saydo` | Disabled without artifact | native button semantics |
| Owner question | empty → saving → saved | Existing note endpoint and Desk refresh | status text; preserve input on failure | label, required field, live status |

## 6. Acceptance trace

| Requirement | Deterministic proof |
|---|---|
| Approved one-page hierarchy replaces four-tab page | `tests/test_approved_company_desk_production.py`; updated shell structure tests |
| DCF doorway uses a real route | Company Desk DCF contract tests and runtime assertion |
| Bands are inline and governed | typed projection tests; unencoded-state runtime assertion |
| Say/Do is canonical, readable, and at most four statement quarters | Company Desk projection test and rendered-runtime assertions |
| Duplicate Say/Do backend is pruned | route inventory test and reachability scan |
| Owner-question capability survives the redesign | shell interaction test |
| Tabs work by keyboard and mobile layout stacks safely | shell keyboard and responsive CSS tests; browser acceptance |
| No visual master drift | `python scripts/check_design_sync.py` |
| Performance & Risk remains complete | BHA-79 acceptance plus targeted panel/policy/allocation tests |

## 7. Delivery decomposition and dependencies

1. **Company contract and canonical evidence** — request-scoped sizing, DCF link, canonical four-quarter Say/Do. No front-end dependency.
2. **Company approved composition** — depends on step 1 payload fields; uses shared controls and registered styles.
3. **Interaction wiring and compatibility cleanup** — depends on steps 1–2; removes the duplicate endpoint and legacy renderer only after route/source census.
4. **Verification** — targeted tests, formatting, lint, typecheck, design sync, desktop/mobile browser checks.
5. **Linear reconciliation** — link approved mockup hashes and this handoff; relate Company issue to prior Company Research and DCF/sizing work; keep BHA-79 Done.

## 8. Release and rollback

| Concern | Trigger | Rollback action | Health proof |
|---|---|---|---|
| Company response fails for a valid tracked ticker | elevated `/api/work-os/companies/<ticker>/desk` 5xx or contract validation failure | Revert Company Desk implementation commit as one unit; restore prior route only if an actual consumer is proven | route contract tests and service logs without payload bodies |
| Canonical Say/Do table unavailable | projection reports unavailable | Keep page live with explicit unavailable state; do not restore synthetic endpoint | Desk still returns 200 and other sections hydrate |
| DCF or price ladder absent | null/unencoded projection | Keep disabled doorway / Not encoded state | no dead link and no raw payload exposure |
| Responsive or keyboard regression | browser acceptance fails | hold release and revert front-end composition commit | desktop/mobile screenshots and keyboard checklist |
| Performance policy mutation regression | BHA-79 policy tests or read-after-write fails | revert only landed policy-editor change, retaining read-only unified page | policy proxy and panel tests |

The rollback owner is the implementation issue owner. Rollback never invents values, re-enables render-time LLM work, or restores a known noncanonical data source without a new approval.

## 9. Linear reconciliation gate

Before the approved mockup flow is complete:

1. Resolve exact existing issues and read their current descriptions, comments, links, status, and relationships.
2. Create or update one implementation issue per independently shippable surface; do not overload a completed historical issue.
3. Link the approved mockup path/hash and this durable handoff document.
4. Include the frontend, backend, persistence, pruning, acceptance, release, and rollback rows above in the issue or linked document.
5. Encode dependencies and related prior work.
6. Re-read the saved document and issues and verify the intended content, links, status, and relationships.
7. Keep unresolved unknowns visible; do not mark the approval flow complete while required implementation or acceptance evidence is absent.

## 10. Reconciliation and implementation receipt

- Linear requirements document: `1d3ce5f2-0243-4462-b530-d93c5739ecad`.
- Company Desk implementation: BHA-94, **In Progress**, related to BHA-72, BHA-85, BHA-26, BHA-82, and BHA-92.
- Performance & Risk implementation: BHA-79 remains **Done**; the requirements document is attached and a reconciliation comment records that no duplicate issue is needed.
- Isolated implementation branch/worktree: `codex/approved-company-desk` at `/private/tmp/earnings-approved-mockups`; the owner's dirty primary checkout was not changed.
- Automated proof: Ruff passed; strict Pyright passed with zero errors; 195 targeted tests passed before final DCF semantics/reachability adjustments, followed by 22 focused tests passing; design sync passed.
- Browser proof: desktop and 390px mobile hierarchy, stacking, no document overflow, Contracts/Say-Do roving-tab keyboard behavior, and zero console errors were verified against the isolated build.
- Remaining activation proof: this Mac's available portfolio database does not contain the production `tracked_companies` table, so populated live-data hydration must be rechecked against the Windows production-shaped host before activation. This does not authorize activation.
