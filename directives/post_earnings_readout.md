# Post-Earnings Readout

## Goal

Produce the canonical, persisted investor readout for one selected reported
quarter. The readout separates reported facts, management explanation, thesis
inference, and next-quarter verification conditions.

## Target source

- Canonical quarter identity: the active selected transcript's `period_end` and
  `fiscal_period_type`.
- Default selection: latest reported selected quarter as of the run date.
  An explicit request can supply the paired `period_end` and
  `fiscal_period_type` arguments. The CLI exposes these as `--period-end` and
  `--fiscal-period-type`. Future period ends and future call dates are ineligible.
  An exact portfolio batch validates all requested active names and selected
  quarters before any generation. Missing scope fails closed. An explicit
  evaluation request uses the single-ticker implementation; the cockpit API
  retains its latest-quarter selection.
- Primary evidence: speaker-attributed, time-coded `transcript_segments` for
  that selected transcript.
- Supporting evidence: the quarter's `earnings_surprises`, tracked KPI deltas,
  thesis/bear/IR anchors, owner watch items, queued earnings notes, call-tone
  alert, and current valuation stance.
- Unavailable blocks are disclosed by omission or an explicit evidence-gap
  statement. Never infer missing figures.

## Authorized tools

- Deterministic SQLite readers in `src/earnings_readout.py`.
- The governed `llm_client.call_llm` entry point with purpose
  `post_earnings_readout`.
- `llm_artifact_store.upsert` for durable publication.

## Output schema

Markdown with exactly five sections:

1. Quarter in one line
2. What changed versus expectations
3. What management said
4. Thesis update
5. What to verify next quarter

The durable row is a ticker-scope `llm_artifacts` artifact with purpose
`post_earnings_readout`, `fiscal_period=<selected transcript period_end>`, and
the selected transcript document ID in `source_doc_ids`.

## Analytical procedure within the five sections

Follow the project skill's
[research method](../src/advisor/skills/earnings-summary-investing/references/research-method.md).
Use its working evidence record; do not add sections or change persistence schema.

| Existing section | Procedure |
| --- | --- |
| Quarter in one line | Select the most material result and investment implication; keep evidence limits visible |
| What changed versus expectations | Compare with the saved dated pre-call bar where available; distinguish guidance, sourced consensus and owner expectations; disclose an absent baseline |
| What management said | Inspect the complete material Q&A exchange, including multipart questions and later responses; report unresolved components neutrally; compare language only with matched speaker/topic/context passages |
| Thesis update | Connect outcomes and material moat mechanism evidence/counter-evidence to accepted pillars and model assumptions; preserve approved breaks; mark unsupported mechanisms unresolved |
| What to verify next quarter | Give a public source, disclosure/event, observable condition and thesis/model consequence for each material check; avoid private-data homework |

For historical quarters, distinguish known-at-call evidence from subsequent
information and current thesis/valuation context. A missing pre-call brief cannot
be recreated as a dated prior expectation. A truncated or unknown Q&A package
cannot support an avoidance or dropped-topic claim. Text supports language
observations, not vocal tone. Do not invent numeric sentiment or fair value.

The runtime loads the same method through `src/research/method_contract.py`.
Its manifest binds the exact assembled prompt and method identity before cache
lookup. The ordinary lane selects a verified exact-event pre-call artifact from
history, with original input/output commitments and parent ID. Missing evidence
stays unavailable. Date-only call metadata excludes same-day briefs. An event
association does not resolve the pre-brief fiscal target.

The runtime includes every stored transcript segment and retains its identity,
role, sequence, timestamps and content commitment. Full stored population does
not prove complete acquisition, extraction or Q&A; these states remain unknown.
Oversize transcript or complete prompt input fails before synthesis. Current
mutable context is labeled separately from call evidence. No historical cutoff
is enforced in this ordinary lane. A separate retained-trace lane must preserve
its exclusive verified scope; it cannot inherit these unrestricted inputs.

The output must retain the five nonempty level-two headings in order. Malformed
output is rejected after the existing single call; no extra format repair or
artifact write occurs. Inspect the installed route and selected artifact. Do not
treat an old cached readout as regenerated under these instructions.
Unavailable evidence must be explicit when it limits a material conclusion;
omission alone does not establish that a question was answered.

## Refresh cadence and scope

- Automatic: daily morning stage 1d, active `portfolio` names only.
- Evaluation: explicit owner request only; never included in a scheduled query.
- The deterministic peek template is always free and does not call an LLM.

## Identity and repeat safety

- **Logical Idempotency Key:** `{ticker}:post_earnings_readout:{period_end}`.
- **Content Identity:** `input_sha256` for canonicalized inputs and the persisted
  artifact digest for generated output.
- **Observation Version:** `{period_end}:{prompt_version}:{input_sha256}`; changed
  inputs or prompt contract supersede the current version within that quarter.
- **Attempt Identity:** unique governed LLM-call/run identity for logs, cost, and
  retry attribution.

The unique-current artifact index maintains one current row per ticker and quarter;
a new period end creates a distinct logical artifact.

## Rate-limit and spend budget

One governed synthesis call per changed quarter input. The purpose has a
$5/month skip-mode budget. A cache hit or deterministic-template render burns
zero tokens. A cap hit returns `budget_skipped` and leaves the template usable.

## Failure policy

- Transient transport or persistence failure: defer that portfolio ticker and
  retry on the next morning run; an explicit request returns a bounded error.
- Hard stop (missing governed transport/auth/config): fail the stage loudly.
- Schema/quarter identity failure: do not call the model and do not persist.
- Empty model output or failed artifact write never counts as generated.
