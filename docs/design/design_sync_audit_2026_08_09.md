# Design sync audit — 2026-08-09

## Decision

The accepted product language is the warm-obsidian Work OS shown by the Harvey
sidebar reference and the current Company Research / Copilot prototypes:

- one persistent 240px left sidebar, collapsed to a 72px icon rail on narrow screens;
- three explicit navigation layers: Portfolio Intelligence, Research Engine,
  Operations & Governance;
- Inter UI type with four content sizes only: 20 / 15 / 13 / 11, plus the
  platform-required 16px mobile form-control floor;
- 16 / 12 / 8 core spacing rhythm, neutral borders, accent only for selection and action;
- one 16px, 1.75-stroke icon family; no mixed Unicode/emoji utility glyphs;
- one operating band per view, with subordinate destinations nested under their parent;
- kit controls for every button, chip, status, checkbox, approval, and dismissal action.

`mockups/harvey_sidebar_flow.html` remains the visual acceptance reference.
`mockups/company_research_experience.html` and
`mockups/copilot_conversation_prototype.html` refine the information architecture
and interaction model without creating a second visual language.

## Surface inventory and current state

| Surface | Render path | Sync state after this pass | Remaining issue |
|---|---|---:|---|
| Production Work OS (`/`) | `src/pipeline/work_os_shell.py` via `execution/comments_server.py` | Aligned | Browser geometry remains release evidence rather than a hosted screenshot test |
| Command center shell | `src/pipeline/command_center_shell.py` | Aligned | Panel-local CSS still carries legacy layout literals |
| Shared Python kit | `src/ui/tokens.py`, `src/ui/controls.py` | Aligned | Existing non-navigation primitives still contain legacy raw geometry |
| React prototype kit | `design-system/src/` | Aligned for tokens/chrome | Component core is still hand-ported rather than generated |
| Company Research prototype | `mockups/company_research_experience.html` | Reference aligned | Active prototype, not production wiring |
| Copilot prototype | `mockups/copilot_conversation_prototype.html` | Reference aligned | Active prototype, not production wiring |
| Workspace research report | `src/report/renderers/workspace_styles.py` and peers | Aligned | Remaining raw geometry is bounded by the shrink-only baseline |
| Earnings calendar artifact | `execution/build_earnings_calendar.py` | Aligned | None known |
| Workspace golden artifacts | `tests/golden/workspace/` | Aligned | Regenerated and rechecked after the report migration |

## What caused the regressions

1. **The written and executable type contracts disagreed.** The directive had
   moved to four visible sizes while `TYPE_SCALE` and `test_ui_tokens.py` still
   required ten distinct sizes. A renderer could satisfy CI and still violate
   the accepted hierarchy.
2. **The UI guard discovered surfaces through `var(--` under `src/` only.** A
   standalone HTML generator could use raw hex, another font stack, freehand
   badges, and off-scale type without entering the registry. The earnings
   calendar was exactly that failure mode.
3. **The guard deliberately ignored layout geometry.** Before this sync, color, font size,
   radius, font family, aliases, and one status-badge pattern were enforced;
   width, height, padding, margin, gap, blur, shadow, and transform were not.
   That conflicts with the current token-purity rule.
4. **The report was a second design system.** `workspace_styles.py` defined its
   own density tokens, editorial type ramp, control classes, and component
   treatments. Its guard grandfathered 217 raw spacing declarations and exempts
   the report type ramp entirely.
5. **React control parity was manual.** Tokens had a generator, but shared
   controls relied on comments and hand-porting. There was no single repository
   command that checked both token drift and application-chrome parity.
6. **Navigation architecture was encoded as a horizontal topbar.** Ask was
   hidden, System was a detached glyph, and the live shell never adopted the
   reference sidebar even though the mockups and prose had.

## Changes in this sync

- Collapsed the canonical type system to 20 / 15 / 13 / 11 while retaining the
  old semantic names as aliases, preventing renderer breakage.
- Added canonical sidebar, nav-item, icon, and icon-button primitives to the
  Python kit and React CSS mirror.
- Added one escaped SVG icon renderer with a consistent 16px / 1.75-stroke
  family, including check and close accents for future approve/dismiss actions.
- Migrated the production command center to the three-layer left sidebar while
  preserving panel ids, hashes, lazy endpoints, palette labels, and state hooks.
- Made Research Copilot, Decision Audit Log, and Execution Queue visible
  destinations under their accepted navigation layers.
- Migrated the earnings calendar from raw light-theme CSS and freehand badges
  to the shared dark palette, kit controls, ticker labels, sidebar, and icons.
- Replaced the calendar's unstructured cross-stage dictionaries with typed
  records while its renderer was open.
- Migrated the workspace report from its horizontal tab bar and independent
  type/spacing ramp to the canonical three-layer sidebar, four visible type
  roles, canonical icons, and shared spacing ladder. The sidebar collapses to
  a named 72px icon rail; mobile destinations retain 44px targets.
- Removed 208 raw workspace-report geometry declarations by tokenizing every
  literal margin, padding, and gap declaration; remaining width/height/layout
  debt is bounded rather than silently exempted.
- Added `scripts/check_design_sync.py` as the repository-level sync check. It
  verifies generated tokens, exact Python/React chrome declarations, and every
  standalone HTML document emitted from `execution/` for kit composition plus
  forbidden raw color, type, radius, font-family, and freehand-badge escapes.
  It also enforces an exact shrink-only raw-geometry baseline across 63 CSS
  surfaces: growth fails, and cleanup fails until the baseline is lowered.
- Wired that check into hosted CI and made `design-system/**` a code path, so
  React-only drift cannot bypass the frontend jobs.

## Guard status

- Generated token drift: green.
- Python UI controls and surface guard: green; one expected full-conformance xfail remains.
- Command center shell suite: green.
- Reference mockup guards: green.
- React token/off-system scan: green.
- Remaining quarantine: `workspace_charts` radius; `workspace_comments` radius;
  `workspace_styles` radius; `ui/cite_marks` color and radius.
- Raw-geometry baseline: 1,353 declarations across 63 existing surfaces;
  `workspace_styles.py` fell from 341 to 133 and has zero raw font-size,
  margin, padding, or gap declarations.

## Adversarial closeout

An independent UX judge initially failed the sync for invalid primary-tab ARIA,
unnamed collapsed calendar links, a shallow selector-presence parity check, the
15px mobile input regression, and missing report-golden verification. The
closeout repairs are now part of the guarded contract:

- the primary sidebar is native navigation; only nested panel switchers use the
  complete tablist/tab/tabpanel pattern;
- collapsed navigation links retain explicit accessible names and 44px mobile
  targets, while mobile inputs retain the 16px platform floor;
- Python/React chrome declarations are compared exactly and the design-sync
  command runs in hosted CI, including on React-only changes;
- calendar tables scroll inside labeled keyboard-focusable regions rather than
  widening the phone viewport;
- workspace report goldens were regenerated after reviewing the shared token and
  control-kit diff, then passed in ordinary non-regeneration mode;
- desktop and 375px checks against the actual `/` route confirmed the Work OS
  sidebar at 240px/72px, all eight destinations and SVG icons, 44px mobile
  targets, a computed 16px Copilot input, the 20/15/13/11 type scale, the
  warm-obsidian ground, and no page overflow;
- the React TypeScript check, token scan, and package build now run clean in this
  checkout.

The final independent pass also caught a clean-runner defect in the new hosted
Design Sync job: it initially ran the guard before installing the Pydantic-bearing
runtime dependency graph. The job now installs the hashed `requirements.lock`,
and a regression test inspects that job block for both the install and guard.
After that repair, the judge scored the current diff **9.3 / 10** with no critical,
high, or medium blockers. Its release condition is explicit: merge only after the
newly pushed commit's hosted CI, including Design Sync, is green. Residual
quarantines and the 1,353-declaration geometry baseline limit a higher score but
are explicit and fail-closed against growth.

## Required follow-on slices

1. **Geometry burn-down.** Reduce the now fail-closed per-surface baseline in
   bounded slices; new surfaces start at zero and existing ceilings cannot grow.
2. **Component parity generation.** Replace the hand-maintained React controls
   port with a deterministic generated core plus a clearly separated React-only
   extension block.
3. **Prototype-to-production wiring.** Connect Company Research card actions and
   Copilot drawers to governed backend capabilities without copying prototype CSS.
4. **Close the quarantine.** Remove the remaining report/citation radius and
   color exemptions, then flip the strict full-conformance xfail green.
