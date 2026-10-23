# Core MVP: plan to bring the three deliverables into the live application

Status: approved 2026-10-20 for the PR split and PR 1. The earlier work and PR 1 are now split into the stacked commits listed under §6; nothing is deployed or published. Each later phase lists the owner approval it needs.

## 1. Outcome

The three MVP reports must stop being task-script outputs. They become normal products of the live pipelines:

- NU Q2 2026 pre-earnings brief (PRE).
- NU Q2 2026 post-earnings review (POST).
- MELI integrated brief, DCF and editable workbook.

One shared contract governs all three. It defines what a report may say (bound figures, verbatim quotes, the owner's thesis), how it reads (argument first, evidence behind), and how it is checked and retained (claim manifest, readiness receipt).

The live pipelines are:

- The scheduled PRE brief (`src/earnings_brief.py`, morning stage 1c).
- The scheduled POST readout (`src/earnings_readout.py`, stage 1d).
- The requested memo / decision-brief workflow (`src/research/decision_brief_workflow.py`).
- The cockpit report reader (`/reports/<T>`) and the DCF workbook route (`/dcf/<T>`).

## 2. What exists now

| Item | Location | State |
| --- | --- | --- |
| Shared binding contract: excerpts, bound figures, replayed arithmetic, pinned owner thesis, model values in analysis, readable source text | `src/research/memo_claim_support.py`, `src/research/decision_brief.py`, `src/research/nu_earnings_evidence.py`, `tests/test_memo_analysis_binding.py` | Uncommitted; `make check-fast` green (1033 passed) |
| Earlier data-plane work: retained SEC submissions, scoped evidence, historical timing, financial readers, MELI model/workbook bindings, Alembic 0059/0060 | About 90 files, about 21k lines in this worktree, plus 3 local commits | Uncommitted, mixed scopes; branch is 1 commit behind `origin/main` |
| Report authors | `.tmp/hero-core-verification/nu-earnings-author/nu_report_author.py`, `.tmp/.../meli-integrated-author/meli_report_author.py`, supplement builders | Task-only; NU/MELI-specific; not product code |
| Artifacts | NU PRE r6 `attempt-5`, NU POST `attempt-2`, MELI `attempt-7` + `MELI.xlsx` (R6) | Offline-checked; native Windows verification and independent review not run |
| Thesis edits | `micro_thesis/holdings/NU.json` (CAR floor), `MELI.json` (FCF basis) | Gitignored private files on the Mac; the Windows copy state is unknown |

## 3. Target design: one contract, three routes

### 3.1 Shared report core (new `src/research/report_contract/`)

Promote the issuer-agnostic parts of the two task authors. NU- and MELI-specific code becomes data in evidence packs, not code.

- **Draft schema:** paragraphs with `{n}` citations and named derived figures (today `nu_report_draft.v4`, renamed to a neutral id). A model or an analyst produces this JSON; it never produces HTML.
- **Evidence units**, behind one interface:
  - a sealed source node, cited by verbatim excerpt;
  - an admitted financial fact (shared financial reader);
  - a verified model value (model commitment);
  - a calculation;
  - the pinned owner thesis.
- **Renderer:** produces the shared report body.
  - Inline cite-mark chips route to `/source/version/<document_version_id>`, following `operations_governance_surface.md`, and do not link to raw external URLs.
  - Formulas and model values go in the baseline section; full source text goes in a collapsed Evidence section.
  - Entities are decoded only for display.
- **Claim mapping and checks:** the context-review builder, the offline binding replay, and the remap and recommit of report-evidence topics (the MELI path already does this).
- **Owner-thesis pinning:** read `micro_thesis/holdings/<T>.json`, then pin its hash and revision. A historical report pins the thesis version at the cutoff and discloses any later clarification.

### 3.2 Route mapping: content changes, section structure does not

`research-method.md` owns the section structure, and the MVP adapts to it.

**PRE**, the four method sections:

| Method section | MVP content |
| --- | --- |
| What this quarter must show | Bottom line plus the 2–4 thesis-linked items, each with its dated bar |
| Numbers to check the moment they print | Bound expectation bar and thesis thresholds |
| What to listen for on the call | Management explanations that are owed or absent (for example, NU capital) |
| Thesis pressure points | Bear case, what would change the view, valuation implication |

Baseline and Evidence stay as back matter.

**POST**, the five method sections:

| Method section | MVP content |
| --- | --- |
| Quarter in one line | Bottom line |
| What changed versus expectations | PRE scorecard against the same dated PRE bar |
| What management said | Explanations, including any absence |
| Thesis update | Rules met, rules that fired, owner housekeeping |
| What to verify next quarter | The next-quarter list |

**Memo / decision brief:** keep the six registered sections and the ten MELI topics. The MELI layout already fits.

### 3.3 Pipeline changes

1. **PRE (`earnings_brief.py`):**
   - Build the evidence pack from the existing context: thesis anchors, tier-1 KPIs, watch items, last call, filings and the transcript, all through the shared resolver.
   - Ask for the structured draft, bind it, render it, and persist both the `llm_artifacts` row and a report artifact with its context review.
   - `grounding_status` changes from `partial` to an explicit binding disposition. The deterministic assembly stays as the fallback when binding fails.
2. **POST (`earnings_readout.py`):**
   - Load the persisted PRE for the same earnings event as the dated bar.
   - Generalize `NuPreBriefCommitment` into an issuer-neutral PRE commitment.
   - If no PRE exists, say so, as the method requires.
3. **Memo / MELI (`decision_brief_workflow.py`, `prepare_decision_brief.py`, `verify_decision_brief.py`):**
   - Add the structured authoring step with model values in analysis.
   - The workbook is produced through `execution/build_meli_platform_dcf.py` and promoted to `dcf/<T>.xlsx`. The report links to `/dcf/<T>`, not to a relative file. The quote input stays owner-entered (Dashboard B33).
4. **Cockpit:**
   - Render the new body inside the workspace shell at `/reports/<T>`.
   - Register the citation and evidence-appendix recipe through the design registry. Use no local CSS: the standalone scroll fix is a wrapper-only workaround.
   - Gather browser evidence on the affected tasks at the supported widths.

### 3.4 Progressive diligence is unchanged

| Tier | Use | Gate |
| --- | --- | --- |
| 0 | Deterministic assembly (existing fallback) | Existing |
| 1 | Bounded LLM draft with binding (default PRE/POST for portfolio names and opted-in evaluation names) | Binding + claim population + section population |
| 2 | Deeper memo with supplements and a model run (requested) | Tier 1 + model commitment + ten-topic evidence |
| 3 | Decision-grade | Native database verification + readiness receipt + independent review |

`decision_grade` and `joint_mvp_status` stay false until their gates pass. Missing evidence appears as a visible "Gap:" line and never as a negative finding. Depth still scales with decision importance, and the binding gate applies at every tier.

## 4. Updates to skills, procedures and rules

- **`research-method.md`:** add short report-composition rules from the MVP feedback:
  - argument first;
  - each number once, with its "so what" and management's explanation (or its absence);
  - link findings to the owner's thesis;
  - 2–4 tracked items;
  - evidence behind the argument;
  - no whole-source quotation in the body.

  Bump the method to `public-evidence@3` through `method_contract.py` (this changes prompt cache identity). Restamp only this file in `reviewed-sources.json`; the 39 existing drifts are out of scope.
- **SKILL.md, `routes.md`, `analysis-paths.md`:** point the PRE, POST and memo routes at the shared contract and the authoring CLI.
- **`directives/llm_calls.md`:** in Implementation, name the binding layer as the deterministic release check. Keep semantic review separate, and record the remaining bypass inventory.
- **`directives/post_earnings_readout.md`:** add the structured draft and the PRE-bar loading. Document the PRE route in the same runbook or in the module docstring; there is no PRE runbook today.
- **`directives/design_language.md`, through its registry path:** add the report citation and evidence-appendix recipe, with goldens.
- **`operations_governance_surface.md`:** record the morning-pipeline behavior change and the new authoring CLI disposition.
- **`DEFINITIONS.md`:** add evidence unit, bound figure and pinned owner thesis.
- **`reconstruction_manifest.json`:** add the new modules and prompts.
- **LLM purposes:** `pre_earnings_brief`, `post_earnings_readout` and `research_narrate` stay. Bump the prompt versions, add eval cases (good draft, unbound number, invented quote, missing PRE bar), and run `execution/run_llm_evals.py`.
- **Quota:** re-measure token use under `llm_quota_scheduling.md`. The windows only change if cost changes materially.

## 5. Rules to prune or retire

| Rule | Where | Action |
| --- | --- | --- |
| "Inferences contain no numerical tokens" | Task authors `nu_pre_author.py` (PRE/POST prompts), `meli_author.py` prompt | Retire. Keep the old scripts only as reproduction records for sealed r4/r2/r3; do not commit them as product code |
| "Every reviewed quote group appears in full in the body" | `nu_pre_author.py` validation | Retire. Replace it with an "uncited reviewed groups" disclosure in the receipt |
| "Analyst inference:" text prefix as the only inference marker | `memo_reader_blocks` in `decision_brief.py` | Keep for replay of historical artifacts; new renders use `data-voice`. Deprecate after superseded artifacts leave the retention window |
| Markdown-only PRE/POST output with `grounding_status: partial` | `earnings_brief.py`, `earnings_readout.py` | Replace with the structured draft and binding; keep markdown only for the deterministic fallback |
| Raw-URL citations in report bodies | Task renderers | Replace with `/source/version/` routing in the shared renderer |
| Stale or ambiguous thesis rules | NU: standalone net-adds rule versus the thesis's compound condition. MELI: thesis WACC 11% versus model 14%. Both: stale tier-1 `current` values | Owner decisions (NU net adds, MELI WACC). Refresh values through the quarterly refresh procedure, not by hand |
| Superseded planning docs | `docs/quality/hero-mvp-*.md` (execution plan, report scope, owner decisions, architecture review, source qualification) | Consolidate into one decision record; mark superseded rules (for example, the deferred MELI native joint gate) instead of committing them as current |
| Superseded artifacts | NU PRE r4, POST r2, MELI r3 on the qualification host | Mark superseded; keep them under the retention policy; do not delete |

No canonical directive currently requires the evidence-dump behavior. Those rules lived in the task scripts.

## 6. Sequencing (small PRs; migrations kept separate)

| Step | Content | Gate |
| --- | --- | --- |
| PR 0 (series) | Rebase onto `origin/main`. Split the earlier uncommitted data-plane work by DAG stage: migrations 0059/0060 alone, then SEC submission import, scoped evidence, MELI model/workbook | Per PR: `make check-fast`, `make check` before push, golden comparison where touched |
| PR 1 | Shared binding contract and its tests (§2, row 1) | `make check-fast`; existing memo/NU/MELI suites |
| PR 2 | `report_contract` core, renderer with cockpit citation routing, design-registry recipe, goldens | `check_design_sync.py`, workspace golden comparison, browser evidence in `/reports/<T>` |
| PR 3 | PRE pipeline adoption | Purpose evals, prompt version bump, morning-pipeline dry run |
| PR 4 | POST pipeline adoption with PRE commitment | Purpose evals, knowledge-order tests |
| PR 5 | Memo/MELI authoring step, `/dcf/<T>` link, workbook promotion | Model/workbook parity tests (44 calcs, 34 assumptions, 3 scenarios unchanged) |
| PR 6 | Method, skill and directive updates; the pruning in §5; reconstruction manifest | Method/reference check; reviewed-source restamp for changed files only |
| Rollout | Deploy to the canonical Windows host. Verify the three artifacts natively, or regenerate them through the pipelines. Persist readiness receipts. Promote `dcf/MELI.xlsx`. Run independent review | Windows native assessment; owner acceptance |

**As split (2026-10-20):** the PR 0 series became six stacked PRs on `origin/main`, after the three earlier commits:

1. analysis-scope witnesses and the report evidence profile;
2. scoped research universes and the resolution clock (migration `0059`);
3. native PDF transcripts and document-processing evidence;
4. retained SEC submission intake;
5. bulk lexical coverage (migration `0060`);
6. the MELI workbook.

PR 1 (binding contract) stands alone on `main`. The memo-verification PR builds on it, and a docs PR closes the stack.

**NU historical PRE/POST:** both were written after the POST snapshot, so the native PRE-before-POST order cannot hold.

**Recommendation:** publish them as labeled historical reconstructions (not sealed) and let the live pipeline prove the sealed order on NU's and MELI's next real earnings.

## 7. Owner decisions and approvals needed

1. Approve the PR split and opening PRs (push and PR creation are not yet authorized).
2. NU POST/PRE: accept the "historical reconstruction" label instead of a re-seal.
3. NU thesis: align the net-adds tracker rule with the thesis's compound condition.
4. MELI thesis: one discount rate (11% thesis file versus 14% model).
5. Windows: deploy authority, the thesis-file copies on the host, and running native verification.
6. LLM spend: eval runs, and the first live PRE/POST generations under the new prompts.
7. Independent review: a new review slot for the final artifacts. All four approved slots are used; do not start one silently.

## 8. Risks

- **Binding failures:** structured drafts can fail binding and fall back often. Measure the rate in evals before switching the scheduled lanes. The contract allows no unchanged automatic retry.
- **Token cost:** larger evidence packs raise per-call tokens; re-measure before the quota windows.
- **Old artifacts:** they must still verify, so the legacy inference marker stays.
- **Private thesis files:** they diverge between machines unless one host owns them; Windows is canonical.
- **PR 0 size:** about 21k uncommitted lines from earlier work need careful review and splitting before any of this lands.
