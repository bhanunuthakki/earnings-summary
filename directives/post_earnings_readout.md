# Post-Earnings Readout

## Goal

Produce the canonical, persisted investor readout for one selected reported
quarter. The readout separates reported facts, management explanation, thesis
inference, and next-quarter verification conditions.

## Target source

- Canonical quarter identity: the active selected transcript's `period_end` and
  `fiscal_period_type`.
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

## Refresh cadence and scope

- Automatic: daily morning stage 1d, active `portfolio` names only.
- Evaluation: explicit owner request only; never included in a scheduled query.
- The deterministic peek template is always free and does not call an LLM.

## Idempotency key

`{ticker}:post_earnings_readout:{period_end}:{prompt_version}:{input_sha256}`.
The existing unique-current artifact index maintains one current row per ticker
and quarter; changed inputs supersede within that quarter, and a new period end
creates a distinct indexed artifact.

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
