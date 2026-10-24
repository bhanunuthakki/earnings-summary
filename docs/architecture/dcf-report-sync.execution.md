# DCF report synchronization execution addendum

Status: partial local implementation, BLOCK for acceptance. This addendum
supplements the [DCF and report synchronization PRD](dcf-report-sync.md). The
accepted PRD bytes remain unchanged.

## Binding and review verdict

- Plan commit: `43cbb81f8ea8c80a31d04a27ce25271c39ae72cb`.
- PRD SHA256: `ff5745f4c6ed026e7782feb3c2532d82b8ffdf90bbe319897cbc13f5e5fad6f3`.
- Fresh main: `4920fb1c09cf61be5b9e3cbf39b2e6397345f7c0`.
- Fresh main supersedes the historical application baseline
  `901b7bdf7488ff5b2843bd03c5901b35e72349c4`. Historical baseline evidence
  remains in the [delivery evidence](dcf-report-sync.delivery.md).
- Independent Sol PRD review: PASS 92/100 with no unresolved scope blockers,
  bound to the exact plan hash and fresh main. Scores: 19/20 outcome, 19/20
  reuse, 23/25 consistency, 18/20 financial/cache/approval, 13/15 acceptance.
- The human authorized a task-specific advisory exception. It establishes no
  formal calibration or qualification claim.
- This PASS is a PRD review, not implementation acceptance. Partial
  implementation remains BLOCK for acceptance. Open blockers include actual
  NU/MELI adapters, pinned candidate generation, unified current-report
  selectors, fenced atomic publication, durable dedup/recovery, complete
  validation and UI. No new product scope is needed.

## Verified main changes to preserve

- `SourceReadContext` moved from `dcf/input_evidence.py` to
  `provenance/source_read_context.py`, with explicit `content_roots` and
  `ocr_state_root`, and `for_product_state_root`. Database paths grant no source
  authority.
- Source context now passes through DCF verification/readiness, report
  builder/financials, decision brief, report contract, claim support, earnings
  report contract and MELI preview.
- `execution/build_artifacts.py` adds `--source-state-root`.
- Pinned OCR PDF source routes now verify version/hash/byte bounds and render
  verified page bytes. Preserve those changes and their unavailable states.
- Candidate work must retain admission, byte authority, physical PDF parity and
  native reconstruction. Add absent/wrong source-root and pinned-PDF
  source/review tests. This verified inventory is sufficient; no further git
  diff was run for this addendum.

## Routing and execution controls

The latest human routing instruction supersedes the earlier GLM preference. Use
the exact `deepseek/deepseek-v4.1-flash-1017` via OpenRouter, on the cheapest
reliable eligible endpoint. Root only orchestrates; execution is delegated. Sol
judges PRD/plans and implementation strictly above 85 with no blockers before CI
and merge. Do not change application model/provider routing or add a product
dependency. This is dev-agent tooling only. Use the existing OpenCode 1.19.4
native client.

Floor: 99% reported uptime over 30 minutes. Refresh uptime before every dispatch.
Pin eligible exact provider slugs; no fallback outside the eligible set. Current
verified quotes in USD per million tokens:

- `open-inference/fp4`: $0.02 input, $1 output, $0.015 cached input.
- `sail-research/fp4/us`: $0.101 input, $0.15 output, $0.006735 cached input.

Both are FP4 and reported 100% 30-minute uptime. OpenInference is the read-heavy
document candidate; Sail is the lower-cost code-output candidate. Choose by
expected total request cost with measured input/output/cache/retry usage.
Quantized endpoint quality remains unverified until bounded local work and
deterministic/Sol review. Never claim broad qualification or an SLA.
`DeepInfra/fp8/flex` reported 86.67% uptime and is ineligible. Source:
https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints

This task uses OpenInference as a bounded mechanical trial. Future code tranches
can use Sail if the cost estimate supports it.

Initial per-tranche caps: at most 14 tool steps, 12 minutes and $0.75; smaller
tasks use 8 steps. Increase only for a concrete integration need. Keep one writer
per owned path, serial tests and no overlapping mutable resources. Add no new
schedules. Scope remains the entire PRD, not a partial workflow. No live model
changes, source collection or financial report publication as smoke tests.

## Ordered tranches

1. Preserve the partial diff and integrate main/source authority plus an explicit
   owner and lock-order map.
2. Implement exact save and real family adapters.
3. Implement pinned isolated generation and cache dependencies.
4. Implement exact verification/publication/all selectors/recovery and
   historical clocks.
5. Implement shared actions and evidence interactions, plus explicit deeper
   section requests.
6. Run exhaustive deterministic/browser/native checks. Obtain Sol implementation
   review above 85 before CI/merge, then normal delivery and canonical Windows
   deployment.
