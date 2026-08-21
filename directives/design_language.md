# Design language

**Status:** canonical agent contract for the visual system.

This directive explains how to make a visual decision. It does not duplicate the
current emitter census, approved exceptions, geometry recipes, or debt ledger.
Those inventories are executable and versioned in code.

## Operating metadata

- **Target:** every shipped component or emitter that changes what a user sees.
- **Inputs:** product intent, content hierarchy, interaction state, and an existing
  global or family master.
- **Output:** markup that selects registered recipes, or a reviewed master/registry
  extension with a regression test.
- **Refresh cadence:** only when the decision model changes; inventory changes do
  not require prose edits.
- **Idempotency key:** the changed master or registered contract plus its test.
- **Rate-limit budget:** none; verification is local and deterministic.
- **Failure policy:** reject the visual change. Never bypass, quarantine, or widen
  an approval merely to make a check green.

## 1. Authority boundary

The system is the UI equivalent of a slide master. Visual decisions are closed:
consumers select a registered recipe; they do not create a private one.

| Concern | Executable authority |
|---|---|
| Color, type, spacing, radius, border, shadow, blur, motion, rails, indents | `src/ui/tokens.py` |
| Global controls, shapes, grids, and composition recipes | `src/ui/controls.py` |
| React mirrors of global tokens and controls | `scripts/gen_design_tokens.py`, `scripts/gen_design_controls.py` |
| Family-specific layout masters and every typed approval/contract | `src/ui/design_registry.py` |
| Static, dynamic, runtime, SVG, and React conformance detection | `src/ui/conformance_scan.py` |
| Emitter census, reconciliation, debt status, and deterministic receipt | `execution/verify_design_conformance.py` |
| One merge-facing composition check | `scripts/check_design_sync.py` |

`src/ui/design_registry.py` is the live inventory oracle. This file must never
reproduce its paths, counts, digests, approvals, sanctions, quarantine, or debt.
Generated CSS/TypeScript is a mirror, never an authority.

## 2. Consumer contract

A governed surface may provide content, semantic HTML, data attributes, and
nonvisual JavaScript hooks. Its visual experience must come from:

1. the global tokens and controls;
2. one registered family master for surface-specific arrangement; and
3. an exact typed contract for any approved dynamic or runtime visual state.

Consumers must not add local visual CSS, raw inline styles, runtime style
mutation, arbitrary SVG presentation values, or open-ended `style` APIs. Preserve
behavior hooks beside kit classes; do not style the hook itself.

If an ad-hoc request cannot be expressed by an existing recipe, reject it as a
consumer edit. Either choose the nearest approved variant or extend the
appropriate master first. Reuse across families belongs in global tokens or
controls; family-only arrangement belongs in that family's registered master.

## 3. Visual grammar

These are semantic constraints. Literal values live only in executable masters.

### Typography

- Use four visible roles: display, title, body, and meta.
- Use the sans family for prose and labels.
- Use mono only for financial values, tickers, timestamps, code, and source
  locators. Mono is not a general accent face.
- Weight and case communicate hierarchy; a consumer cannot invent another size,
  line height, tracking, or font role.

### Color

- Use semantic roles: ground/surface, primary/muted text, border, accent, and
  status.
- Accent marks interaction, selection, focus, or unread state. It is not
  decoration.
- Status colors communicate status only and must retain a non-color cue.
- Raw colors, named colors, ad-hoc opacity, gradients, and consumer aliases are
  prohibited outside registered masters or generated mirrors.

### Shape, depth, and motion

- Shape comes from registered control and family recipes, including radius,
  border, shadow, blur, and transform.
- Use elevation only to explain layering or focus.
- Motion uses the registered transition vocabulary and honors reduced-motion.
- A different corner, shadow, animation, or overlay geometry is a master change,
  not a local tweak.

### Spacing, grids, and indents

- Use the spacing ladder and registered grid, rail, and indent recipes.
- Density is intentional: compact for operating controls, comfortable for
  reading, and spacious only where hierarchy needs it.
- Alignment follows content hierarchy. Do not repair a layout with one-off
  offsets, widths, gaps, or breakpoints.

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

An on-scale token does not legitimize a hand-built button, badge, chip, field,
table, drawer, card, or overlay. Compose the kit.

Accessibility is part of the recipe: semantic elements, keyboard reachability,
visible focus, labels, contrast, non-color state cues, and reduced motion are
required. Hover may enrich an action but cannot be its only doorway.

## 5. Arrangement

- Application surfaces optimize for decisions: one dominant operating band,
  compact controls, explicit state, and progressive disclosure.
- Research documents optimize for reading: clear hierarchy, restrained density,
  traceable evidence, and no application chrome masquerading as content.
- Responsive behavior, empty/loading/error states, and overlays must use
  registered recipes. They are not exceptions to the master boundary.

Product behavior is owned elsewhere. Do not copy it into this directive:

- navigation and destination hierarchy: `directives/navigation_ia.md`;
- doorway, overlay, dismissal, and interaction laws:
  `directives/interaction_paradigm_2026_06.md`;
- comments and chat: `directives/report_comments_and_chat.md`;
- provenance behavior: `directives/data_provenance.md`;
- operational controls: `directives/operations_governance_surface.md`;
- discovery and ingestion policy: `directives/news_sources_plan.md` and
  `directives/ir_events_ingestion.md`.

Those contracts may specify behavior, data, and state. They do not authorize a
new visual recipe.

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

Approvals are exact and typed. Permanent exemptions are limited to nonvisual
policy infrastructure. Quarantine is temporary, owned, and shrink-only. Debt may
shrink but may not grow. A request to broaden any of these requires explicit
review; it is never an implementation shortcut.

Edit this directive only when an agent's decision rule changes. Do not add
feature history, implementation diaries, generated tables, surface counts, or
product specifications.

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

The hosted route-canary matrix uses production-rendered Work OS seams at desktop
and narrow widths. Missing evidence or a role/geometry/focus/motion/state failure
blocks the job. Instrumentation may annotate real nodes, but cannot replace a
route with parallel HTML/CSS or satisfy it from another hidden surface.

Report-renderer changes require workspace golden regeneration and diff review;
generated React changes require the design-system check/build.

The scanner owns mechanical structure; UI tests own focus, dismissal, doorway
reachability, and goldens; review owns hierarchy. A targeted test cannot override a red composed guard or receipt.
