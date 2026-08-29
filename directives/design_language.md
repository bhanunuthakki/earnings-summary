# Design language

**Status:** canonical agent contract for the visual system.

Governs visual decisions; inventories live in code.

## Operating metadata

- **Target:** every shipped component or emitter that changes rendered UI.
- **Inputs:** task, hierarchy, state, and owning master.
- **Output:** registered markup or a tested master/registry extension.
- **Refresh:** only when the decision model changes, not when inventory changes.
- **Logical Idempotency Key:** owning master or canonical contract plus the named visual decision.
- **Content Identity:** digest of master, registry, or rendered evidence.
- **Observation Version:** current master and registry revision inspected for the change.
- **Attempt Identity:** unique validation or browser-audit invocation and its receipt.
- **Rate-limit budget:** none; verification is local and deterministic.
- **Failure policy:** reject the change; never widen an approval to make a check green.

## 1. Authority boundary

Visual decisions are closed: consumers select a master recipe; they do not create one.

| Concern | Executable authority |
|---|---|
| Tokens and scales | `src/ui/tokens.py` |
| Controls and composition | `src/ui/controls.py` |
| React mirrors | `scripts/gen_design_tokens.py`, `scripts/gen_design_controls.py` |
| Family masters and approvals | `src/ui/design_registry.py` |
| Conformance detection | `src/ui/conformance_scan.py` |
| Census, debt, and receipt | `execution/verify_design_conformance.py` |
| Merge check | `scripts/check_design_sync.py` |

`src/ui/design_registry.py` is the inventory oracle. Do not copy its paths, counts, approvals, or debt.

## 2. Consumer contract

A governed surface may provide content, semantic HTML, data attributes, and nonvisual hooks. UI comes from:

1. the global tokens and controls;
2. one registered family master for surface-specific arrangement; and
3. an exact typed contract for any approved dynamic or runtime visual state.

Consumers must not add local visual CSS, inline styles, runtime style mutation, arbitrary SVG presentation, or open-ended `style` APIs.

If no recipe fits, choose the nearest variant or extend the owning master. Cross-family reuse is global.

### Same-project page continuity

For a new or reworked page, start from the nearest shipped sibling serving the same task.
Preserve its registered shell, navigation, four type roles, controls, density, responsive
behavior, and state anatomy; incidental content need not match.

A new visual family is allowed only when no existing family can express the task. Use the
extension protocol with a typed rationale and an adversarial continuity test.

## 3. Visual grammar

Literal values live only in executable masters.

### Typography

- Use four visible roles: display, title, body, and meta.
- Use the sans family for prose and labels.
- Use mono only for financial values, tickers, timestamps, code, and source locators.
- Weight and case communicate hierarchy; consumers cannot invent another type role.

### Color

- Use semantic ground/surface, text, border, accent, and status roles.
- Accent marks interaction, selection, focus, or unread state, never decoration.
- Status colors communicate status only and must retain a non-color cue.
- Raw colors, ad-hoc opacity, gradients, and consumer aliases are prohibited.

### Shape and depth

- Shape comes from registered control and family recipes: radius, border, shadow, blur, and transform.
- Use elevation only to explain layering or focus.
- Different corners, shadows, or overlay geometry require a master change.

### Motion

- Animate only for feedback, state legibility, spatial continuity, or preventing jarring change—never decoration.
- Keep frequent/keyboard flows and updates instant; values, charts, evidence and reading position stay stable.
- Occasional overlays/disclosures may use an anchored recipe. Masters own timing, properties and reduced motion; `directives/interaction_contract.md` owns behavior.

### Spacing, grids, and indents

- Use the spacing ladder and registered grid, rail, and indent recipes.
- Density is compact for controls, comfortable for reading, and spacious only for hierarchy.
- Follow content alignment; no one-off offsets, widths, gaps, or breakpoints.

## 4. Composition grammar

Use the canonical primitives rather than lookalikes:

| Intent | Primitive |
|---|---|
| Action | `.k-btn` with a registered intent/size variant |
| Filled status | `.k-pill` with a status variant |
| Filter, kind, or outline tag | `.k-chip` with a registered variant |
| Callout or grouped context | `.k-well` |
| Ticker plus company | `ticker_label()` |
| Stored or model-generated prose | `ui.prose.render_prose()` |

An on-scale token does not legitimize a hand-built component. Compose the kit.

Recipes include semantics, keyboard access, focus, labels, contrast, non-color cues, and reduced motion.

## 5. Arrangement

- Application surfaces optimize for decisions: one dominant operating band, compact controls,
  explicit state, and progressive disclosure.
- Research documents optimize for reading: clear hierarchy, restrained density, and traceable evidence.
- Responsive behavior, empty/loading/error states, and overlays use registered recipes.

### Compositional restraint

The shared `frontend-quality` procedure owns the generic rubric. This project narrows it:

- Keep the four registered type roles and sans/mono uses; hierarchy must not add another.
- Start in normal flow and use registered family recipes. Every nested boxed region needs a named
  semantic, state, interaction, or ownership boundary; flatten the rest.
- Accent is interaction/selection/focus/unread; status keeps its own role and non-color cue.
  Decorative rails and ornamental variation are not recipes.
- Equivalent sections share a registered composition grammar. Bullets and indentation represent
  actual content structure, not texture; subtitles add information rather than repeat titles.
- Before the composed guard, perform the page-level reduction pass: remove non-semantic
  decoration, redundant containers, headings, subtitles, badges, dividers, and icons. A remaining
  visual difference needs a typed master rationale and adversarial test under the extension protocol.
- For material work, inspect the sibling and affected page in a browser before implementation,
  then exercise final states and widths. Mockup CSS is prototype-only; production recomposes it
  through registered masters.

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
2. Add the smallest closed vocabulary: semantic token, component variant, family recipe, or exact
   dynamic/runtime contract; no open style bag. For motion, record purpose, frequency, input, anchor/direction, reduced motion, and layout/paint exceptions.
3. Add a red/green adversarial test that proves the requested decision passes and
   a nearby drift attempt fails.
4. Update `src/ui/design_registry.py` with owner and rationale when the census,
   master set, geometry, evidence mode, or approval changes.
5. Regenerate mirrors and run the merge-facing check.

Approvals are exact and typed. Permanent exemptions are limited to nonvisual policy infrastructure.
Quarantine is temporary, owned, and shrink-only; debt may shrink but not grow.

Edit this directive only when an agent's decision rule changes. Do not add history, diaries,
generated tables, surface counts, or product specifications.

## 7. Verification

Run the composed guard for every visual change:

```powershell
python scripts/check_design_sync.py
```

For conformance work, inspect the deterministic receipt directly:

```powershell
python execution/verify_design_conformance.py --check --route-canaries
python -m pytest tests/test_design_registry.py tests/test_design_conformance_canonical.py tests/test_design_sync.py tests/test_ui_controls.py -q
```

Renderer changes require golden diff review; generated React changes require the design-system check/build.
