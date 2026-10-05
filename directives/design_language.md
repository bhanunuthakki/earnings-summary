# Design language

**Status:** canonical visual contract. Inventories live in code.

## Operating metadata

- **Target:** shipped components and emitters that change rendered UI.
- **Inputs:** task, hierarchy, state, and owning master.
- **Output:** registered markup or a tested master/registry extension.
- **Refresh:** decision-model changes only.
- **Logical Idempotency Key:** owning master/contract and named visual decision.
- **Content Identity:** master, registry, or rendered evidence digest.
- **Observation Version:** current inspected master and registry revision.
- **Attempt Identity:** unique validation/browser invocation and receipt.
- **Rate-limit budget:** none; verification is local and deterministic.
- **Failure policy:** reject changes; never widen approval to pass.

## 1. Authority boundary

Consumers select master recipes; they cannot create visual recipes.

| Concern | Executable authority |
|---|---|
| Tokens and scales | `src/ui/tokens.py` |
| Controls and composition | `src/ui/controls.py` |
| React mirrors | `scripts/gen_design_tokens.py`, `scripts/gen_design_controls.py` |
| Family masters and approvals | `src/ui/design_registry.py` |
| Conformance detection | `src/ui/conformance_scan.py` |
| Census, debt, and receipt | `execution/verify_design_conformance.py` |
| Merge check | `scripts/check_design_sync.py` |

`src/ui/design_registry.py` owns the inventory. Do not copy paths, counts, approvals, or debt.

## 2. Consumer contract

Surfaces provide content, semantic HTML, data attributes, and nonvisual hooks. UI comes from:

1. the global tokens and controls;
2. one registered family master for surface-specific arrangement; and
3. an exact typed contract for any approved dynamic or runtime visual state.

Consumers must not add local visual CSS, inline styles, runtime style mutation, arbitrary SVG presentation, or open-ended `style` APIs.

Use the nearest variant or extend its master. Cross-family reuse is global.

### Same-project page continuity

Start from the nearest shipped sibling serving the same task. Preserve its registered shell,
navigation, four type roles, controls, density, responsive behavior, and state anatomy; content may differ.
Add a new visual family only when existing families cannot express the task, with typed rationale
and an adversarial continuity test.

## 3. Visual grammar

Only executable masters own literal values.

### Typography

- Use four visible roles: display, title, body, and meta.
- Use the sans family for prose and labels.
- Use mono only for financial values, tickers, timestamps, code, and source locators.
- Weight and case express hierarchy; consumers cannot add type roles.

### Color

- Use semantic ground/surface, text, border, accent, and status roles.
- Accent marks interaction, selection, focus, or unread state, never decoration.
- Status colors communicate status only and must retain a non-color cue.
- Raw colors, ad-hoc opacity, gradients, and consumer aliases are prohibited.

### Shape, depth, and motion

- Shape comes from registered control and family recipes: radius, border, shadow, blur, and transform.
- Use elevation only to explain layering or focus.
- Motion uses the registered transition vocabulary and honors reduced-motion.
- Different corners, shadows, motion, or overlay geometry require a master change.

### Spacing, grids, and indents

- Use the spacing ladder and registered grid, rail, and indent recipes.
- Controls are compact; reading is comfortable. Spaciousness is for hierarchy only.
- Follow content alignment; no one-off offsets, widths, gaps, or breakpoints.

## 4. Composition grammar

Use canonical primitives:

| Intent | Primitive |
|---|---|
| Action | `.k-btn` with a registered intent/size variant |
| Filled status | `.k-pill` with a status variant |
| Filter, kind, or outline tag | `.k-chip` with a registered variant |
| Dropdown | Searchable Single-Select (`ui.controls`) |
| Callout or grouped context | `.k-well` |
| Ticker plus company | `ticker_label()` |
| Stored or model-generated prose | `ui.prose.render_prose()` |

Compose the kit; on-scale tokens do not legitimize hand-built components.

Recipes cover semantics, keyboard access, focus, labels, contrast, non-color cues, and reduced motion.

## 5. Arrangement

- Application surfaces optimize for decisions: one dominant operating band, compact controls,
  explicit state, and progressive disclosure.
- Research documents optimize for reading: clear hierarchy, restrained density, and traceable evidence.
- Responsive behavior, empty/loading/error states, and overlays use registered recipes.

### Collapsible panels and vertical space

- Supporting navigation and side panels collapse with the shared quiet icon button and
  collapse icon across families. Masters own geometry; keep the expand control visible at the closed edge.
- Closing panels releases space for visible selected content. Retain a compact label or material status when needed.
- Combine identity, period, state, and actions in one compact row; wrap at narrow widths.
  Avoid redundant stacked rows.
- Put tactical identifiers, capture details, diagnostics, and general disclaimers in disclosures
  or linked footnotes, not full-width bands above content.
  Keep material period, unit, basis, stale state, and data gaps beside each claim; compact status controls can expose detail.
- Compare open/closed panels and space above useful content. Each row needs a distinct task or material state.

### Compositional restraint

The shared `frontend-quality` procedure owns the generic rubric. This project narrows it:

- Follow §3 typography and color rules. Decorative left rails and ornamental variation are not recipes.
- Start in normal flow with registered family recipes. Each nested box needs a named semantic,
  state, interaction, or ownership boundary; flatten the rest.
- Equivalent sections share a registered grammar. Bullets and indents express content structure;
  subtitles add information rather than repeat titles.
- Before the composed guard, remove non-semantic decoration and redundant containers, headings,
  subtitles, badges, dividers, and icons. Remaining visual differences need a typed master rationale and adversarial extension test.
- Inspect the sibling and affected page in a browser before material implementation, then verify
  final states and widths. Mockup CSS is prototype-only; production uses registered masters.

Product behavior is owned elsewhere. Do not copy it into this directive:

- navigation and destination hierarchy: `directives/navigation_ia.md`; executable routes and shell
  tests remain authority, and the directive is draft evidence only until owner approval;
- all active interaction, doorway, overlay, and dismissal behavior:
  `directives/interaction_contract.md`;
- `directives/interaction_paradigm_2026_06.md` is record-only history and never an input to
  current behavior;
- comments and chat: `directives/report_comments_and_chat.md`;
- provenance behavior: `directives/data_provenance.md`;
- operational controls: `directives/operations_governance_surface.md`;
- discovery and ingestion policy: `directives/news_sources_plan.md` and
  `directives/ir_events_ingestion.md`.

Those contracts may specify behavior, data, and state. They do not authorize a new visual recipe.

## 6. Extension protocol

For a legitimate new visual need:

1. Identify the owning global or family master. If none exists, add one typed
   master entry rather than styling the consumer.
2. Add the smallest closed vocabulary: semantic token, component variant, family
   recipe, or exact dynamic/runtime contract. Do not add an open-ended style bag.
3. Add a red/green adversarial test that proves the requested decision passes and
   a nearby drift attempt fails.
4. Update `src/ui/design_registry.py` with owner and rationale when the census,
   master set, geometry, evidence mode, or approval changes.
5. Regenerate mirrors and run the merge-facing check.

Approvals are exact and typed. Only nonvisual policy infrastructure has permanent exemptions.
Quarantine is temporary, owned, and shrink-only; debt must not grow.

Edit only when a decision rule changes. Exclude history, diaries, generated tables, surface
counts, and product specifications.

## 7. Verification

Run the composed guard after visual changes:

```powershell
python scripts/check_design_sync.py
```

For conformance, inspect the deterministic receipt:

```powershell
python execution/verify_design_conformance.py --check --route-canaries
python -m pytest tests/test_design_registry.py tests/test_design_conformance_canonical.py tests/test_design_sync.py tests/test_ui_controls.py -q
```

Route canaries use production renders at both widths, wait for hydration, walk light/open-shadow
DOM, and check archetype, anatomy, type, spacing, shape, depth, alignment, fit, and reduced motion.

Report-renderer changes require workspace golden regeneration and diff review;
generated React changes require the design-system check/build.
