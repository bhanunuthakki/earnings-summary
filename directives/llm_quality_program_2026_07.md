# Directive: LLM Quality Program — logs, evals, and multi-tier orchestration

**Status: RATIFIED 2026-07-25 (owner: P0→P4 in order; observability = bespoke +
OTel naming, NO Phoenix service). Supersedes the §4.7 edit-splice mutation
layer of `meta_eval_governance.md` as the LONG-TERM direction while reusing its
harness (bandit, budget governor, multi-arm judging, breaker) unchanged. Each
phase lands as its own PR wave behind the usual eval gates.**

## 0. Why this program exists — the step-back finding

The July-2026 prompt-quality build (`meta_eval_governance.md` §4.7, PRs
#1005/#1008/#1010/#1014/#1027) produced working machinery at the WRONG
ALTITUDE in one place: prompts on this platform are inline f-strings, so the
loop had to *reverse-engineer* the instruction scaffold from captured renders.
That workaround caps coverage at whatever the harvest captured (9 of 59 costed
purposes on 2026-07-25), makes edits anchor-fragile, and couples improvement to
a corpus that exists for a different reason. The root fix is the industry
standard the workaround was imitating: **prompts as versioned templates with
separated, typed variables, logged as first-class fields on every call**.

Research grounding (2026-07-25 sweep; keep links, verify before load-bearing
use):

* Templates + variables + versioned registry as the production norm:
  Braintrust prompt-versioning guide; AWS Well-Architected GENOPS03-BP01.
* Reflective prompt evolution (GEPA, ICLR 2026 oral, arXiv:2507.19457):
  full-instruction rewrites driven by natural-language reflection on judged
  failures + a Pareto frontier of candidates; ~10%+ over MIPROv2 at 35× fewer
  rollouts than RL. Needs exactly the per-case judged metric this platform
  already has (pairwise judge + rubric facets).
* Observability: OpenTelemetry GenAI semantic conventions (`gen_ai.*`
  attributes; spans per call with stage/parent context). Adopt the NAMES in
  the bespoke ledger; no new always-on service (owner decision).
* Orchestration: per-request cascades with confidence/validation-based
  escalation (FrugalGPT / RouteLLM lineage; 40–70% production savings) as the
  complement to the existing offline downgrade loop.
* Judge hygiene: pairwise + both orderings (already done here); never same
  model family as generator and judge; recalibrate against human labels on a
  cadence (judges drift in 60–90 days).

What the July build got RIGHT and this program keeps: judged multi-arm
experiments with position-swapped dual judges; the pooled promotion bar +
auto-apply/auto-demote; the Thompson bandit + ε-floor as the explore/exploit
shell; the $40/mo measurement budget governor; the quota breaker and the
infra-vs-quality honesty taxonomy (`judge_infra`, `JUDGE_DEGRADED`,
`TRANSPORT_DEGRADED`).

## P0 — Prompt registry (templates + typed variables as first-class)

**Goal:** every LLM call site can name its template; the ledger records WHAT
template+version ran with WHICH variables, separately.

Design:

* `src/llm/prompt_registry.py`: `PromptTemplate(template_id, version, body,
  variables)` where `body` uses `{var}` slots and `variables` is a typed spec
  (name → kind: `data` | `instruction_param`). `render(vars) -> str` validates
  presence and never silently drops a slot. Registry = module-level dict,
  greppable, one entry per purpose-prompt (the `llm_calls.md` "prompts are
  greppable constants" rule, upgraded to structured).
* `call_llm(..., template: PromptTemplate | None, template_vars: ...)`
  renders internally; the ledger row gains `template_id`, `template_version`,
  `vars_sha256` (migration, nullable — unmigrated call sites keep passing raw
  prompts and log NULLs, visibly).
* `prompt_versions.py` remains the human-bump A/B dimension; the registry's
  `version` auto-derives as `sha256(body)[:12]` so an edit is ALWAYS a new
  version with zero bump discipline required. Both are logged.
* Migration path: top production spend first — `news_structuring`,
  `recent_developments`, `earnings_themes_split`, `saydo_commitment_extract`,
  `bear_case` — then opportunistic. The §4.7 scaffold-derivation path stays as
  the fallback for unmigrated purposes and is DELETED when coverage passes
  ~90% of spend (kill criterion, not a diary).

Gate: registry-rendered output byte-identical to the current inline f-string
for each migrated purpose (golden assertion per migration PR), plus the usual
suite.

## P1 — Trace context in the ledger (OTel GenAI naming, bespoke storage)

**Goal:** one drill-down from "pipeline stage X is slow/expensive/failing" to
the exact calls — the July diagnosis took ~15 hand-written SQL queries.

Design:

* `llm_calls` gains nullable `trace_id`, `span_id`, `parent_span_id`,
  `stage` (e.g. `morning_pipeline.0b.decision_conditions_extract`). Field
  SEMANTICS follow OTel GenAI conventions (`gen_ai.operation.name` ≈ purpose,
  `gen_ai.request.model` ≈ model — a mapping comment in the migration, so a
  future OTLP export is a projection, not a rewrite).
* A tiny `llm.tracectx` contextvar module: pipelines/stages open a stage
  context; `record_llm_call` reads it implicitly. No API churn at call sites.
* Panel: per-stage cost/latency/error rollup + drill-down to calls (System →
  Evals gains a "Traces" section or a sibling panel; kit components only).

Gate: the morning pipeline's next run shows ≥90% of its LLM calls carrying a
stage; a one-query reproduction of the July hour-11 burst analysis.

## P2 — Reflective mutation (GEPA-style) replacing edit-splice proposals

**Goal:** the improvement loop proposes FULL revised templates from judged
failure evidence, not 1–4 exact-substring edits from a fixed 11-strategy menu.

Design:

* Mutation operator: given (template body, K judged failure cases with judge
  rationales + facet losses), an Opus reflection call writes a diagnosis and a
  revised template body. The §4.7 strategy taxonomy becomes optional steering
  vocabulary inside the reflection prompt, not a constraint.
* Candidates are REGISTRY VERSIONS (new body → new auto-version), so arms are
  first-class citizens: `prompt_arms.edits_json` gains a sibling
  `template_body` mode (arm = whole candidate template). `apply_edits` and the
  anchor machinery remain only for the legacy fallback path.
* Per-purpose Pareto frontier (quality score vs output-token cost) persisted;
  the bandit draws which FRONTIER PARENT to mutate next (explore/exploit over
  candidates, GEPA-style) instead of which strategy to apply.
* Promotion unchanged: pooled §4.4 bar, auto-apply — but the applied artifact
  becomes "pin registry version X for purpose P" (a `prompt_pin_overrides`
  row referencing the version), byte-stable and trivially reconciled into git.

Gate: on ≥2 purposes, the reflective operator's promoted-candidate rate ≥ the
edit-splice baseline's over 4 weeks; kill the old proposal path when met.

## P3 — Cascade pilot (per-request cheap-first with escalation)

**Goal:** quota/cost headroom beyond the offline downgrade loop, without
quality loss, on the purposes where failure is CHEAPLY DETECTABLE.

Design:

* Scope: FAST-tier structured purposes only (schema-validated outputs —
  `call_llm_structured` call sites). Escalation signal #1 is free: parse /
  schema failure → retry once on the next tier up. Signal #2 (optional,
  measured before adoption): self-reported confidence field, calibrated
  against eval outcomes before it gates anything.
* `LLM_MODELS` entry format gains an optional cascade: `purpose → [haiku,
  sonnet]`; single-model entries behave exactly as today. The ledger records
  `cascade_step` so escalation rate is measurable per purpose.
* Eval-gated rollout: a purpose enters the cascade only with a golden/rubric
  gate in place; weekly eval rungs run against the CASCADE (not the top
  model) so quality regressions surface as eval failures, not surprises.

Gate: per purpose — escalation rate <30%, eval scores within noise of the
pre-cascade baseline, measured token savings reported on the panel.

## P4 — Judge hygiene

* **Cross-family judging:** `JUDGE_POOL` gains an OpenRouter non-Anthropic
  judge (provider-pinned per `openrouter_backend.md`); with Gemini dormant the
  current pool is Claude-judging-Claude, which the same-family bias literature
  says to avoid. Verdict math already supports per-judge tallies.
* **Human calibration cadence:** quarterly, ~20 sampled judged cases surfaced
  for owner labels (a small packet section, consequence-first per the action-UX
  bar); agreement < ~0.8 triggers the existing `AuditSpec.judge_model`
  escalation path. Extends `spot_check_eval_judge.py` from ad-hoc to scheduled.
* **Verbosity guard:** `decide_switch`/`decide_ab` demote a win to HOLD when
  the winner's mean output length exceeds the loser's by >2× and the margin is
  thin — the measured verbosity-bias mitigation, using the token-efficiency
  fields already recorded.

## Budget & scheduling

Rides existing envelopes: measurement stays inside the $40/mo meta ceiling;
new scheduled legs (P4 calibration packet) register in
`llm_quota_scheduling.md` per the standing rule. No new always-on services.

## Kill criteria

* P0 stalls if a migrated purpose's registry render can't be made
  byte-identical — stop and redesign rather than shipping drifted prompts.
* P2's reflective operator is killed (revert to edit-splice) if it fails its
  gate after 6 weeks of cycles.
* P3 is killed per-purpose on any sustained eval regression; cascades never
  expand past structured purposes without a new ratification.
