# Frontend Experience Quality — Implementation Plan

**Status:** implemented 2026-08-25; retained as the delivery record
Implementation was separately authorized after requirements review.
**Date:** 2026-08-25
**Requirements:** `frontend_experience_quality_requirements.md`

## Outcome

Make user-centered reasoning, compositional restraint, and rendered-interface iteration a
normal part of building any frontend. Keep technical implementation checks, project design
systems, and product-specific behavior owners intact, but stop allowing a green build or a
token-compliant component tree to stand in for a coherent user experience.

## Preconditions

1. Obtain explicit authorization before editing Earnings Summary directives. The project
   contract treats directive edits and commits as separate approvals.
2. Preserve the current dirty worktrees. `/Applications/agent-instructions` already has
   user-owned changes in `AGENTS.md`, the generator, tests, and an untracked
   `procedures/mockup-review.md`; Earnings Summary has extensive in-progress UI changes.
   Re-read and reconcile those exact files before any write.
3. Do not combine this instruction-system work with production UI cleanup. First change the
   governing instructions and evals; audit or remediate product surfaces in separately
   approved work.

## Source disposition

### Shared instruction repository

| Source | Disposition | Intended ownership after the change |
|---|---|---|
| `AGENTS.md` | Change minimally | One routing row for frontend creation/modification/review; no visual rubric in always-loaded context |
| `procedures/frontend-quality.md` | Add | Canonical first-principles UX, restraint, rendered iteration, reduction pass, and evidence contract |
| `procedures/code-change.md` | Keep architecture; update route | General behavior-change loop; compose `frontend-quality` for visible interfaces |
| `procedures/code-change.FRONTEND.md` | Narrow and reconcile | Technical frontend correctness only; remove stale claims and point to `frontend-quality` for UX/composition |
| `procedures/scaffold-design-system.md` | Substantially simplify | Generate a neutral foundation after hierarchy is known; stop acting as a generic Next/Radix/Tailwind aesthetic recipe |
| `procedures/mockup-review.md` | Update | Browser baseline, UX hypothesis, reduction pass, and visual evidence before approval/handoff |
| `procedures/grill-me.md` | Small cross-reference | Use discovery only for material unresolved product choices; do not turn every UI change into an interview |
| `procedures/explain-change.md` | Small cross-reference | For frontend changes, explain the task exercised, rendered proof, and verification gaps |
| `procedures/harden.md` | Change gate and dispatch | `ux-design` becomes L1 blocking when applicable; UX/frontend briefs include `frontend-quality` |
| `procedures/agents/ux-design.md` | Expand judgment rubric | Own user-task clarity, whole-page hierarchy, restraint, and semantic composition |
| `procedures/agents/frontend-web.md` | Clarify implementation ownership | Own faithful implementation, browser/console/responsive proof, and technical correctness without re-grading taste |
| `procedures/design-conformance-audit.md` | Retire as a global Earnings-specific owner | Project-specific cadence and output move back to the Earnings Summary runbook; generic semantic criteria live in `frontend-quality`/`ux-design` |
| `procedures/context-engineering.md` | No prose change expected | Continue to enforce one owner, progressive disclosure, and source synchronization |
| `snippets/sync_agent_stubs.py` | Update | Generate/discover `frontend-quality`; remove or deprecate the global Earnings-specific audit adapter |
| `snippets/test_guide_generation.py` | Expand | Structural routing, identity-copy, reference, orphan, and stale-pointer checks |

Runtime-owned system/developer collaboration instructions remain out of repository scope.
Do not duplicate their personality, progress-update, autonomy, or final-answer rules. The
shared procedure should add only the missing frontend-specific user-outcome and proof contract.

### Earnings Summary project

| Source | Disposition | Intended ownership after the change |
|---|---|---|
| `AGENTS.md` | Keep compact; one truthful route if needed | UI work uses the shared frontend procedure plus the project design contract |
| `directives/README.md` | Correct authority statuses | Separate canonical/living contracts, drafts, runbooks, and record-only programs |
| `directives/design_language.md` | Add project-specific restraint section | Exact four-role typography, mono exceptions, accent/status distinction, registered recipes, and project reduction rules |
| `directives/design_conformance_audit.md` | Expand and clarify | Project cadence/output owner; semantic audit includes container economy, layout grammar, formatting, and redundant hierarchy |
| `directives/navigation_ia.md` | Demote from living authority unless separately approved | Remain draft evidence; executable navigation/code is current authority until owner sign-off |
| `directives/interaction_paradigm_2026_06.md` | Keep as record-only; extract active laws | Move the still-governing interaction subset into a concise living contract or executable registry reference |
| `directives/report_comments_and_chat.md` | Split current contract from history | Keep only current comments/Copilot behavior in the living contract; preserve superseded proposals and estimates as history |
| `directives/data_provenance.md` | Keep semantic ownership | Continue owning provenance meaning and non-color cues; do not add general visual styling rules |
| `directives/operations_governance_surface.md` | Keep; align evidence vocabulary | Continue owning operator truth and actions; use the same compact browser-evidence record for visible changes |
| Historical build/design plans | Mark evidence-only | Never use shipped or superseded plans as current frontend authority |
| `src/ui/*`, design registry, scanners, and UI tests | Keep executable authority | Mechanical conformance and exact project recipes remain deterministic owners |

## Delivery phases

### Phase 1 — Establish the shared frontend-quality owner

Add `procedures/frontend-quality.md` with:

- an outcome-first UX brief derived from the user request, rendered workflow, and product
  evidence—not a mandatory questionnaire;
- a definition of material frontend change and proportionate applicability;
- the observe–reason–change–reobserve loop for runnable existing interfaces;
- browser/renderer evidence for affected tasks, states, and supported viewports;
- typography, container, layout, accent/status, indentation, bullet, and title/subtitle
  restraint rules;
- a required deletion/flattening pass;
- explicit product-owner routing for IA, overlays, controls, provenance, and operations;
- a compact handoff/PR evidence schema;
- a stop rule: no claim of visual verification when rendering is unavailable.

Add one progressive-procedure route in shared `AGENTS.md`. Update `code-change` and its
frontend reference to compose the new procedure rather than duplicate it.

### Phase 2 — Repair generative frontend workflows

Refactor `scaffold-design-system.md` so it stops preselecting a generic LLM aesthetic:

- infer and preserve the repository’s stack before proposing Next.js, Tailwind, Radix, or
  another library;
- use framework defaults only for a genuinely greenfield project with no chosen stack;
- establish content hierarchy and one neutral page frame before tokens/components;
- keep one primary font, a small semantic role set, and restrained component variants;
- make empty/loading/error components visually neutral rather than card-like by default;
- move long framework-specific samples into optional templates/references so the workflow
  itself stays concise;
- require a rendered reduction pass before declaring the scaffold usable.

Update `mockup-review` to require an observed baseline, an explicit user-task hypothesis,
whole-page reduction, and proportional browser evidence before approval. Preserve its
existing data-truth, interaction, backend, and production-authorization boundaries.

Add narrow pointers in `grill-me` and `explain-change`: consequential UI ambiguity may use
requirements discovery, and frontend explanations include user-visible rendered proof.

### Phase 3 — Make hardening capable of catching the problem

Change the `harden.md` L1 matrix cell for applicable `ux-design` from `A` to `B`.

Revise `ux-design.md`:

- require inspection of the real rendered primary flow;
- grade user-task clarity, information hierarchy, compositional economy, consistency,
  progressive disclosure, and the reduction pass;
- classify a broken/obscured primary task or repeated/systemic over-composition as `high`;
- keep isolated polish issues `medium`/`low` at L1;
- retain the stricter L3 accessibility/usability bar.

Revise `frontend-web.md` to own implementation fidelity, affected state/viewport checks,
console/network evidence, responsiveness, and accessibility mechanics. It should cite a UX
finding rather than duplicate it.

Update hardening dispatch so the UX and frontend experts receive the shared frontend-quality
contract alongside their specialist rubrics.

### Phase 4 — Reconcile project authority before adding more rules

Normalize the UI/output section of `directives/README.md` into a small status table with one
owner per concern. Do not mass-rewrite unrelated directives.

- Keep `navigation_ia.md` explicitly non-governing until owner approval; remove it from the
  living-contract list in the meantime.
- Preserve `interaction_paradigm_2026_06.md` as history and extract only still-active laws
  into a concise living interaction contract or executable-owner pointers.
- Split the current comments/Copilot contract from superseded design proposals and estimates.
- Make `design_language.md` point only to active owners.
- Make the design-conformance runbook the single owner of its cadence and report location.
  An adapter may execute an existing schedule or an explicit request but may not create a
  schedule implicitly.

This phase is required before adding project restraint prose; otherwise the new rules would
join an already ambiguous authority graph.

### Phase 5 — Add the Earnings Summary refinement

Add a concise section to `directives/design_language.md` that narrows the shared contract:

- use only the existing four roles needed by the hierarchy;
- keep sans for prose/labels and mono only for the named financial/code/locator roles;
- preserve the project’s accent versus status distinction;
- begin with normal flow and registered page/family recipes;
- require a named semantic boundary for every nested boxed region;
- reject decorative left rails and ornamental variation;
- require the page-level reduction pass before `scripts/check_design_sync.py`;
- retain existing registered-master extension and adversarial-test requirements.

Expand `directives/design_conformance_audit.md` to review the same project-specific semantic
classes without re-reporting deterministic failures or sanctioned exceptions. Cover at least
container nesting, competing layout grammars, redundant title/subtitle stacks, decorative
formatting/bullets/indentation, and unjustified visual differentiation.

### Phase 6 — Add deterministic checks and shadow evaluations

In the shared instruction repository, add structural tests that assert:

- `frontend-quality` is present in the canonical procedure registry and generated runtime
  artifacts;
- the top-level route exists exactly once;
- frontend workflows and hardening rubrics reference the canonical owner;
- stale “Frontend Correctness lives in AGENTS.md” claims are absent;
- the Earnings-specific audit is no longer exposed as a generic global owner, if retired;
- generated artifacts are identity-consistent and reference targets exist.

Add paired semantic fixtures where both variants use valid tokens/components:

1. unboxed grouping versus nested cards;
2. semantic status rail versus decorative left border;
3. one hierarchy cue versus combined size/weight/color/indentation;
4. genuine list versus decorative bullets;
5. informative subtitle versus title repetition.

Add blinded, versioned task-trajectory evaluations for a small representative set:

- existing page material redesign;
- small visual adjustment;
- greenfield scaffold;
- nonvisual frontend behavior change;
- unrunnable preview requiring an explicit verification gap.

These begin in shadow mode. They evaluate the UX hypothesis, applicable browser evidence,
reduction pass, and truthful gap reporting. They do not establish universal invocation
coverage without an independent task population frame.

### Phase 7 — Synchronize and validate

In `/Applications/agent-instructions`:

1. run targeted structural tests for the generator and governance;
2. run `python snippets/sync_agent_stubs.py --check`;
3. synchronize with `python snippets/sync_agent_stubs.py`;
4. rerun the check and targeted tests;
5. inspect generated Claude, Codex, Gemini, and other managed artifacts for identity and
   trigger correctness.

In Earnings Summary:

1. run instruction/reference checks added for living UI authority;
2. run `python scripts/check_design_sync.py`;
3. run the focused design registry, conformance, sync, and UI-control tests;
4. exercise the design-conformance audit against the paired fixtures;
5. do not regenerate report goldens unless production renderer code changes—this plan
   changes instructions and fixtures, not the product UI.

## Recommended change sequence

Land this as small, separately reviewable changes:

1. shared `frontend-quality` procedure, routing, and structural tests;
2. shared scaffold/mockup/hardening reconciliation;
3. Earnings Summary authority/status cleanup;
4. Earnings Summary design refinement and semantic audit fixtures;
5. shadow task-trajectory evals and cross-runtime synchronization.

Do not bundle production UI remediation into these changes. After the instruction work is
green, run an explicitly authorized conformance review of current surfaces and create a
separate, evidence-backed remediation plan.

## Completion evidence

The instruction implementation is complete only when:

- every active frontend instruction has one owner and truthful status;
- ordinary frontend implementation invokes both engineering correctness and frontend
  experience quality;
- a material runnable UI change requires proportional iterative rendered evidence;
- L1 hardening can block systemic visual incoherence for personal projects;
- project-specific rules narrow the shared contract without duplicating it;
- structural tests and synchronization checks pass;
- paired clutter/restraint fixtures receive the expected semantic verdicts; and
- remaining uncertainty is limited to shadow-eval calibration and invocation coverage,
  neither of which is falsely reported as proven.
