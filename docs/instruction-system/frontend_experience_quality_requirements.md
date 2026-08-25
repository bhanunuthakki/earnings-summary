# Frontend Experience Quality — Reconciled Requirements

**Status:** reconciled after independent review (`REVISE`)
**Date:** 2026-08-25
**Scope:** shared agent instructions, frontend generation workflows, hardening rubrics,
and the Earnings Summary design contract
**Non-canonical:** this document is an audit intermediate. It does not authorize edits
to canonical procedures or project directives.

## 0. Instruction layers reviewed

The implementation must reconcile the assembled context, not edit one file in isolation:

1. runtime-owned system/developer collaboration behavior;
2. shared always-loaded `AGENTS.md` routing, authority, and completion rules;
3. shared frontend workflows and references (`code-change`, `code-change.FRONTEND`,
   `scaffold-design-system`, `mockup-review`);
4. shared review and hardening criteria (`harden`, `ux-design`, `frontend-web`, and
   `design-conformance-audit`);
5. shared user-discovery and handoff workflows (`grill-me`, `context-engineering`, and
   `explain-change`) where they affect visible behavior or owner understanding;
6. generated runtime adapters and synchronization tests;
7. the Earnings Summary project `AGENTS.md`;
8. the project’s living UI/output contracts and their authority links, especially
   `design_language`, `navigation_ia`, `report_comments_and_chat`, provenance surfacing,
   interaction rules, operations/governance surfaces, and the semantic conformance audit;
9. executable tokens, controls, registries, scanners, tests, and browser canaries that
   provide higher-fidelity enforcement than prose.

Historical build plans may supply evidence but must not silently regain authority over a
living contract.

## 1. Problem

Agents can produce a frontend that is token-compliant, component-compliant, accessible,
and technically correct while the whole surface remains visually incoherent. The coding
loop is also detached from the rendered interface: agents can reason from source, pass
structural tests, and defer browser inspection until the end rather than letting observed
page behavior shape the implementation. User-reported failure patterns include unnecessary
cards and nested boxes, decorative left rails,
accent color without semantics, competing layout grammars, too many text treatments,
gratuitous indentation, redundant title/subtitle stacks, and bullets used as decoration.

The current instruction system governs the ingredients more strongly than the finished
composition. It can prevent raw colors or an unregistered button, but it does not reliably
prevent an agent from over-composing approved ingredients. This requirements audit does
not claim that existing rendered canaries are absent or that every reported example passes
them. Earnings Summary already has blocking two-width route canaries; the missing layer is
iterative UX intent and semantic judgment beyond their deterministic anatomy checks.

## 2. Audit finding and cause

This is a governance gap, not evidence of a single large owner mistake.

| Layer | What it currently catches | Why the reported failures pass |
|---|---|---|
| Top-level `AGENTS.md` | Routing, safety, evidence, and workflow ownership | Intentionally contains no route to a dedicated frontend-experience procedure |
| `code-change.FRONTEND.md` | Accessibility, mobile mechanics, states, and a final instruction to render and inspect | Browser use appears as a finishing check, not a required observe–reason–change–reobserve loop; no first-principles UX brief or specified visual evidence |
| `harden.md` | Dispatches `ux-design` and `frontend-web` experts | UX is advisory at L1 and blocking only at L3; personal projects are normally capped at L1 |
| `agents/ux-design.md` | Consistent scale, tokens, accessibility, flows, and usability | “Consistent” is one broad line; there is no reduction pass, anti-pattern rubric, or whole-page composition test |
| `agents/frontend-web.md` | Correctness, performance, responsive behavior, and rendered proof | Explicitly delegates design language and IA elsewhere |
| `scaffold-design-system.md` | Tokens, accessible primitives, state components, and locale helpers | Produces valid ingredients but does not constrain how many roles, boxes, accents, or layout patterns are composed on a page |
| `mockup-review.md` | Existing-kit reuse, density preservation, interaction truth, and implementation handoff | Does not require a deletion/flattening pass or justification for each visual distinction |
| Earnings Summary deterministic guards | Registered tokens, controls, recipes, geometry, known structural drift, and two-width rendered route canaries | Semantic intent, necessity, and iterative implementation reasoning remain incomplete oracles even when anatomy is valid |
| `design-conformance-audit.md` | Five semantic drift classes, including decorative accent and type misuse | Narrow, advisory, periodic/opt-in, and not a mandatory gate for every frontend build; omits broad box/nesting/layout/text-structure clutter |

The history identifies a specific migration seam. Commit `b1e8196` (“Simplify
cross-runtime context architecture,” 2026-07-29) removed the always-loaded `Frontend
Correctness` section from shared `AGENTS.md` and created the much smaller
`code-change.FRONTEND.md` reference. That was a reasonable progressive-disclosure move,
but it preserved mostly technical mechanics and reduced rendered inspection to one final
bullet. `scaffold-design-system.md`, `ux-design.md`, and `frontend-web.md` still refer to
“Frontend Correctness” as if it lives in `AGENTS.md`. The migration therefore left stale
ownership language and no semantic composition contract. The compositional-restraint gap
predates the migration; the migration made the routing and enforcement gap easier to miss.

The project layer also has authority ambiguity that must be cleaned up before adding more
rules:

- `directives/README.md` lists `navigation_ia.md` as a living UI contract, while the file
  itself says it is a draft that does not govern until owner approval.
- `design_language.md` points to `interaction_paradigm_2026_06.md` as an interaction-law
  owner, while that file is marked complete and record-only.
- `report_comments_and_chat.md` is listed as a living UI/output contract and marked
  shipped/superseded, but retains proposed architecture, pre-coding questions, estimates,
  and an implementation cadence that can be mistaken for current instructions.
- The shared design-conformance procedure says explicit-request-only and no scheduled
  mutation, while the project runbook describes a registered monthly job and a gitignored
  report write.

Adding another visual checklist without resolving these source-of-truth conflicts would
increase the instruction mess rather than fix it.

The system evolved toward objective, testable controls: accessibility, tokens, component
reuse, responsive behavior, and exact registries. Those controls are valuable, but they
substituted visual consistency for visual restraint. Consistency answers “are these parts
from the same kit?” Restraint answers “should these parts exist at all?”

## 3. Desired outcome

Any agent-created or agent-modified frontend should default to a quiet, coherent composition:

- one primary type family, with narrowly defined semantic exceptions;
- a small, stable set of text roles;
- one dominant layout grammar per surface;
- ordinary document flow before containers;
- whitespace, alignment, and proximity before borders or backgrounds;
- accent reserved for interaction, selection, focus, or unread state, with status using
  its separate semantic role;
- indentation and bullets only when content structure requires them;
- the minimum number of boxes, headings, labels, and decorative elements needed for
  comprehension and operation.

Technical conformance is necessary but insufficient. Completion requires both mechanical
proof and a browser-grounded page-level semantic review. Browser inspection is part of
implementation, not a downstream audit substitute for first-principles UX reasoning.

## 4. Instruction architecture requirements

### IR-1 — One canonical reusable frontend-quality procedure

Create `procedures/frontend-quality.md` as the single shared owner of first-principles UX
reasoning, compositional restraint, rendered implementation loops, and visual handoff
evidence. Other workflows must route to it rather than restating variants of the rules.
It must apply to greenfield generation, feature implementation, redesign, mockups, and
audits across web and other rendered interfaces.

### IR-2 — Small top-level routing cue

Keep the always-loaded instruction compact. Add one progressive-procedure row routing
frontend creation, modification, or review to `frontend-quality`, composed with
`code-change`, `mockup-review`, or the applicable scaffold as needed. Do not place the
rubric itself in `AGENTS.md`, and do not duplicate the existing general code-change route.

### IR-3 — Generative workflows apply restraint before styling

`scaffold-design-system`, `code-change.FRONTEND`, and `mockup-review` must defer their
shared UX/composition rules to `frontend-quality` and sequence work as:

1. establish content and interaction hierarchy;
2. compose the fewest existing roles and primitives needed;
3. perform a deletion and flattening pass;
4. add or extend styling only for a remaining semantic need;
5. verify the entire surface at supported widths.

### IR-4 — Material frontend implementation uses an observe–reason–change loop

For this contract, a **material frontend change** adds, removes, rearranges, or materially
restyles a visible region, control, hierarchy, navigation path, state, or responsive
behavior. A typo-only copy correction, nonvisual handler change, or mechanically regenerated
mirror with no rendered delta is not material.

Before a material change to an existing runnable interface, the agent must render and
inspect the affected surface. It must identify the primary user, primary job, dominant action,
information hierarchy, current friction, and the smallest visual change that addresses the
requested outcome. After each material composition change, it must re-render, interact with
the affected states, and revise from observed behavior rather than source inspection alone.
The loop covers affected project-supported viewports and applicable states; it does not
force unrelated state matrices or screenshots onto a small change.

A frontend change is not visually complete merely because tests and the build pass. If the
available environment cannot render the interface, the agent must report the verification
gap and must not claim that appearance, hierarchy, responsiveness, or interaction quality
has been verified.

### IR-5 — Hardening owns a semantic visual-quality gate

The `ux-design` rubric must explicitly evaluate whole-page hierarchy, restraint, and
composition. For projects with a user-facing frontend, change the L1 `ux-design` cell from
advisory to blocking. At L1, only a broken/obscured primary task or repeated/systemic visual
incoherence is high; local polish remains medium/low and does not block. L3 retains its
stricter accessibility and usability bar. This uses the existing hardening gate semantics
without inventing a second composition-only expert.

### IR-6 — Implementation and UX ownership remain distinct

`ux-design` owns whether the hierarchy and composition are coherent. `frontend-web` owns
whether that design is implemented correctly and rendered faithfully. Findings must have
one owner and may cross-reference the other rubric without duplication.

### IR-7 — Project contracts may narrow, not duplicate

Project design directives may define exact roles, recipes, and sanctioned exceptions.
They must inherit the generic visual-restraint contract and add only product-specific
constraints. Earnings Summary should retain its registered-master architecture while
adding the missing reduction and whole-page composition rules.

Compositional restraint does not own product navigation, doorway destinations, overlay
behavior, mutating-control semantics, provenance meaning, or operational truth. Route those
concerns to their active project owners and require an operations-governance disposition
when the change affects an operation or operator action.

### IR-8 — Reconcile user-helping behavior without adding ceremony

Frontend instructions must begin from the user’s task, observed workflow, and requested
outcome. They must preserve the shared autonomy rule: inspect available evidence and make
reasonable reversible decisions without forcing a requirements interview for every UI
change. Ask the user only when an unresolved product choice would materially change the
result. Use a mockup or prototype when recognition is more informative than prose, and do
not treat prototype approval as production authorization.

### IR-9 — Resolve stale and contradictory authority pointers

Every active frontend instruction must have one truthful status and owner. Living contracts
must not point to draft, superseded, or record-only documents as current authority. Historical
material must either be moved to a clearly historical location or reduced to a short lineage
link from the live contract. Cross-runtime references to “Frontend Correctness in AGENTS.md”
must be replaced with the actual canonical owner.

For the design-conformance audit specifically, the project runbook owns cadence and output.
The shared/project adapter may run when explicitly requested or when executing that already
registered project schedule; it must not create or change a schedule on its own. The runbook
must define whether a report is written and where, eliminating the current write/no-write
contradiction.

### IR-10 — Completion communicates user-visible proof

Frontend handoff must lead with the user-visible outcome, the task/flow exercised in the
browser, widths and states inspected, deterministic checks run, and any unverified visual or
interaction behavior. File lists and green tests alone do not communicate that the interface
helps the user.

## 5. Visual decision requirements

### VR-0 — First-principles UX brief

Before selecting layout or styling, record a concise implementation hypothesis:

- who is using the surface and what they are trying to accomplish;
- the one primary task or decision the surface should make easiest;
- the minimum information required for that task;
- the intended reading and interaction order;
- which information and actions are primary, supporting, or progressively disclosed;
- the current observable friction and the expected user-visible improvement.

Do not begin by choosing cards, grids, accents, or component variants. Those are possible
responses to the hierarchy, not the hierarchy itself.

The brief should be derived from the user request, current rendered workflow, product data,
and existing contracts. It is not a mandatory questionnaire. A material unresolved choice
is surfaced to the user; ordinary design judgment remains the agent’s responsibility.

### VR-1 — Typography economy

- Use one primary font family throughout a surface.
- Permit mono or another face only for a named semantic role, never as decoration.
- Use only the registered text roles needed by the hierarchy; do not invent another role.
- Reserve the largest role for one dominant page or region heading.
- Do not combine size, weight, italics, case, color, and indentation when one treatment
  communicates hierarchy. Required status redundancy and accessibility cues are not
  decorative hierarchy treatments.
- A subtitle must add information rather than restate its title.

### VR-2 — Container economy

- Start from unboxed flow.
- A box, panel, card, well, rail, divider, background, or shadow must communicate a named
  semantic, state, interaction, or ownership boundary.
- Prefer whitespace, alignment, and proximity when they communicate the same grouping.
- Nested boxed regions require a distinct named boundary at every level. Otherwise flatten.
- If removing a container does not reduce comprehension or operability, remove it.

### VR-3 — Layout consistency

- Use one dominant layout grammar per surface or registered surface family.
- Equivalent sections use the same composition recipe.
- Do not introduce layout variety solely to make adjacent sections look different.
- Responsive changes preserve the information hierarchy rather than inventing a second
  visual language.

### VR-4 — Semantic accents only

- Accent color communicates interaction, selection, focus, or unread state.
- Status color communicates status only and retains a non-color cue.
- Decorative colored left borders and arbitrary accent rails are prohibited.
- Gradients, ornamental icons, oversized numerals, tinted panels, and floating decorative
  shapes require a concrete product rationale and an approved recipe.

### VR-5 — Structural formatting only

- Indentation represents a genuine parent-child or list relationship.
- Bullets represent a genuine list of parallel items.
- Ordinary prose, labels, metrics, and isolated facts must not be converted into bullets
  or indented blocks merely to create texture.

### VR-6 — Required reduction pass

Before completion, the agent must inspect the whole surface and:

1. remove non-semantic decoration;
2. flatten redundant containers;
3. normalize equivalent text and controls;
4. remove redundant titles, subtitles, helper text, badges, dividers, and icons;
5. reject locally attractive components that introduce another page-level grammar.

When uncertain between two treatments, choose the plainer one unless the richer treatment
has a named semantic or interaction purpose.

## 6. Audit and evidence requirements

### ER-1 — Dual proof

Every material frontend change requires:

- deterministic proof from the project’s design, accessibility, and frontend checks; and
- rendered page-level evidence at affected project-supported viewports.

The rendered evidence must come from the real application route or the closest faithful
local preview, not isolated markup when production integration exists.

### ER-2 — Browser-grounded implementation evidence

For a material change to a runnable existing web interface, the agent must use available
browser tooling to:

1. capture the relevant baseline before editing;
2. exercise the primary task and affected interactions;
3. inspect affected loading, empty, error, populated, focus, and overflow states when applicable;
4. inspect the affected project-supported viewports;
5. check visible hierarchy, clipping, density, interaction feedback, console errors, and
   failed network requests;
6. capture the final state and compare it with the stated UX hypothesis.

This is an iterative implementation loop, not a single screenshot taken after coding.
For non-web interfaces, use the closest available renderer, simulator, or device-equivalent
evidence and apply the same loop.

Record the compact evidence in the normal handoff or Pull Request: route/surface, primary
task exercised, viewports, states, deterministic checks, browser/renderer used, result, and
any gap. If rendering is unavailable, distinguish “functional checks passed” from “visual
verification unavailable”; do not block unrelated nonvisual proof or claim visual success.

### ER-3 — Semantic review questions

The reviewer must be able to answer:

- What purpose does each visible container serve?
- Why is each indentation present?
- Why does each text role differ from the surrounding role?
- What state or action does each accent communicate?
- Why is a section visually different from equivalent sections?
- What becomes harder to understand or operate if the element is removed?

An unclear answer is a finding, not automatic permission to keep the treatment.

### ER-4 — Severity

- A single local excess with no usability impact may be low or medium.
- A repeated pattern, competing page grammars, obscured hierarchy, or system-wide
  over-composition is high.
- On the blocking UX rung, unresolved high findings block.

### ER-5 — Adversarial examples

The shared contract and evaluation fixtures must include paired examples where both variants use valid
tokens/components but only the restrained composition passes. At minimum cover:

- unboxed grouping versus nested cards;
- semantic status rail versus decorative left border;
- one hierarchy treatment versus combined size/weight/color/indentation;
- genuine list versus decorative bullets;
- informative subtitle versus title repetition.

## 7. Acceptance criteria

### Deterministic instruction and synchronization checks

1. The top-level procedure table, `code-change.FRONTEND`, `mockup-review`,
   `scaffold-design-system`, `harden`, `ux-design`, and `frontend-web` point to the one
   canonical `frontend-quality` owner without duplicating its rubric.
2. The `ux-design` hardening rubric contains explicit, verdict-bearing whole-page
   composition criteria.
3. The L1 hardening matrix makes applicable `ux-design` blocking, with severity rules that
   keep local polish non-blocking and systemic incoherence high.
4. The design-system scaffold and mockup workflow both require a reduction pass.
5. `frontend-quality.md` defines materiality, a proportional baseline/iteration loop, and
   the compact evidence record or declared verification gap.
6. Earnings Summary’s directive adopts project-specific restraint rules without copying
   the full shared contract.
7. The semantic conformance audit covers typography economy, container nesting, layout
   grammar, structural formatting, and redundant hierarchy in addition to its existing
   drift classes.
8. Canonical procedure edits are followed by
   `python snippets/sync_agent_stubs.py --check`, synchronization, and a second clean check;
   generated Claude, Codex, Gemini, and other managed artifacts remain consistent without
   treating hand-authored runtime wrappers as generated prose targets.
9. No active shared instruction refers to a nonexistent always-loaded “Frontend
    Correctness” owner.
10. No living project UI contract delegates current authority to a draft, superseded, or
    record-only document without an explicit active subset.
11. Frontend handoff states the user task exercised and observable outcome, not only code
    changes and test commands.

### Behavioral and semantic evaluations

12. Paired rendered fixtures cover the five ER-5 cases with declared expected findings;
    the restrained and cluttered variants may both use valid tokens and components.
13. Blinded, versioned task-trajectory evaluations assess whether agents state the UX
    hypothesis, use applicable rendered evidence, perform a reduction pass, and disclose
    unavailable verification.
14. Evaluation results do not claim universal invocation coverage. Such a claim requires an
    independent task population frame under the judging policy; until then, coverage is
    explicitly unproven.

## 8. Non-goals

- Mandating a single aesthetic, brand, framework, or component library.
- Prohibiting all cards, borders, subtitles, mono text, bullets, or visual accents.
- Replacing accessibility, responsive, performance, or deterministic conformance checks.
- Encoding arbitrary pixel values or rigid universal nesting counts in the global rules.
- Automatically rewriting existing production UIs as part of the instruction change.

## 9. Independent review disposition

The requested independent sub-agent returned **REVISE**. This version accepts its findings:

- selected the existing L1 gate mechanism instead of leaving enforcement open;
- replaced an unprovable routing claim with deterministic reference/sync checks and
  separate behavioral evaluations;
- made browser requirements proportional and defined the evidence record;
- qualified causal claims against existing route canaries;
- aligned typography and accent/status language with the project contract;
- preserved project behavior ownership and operations-governance routing;
- specified the canonical sync flow and the non-claim on invocation coverage;
- resolved the design-audit cadence owner in the requirements.

The exact fixture implementations remain delivery work, not an unresolved product decision.
