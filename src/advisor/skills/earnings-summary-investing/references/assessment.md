# Monthly investing skill assessment

Use the existing Monthly prompt architecture refresh job, first Monday at 10:00
America/Los_Angeles. Keep the project portion within fifteen minutes. Do not add a
second scheduler or rerun production jobs. Read the quota registry before dispatch.

## One review with the project's improvement evidence

1. Pin a freshly fetched remote default-branch revision in an isolated checkout.
   Resolve both installed project skills and compare their source revisions with
   that release. Run the source checker. Missing release files, stale remote
   evidence or installed differences are explicit holds, not a clean result.
2. Read available authorized receipts from weekly model/prompt evaluations,
   monthly advisor memos and the calibration scorecard, plus recorded owner
   corrections. Use `directives/llm_evals.md` and `directives/model_eval_loop.md`
   for evaluation and promotion authority. Missing, thin or inaccessible inputs
   stay explicit. Read existing outputs; do not run their jobs or open a live DB
   without separately resolved host authority. Record output identity and date.
3. Assess invocation using [synthetic cases](invocation-cases.json). Give independent
   workers the actual skill catalog and prompts before disclosing expected routes.
   Record their selection before they read full skill instructions. Then inspect
   their proposed sources, analysis path, coverage and write boundaries. Use up to
   three bounded workers if quota and the remaining time allow; otherwise defer.
4. Assess analysis quality from available existing outputs: comparability,
   provenance, counter-case, decision sensitivity and supported action. Separate
   skill-routing faults from prompt/model faults, data gaps, product capability
   gaps and missing owner decisions. Put each action at its source owner. Do not
   copy business logic or model-promotion policy into skill prose.
5. Fix demonstrated instruction faults in tracked source on an isolated branch.
   Rerun each affected case, a held-out paraphrase and a negative control. Check
   links, source drift and skill validity. Record source reviews explicitly.
   Model/prompt changes use their existing held-out evaluation and promotion gates;
   a successful skill smoke test cannot promote a model or analytical prompt.

## Existing evidence readers

| Input | Existing owner and receipt | Interpretation |
| --- | --- | --- |
| Weekly evaluation | `src/evals/coverage.py` latest/immutable run receipts; `execution/run_weekly_model_eval.py` is the producer | Keep attempted, graded, insufficient and errors distinct; sweep success is not skill quality |
| Prompt outcomes | `src/llm/calibration.py` read-only summaries by purpose and prompt version | Preserve count and dates; pooled unrelated purposes do not assess this skill |
| Monthly advisor memos | `src/advisor/store.py` explicit-path memo readers | Preserve memo ID, kind, context, score status and date; prose alone is not realized quality |
| Monthly calibration | `src/calibration_coach.py`; `execution/run_calibration_scorecard.py` is the producer | Select exact YYYY-MM.json and validate fields. The generic latest loader can select the separate advice-influence sibling. Keep thin, suppressed and gated states distinct |

Do not call writer helpers to collect evidence. In particular, the advisor score
helper opens a writable connection; read existing outcome scores only through an
approved read-only projection or connection. The producer paths above identify
source ownership, not permission to run a job. Scheduler execution and receipt
freshness require their own evidence.

## Evidence and delivery

Selection/planning smoke tests do not measure actual fresh-runtime invocation or
financial quality. If an approved isolated host harness exists, record its real
invocation traces separately. Otherwise mark host invocation reliability not
measured. Separate source-check status, selection, plan safety, output quality and
host invocation results. Never turn keyword checks or absence of drift into an
analysis-quality pass. A changed instruction is not evidence of improvement until
an affected case and held-out wording have been checked.

Keep one dated private receipt under the configured research output authority.
Include released and installed revisions, input receipts, cases and exact test
method, failures, root causes, changes, validation and owner decisions. Maintain
one prioritized action list with the existing monthly review. Use private owner
feedback privately; only synthetic cases belong in public source. Changes still
need integration before they are released. Do not push, merge, deploy, change
approved thesis rules, execute trades or mutate investing state in this job.
Report meaningful findings or required action. Defer on shared quota or resource
contention. Retain partial results and the exact remaining work if the time limit is reached.
