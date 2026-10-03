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
  evaluation request uses the single-ticker implementation. The cockpit API
  accepts the same paired selectors and keeps latest-quarter selection when
  both are absent.
- Primary evidence: speaker-attributed, time-coded `transcript_segments` for
  that selected transcript.
- Supporting evidence: the quarter's `earnings_surprises`, tracked KPI deltas,
  thesis/bear/IR anchors, owner watch items, queued earnings notes, call-tone
  alert, and current valuation stance.
- Unavailable blocks are disclosed by omission or an explicit evidence-gap
  statement. Never infer missing figures.

### Explicit saved-evidence mode

The single-ticker core and `POST /api/earnings-readout/generate` also accept
`retrieval_trace_id` and an offset-aware `knowledge_cutoff`. Both are required,
along with the paired fiscal selectors. The selected transcript identifies the
readout quarter. It does not turn annual source facts into quarterly facts.

This mode reads one saved trace through the strict retrieval and Research
Snapshot owners. Its verified AnalysisScope must name the requested period end,
issuer and exact knowledge cutoff. The reader checks the retained source bytes
against their immutable commitments under the server-configured evidence root.
It does not acquire sources or substitute current transcript text, valuation,
notes or thesis blocks for missing historical evidence.

Each fact retains its actual start, end, period kind, source fiscal label, unit,
currency, accounting basis, consolidation scope, dimensions and metric definition.
The existing projection owner resolves checkpoint and inherited delta entries.
Annual or prior-period context does not establish quarterly comparability.
Consensus, cutoff-qualified thesis rules and requested-period comparison coverage
remain explicit gaps when this saved context does not establish them. The readout
is partial; a bounded trace does not prove complete financial or thesis coverage.

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
the selected transcript document ID in `source_doc_ids` for legacy transcript
mode. Saved-evidence mode retains version IDs and source locators in
`content_json.retained_evidence`; it does not invent legacy document IDs.

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

Saved-evidence content identity also includes the trace seal, snapshot member
seal, immutable snapshot request hash, full AnalysisScope, projection seal,
actual fact dimensions and verified evidence items. A changed saved identity
invalidates reuse even when the rendered prompt text is unchanged. Verification
and raw-byte checks run before a cache hit is accepted. Existing saved artifacts
remain readable without a refresh or hidden acquisition.

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
