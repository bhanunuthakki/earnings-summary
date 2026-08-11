# L1 LLM evals and orchestration audit

**Verdict: BLOCK**

Audited 2026-08-11 against source commit `c6ebbe471343a080be5479ad4c26334fe8630b04`. The checked-in hardening state records one open LLM blocker, but the fresh source audit finds **five high-severity blockers** and two medium findings. The central runtime is otherwise mature: purpose routing, schema repair, budgets, attempt telemetry, provider provenance, eval registration, and fail-closed sweep receipts exist and their focused provider-free tests pass.

## Scope and evidence boundary

- Read-only source audit of `src/`, `execution/`, `evals/`, relevant tests and directives, `.harden/state.json`, and `docs/hardening/L1/activation_receipt_2026_08_10.json`.
- The target worktree had no `data/portfolio.db`. The live database was not opened or mutated. Live-state evidence was limited to the existing durable receipt file at `data/model_eval_runs/latest.json` in the canonical runtime, sanitized CLI/auth diagnostics, and a metadata-only Gemini model-catalog request.
- Production-call AST census, excluding the central transport implementations: 134 calls across 92 files; 123 statically resolved calls and 11 reviewed dynamic wrappers; 104 distinct literal purposes; zero unregistered literal purposes; zero uncovered literal purposes; zero `call_llm_structured*` calls missing the `schema` keyword.
- `python execution/run_llm_evals.py --coverage-gate`: **PASS**, 125/125 registered purposes with an executable eval mode and zero uncovered.
- Focused provider-free governance suite: **173 passed** (`test_llm_governance`, schema guard, eval receipt/coverage, ledger, transport, resolver, Gemini, Codex primary routing, model sweep, weekly orchestration).
- Direct provider scan found no feature-level `anthropic`, `openai`, `google.genai`, or `claude_agent_sdk` imports outside the governed backend modules.

## Blocking findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| high | `docs/hardening/L1/activation_receipt_2026_08_10.json:30` | The external Gemini credential blocker is **current, not stale**. The canonical external env has a Gemini key configured and `LLM_FALLBACK_DISABLED=1`; a sanitized metadata-only `client.models.list()` probe on 2026-08-11 was rejected with HTTP 400 and the repository's `API_KEY_INVALID` classifier. The latest durable sweep receipt remains `status=alert`: 12 attempted, 4 graded, 6 insufficient, 2 errors. Therefore there is still no current provider-backed `passed` receipt with a graded cohort and zero provider errors. | Rotate the external Gemini key; keep emergency fallback disabled until a direct metadata probe succeeds; then run the complete provider-backed sweep outside the protected 03:00-05:00 PT window. Closure requires `graded > 0`, `errors = 0`, `status = passed`, and retention of the new receipt on the deployed SHA. |
| high | `src/llm_client.py:1233` | `transcript_metadata` is a raw-string classifier, not schema-validated output. `src/parser.py:130-141` accepts any underscore-bearing string, splits it, and uses the pieces to rename transcript files. A malformed but underscore-containing response can therefore drive a filesystem action. This violates the L1 rule that programmatic LLM output validate or raise. | Return a Pydantic envelope such as `{ticker, fiscal_quarter, fiscal_year, identified}` through `call_llm_structured`; use a closed quarter enum and bounded year/ticker validators; repair once; rename only from the validated object. |
| high | `execution/pressure_test_thesis.py:327` | The pressure-test CLI requests JSON through `call_llm`, strips fences, and checks only that `json.loads` returns a dict (`:333-340`). The untyped dict is then written to an audit artifact and used by the diligence append path. Required fields, enum values, numeric/list shapes, and extra fields are not validated, and there is no schema-repair attempt. | Define a closed Pydantic response model, route through `call_llm_structured`, validate the conviction enum and all fields, repair once, and persist only the validated model dump. |
| high | `execution/dcf_opus_assumptions.py:222` | The DCF assumption generator uses an ad hoc first/last-brace JSON parser, then writes the resulting unvalidated dict to `data/dcf_assumptions/<ticker>.json` (`:231-250`). Model-proposed valuation model, growth, margin, tax, capex, terminal method, and exit multiple values can reach canonical DCF inputs without Pydantic validation or one repair. | Define a strict DCF-assumptions model with numeric bounds, enums, segment-key validation, and cross-field rules; call `call_llm_structured`; repair once; persist only validated output. |
| high | `execution/extract_risk_factors.py:212` | `risk_factor_diff` classifies output with the substring `"no material rewording"`. The same function returns `None` after any provider exception (`:215-217`), so downstream state cannot distinguish a model-declared no-change result from transport failure. This violates both the no-keyword-classification and no-silent-failure L1 rules. | Use a schema with `classification: material_change | no_material_change`, `summary`, and evidence fields. Validate/repair centrally. Return or persist an explicit typed failure/deferred outcome on provider errors rather than `None`. |

No critical findings were identified.

## Non-blocking findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| medium | `src/llm/cli.py:117` | The repo-local Claude subprocess sanitizer removes ambient `ANTHROPIC_BASE_URL` but does not reject or remove `ANTHROPIC_API_KEY`; the setup error even suggests setting that key (`:1048-1052`). Every Claude row is nevertheless stamped `transport=subscription_cli` and `auth_class=subscription` (`:1347-1353`). The machine's canonical `claude_cli.py` rejects this key because Claude Code can silently switch to metered API billing. Current state is safe—no Anthropic key was present and `claude auth status --json` reported a Max subscription—but a future env change can cause surprise billing and false provenance. | Reuse the canonical Claude membership wrapper or implement the same fail-closed `ANTHROPIC_API_KEY` rejection before spawn; update the setup hint; test process and canonical project-env loading. |
| medium | `src/llm/cli.py:851` | Dynamic `lens:*` purposes intentionally bypass the central purpose picker: models are stored on `Lens` objects and passed explicitly. This distributes model pins across lens files and makes explicit-model precedence bypass DB model overrides. The synthetic `lens:*` capture audit prevents this from becoming an eval-coverage blocker, but it is not the required central cheapest-sufficient picker. | Move lens model roles to a single reviewed registry/resolver keyed by lens purpose family, preserve `lens:*` eval coverage, and allow governed DB overrides without call-site model IDs. |

## Control assessment

### Model selection and transport

- `src/llm/resolver.py:98-169` centralizes explicit override, DB pin, purpose pin, provider-family dispatch, and capability checks.
- `src/llm/cli.py:153-180` defines the default, fast, and Codex role mapping; `:860-905` resolves DB overrides before code pins and warns on unknown purposes.
- Normal routing is Codex membership first with Claude membership fallback. OpenRouter automatic fallback is fail-closed and currently has an empty purpose allowlist. Gemini production promotion remains eval-gated; the disabled invalid Gemini fallback does not silently affect normal successful membership calls.
- The two deliberate exceptions are recorded above: distributed lens pins and the Claude membership-key guard.

### Structured output

- `src/llm/structured.py:135-208` parses the expected JSON shape, applies `TypeAdapter.validate_python`, applies optional domain guardrails, and retries once with feedback. `:209-282` fails loudly or escalates only under the governed policy.
- The static schema ratchet passed for all 92 `call_llm_structured*` production calls.
- That ratchet only sees calls already using the structured wrapper; it does not protect direct `call_llm` calls whose prose is later parsed or classified. The four high source findings above are those bypasses.

### Cost, latency, failure, and fallback telemetry

- `src/llm_call_ledger.py:33-86` carries model, purpose, ticker/scope/run attribution, input/cache/output tokens, elapsed time, cost estimate, retry/attempt counts, outcome, failure class, and fallback provenance.
- Claude uses `-p --output-format json` and records the CLI envelope's token counts and `total_cost_usd` (`src/llm/cli.py:1293-1353`). Codex records measured token usage and a public-API-equivalent cost estimate (`src/llm/codex_backend.py:152-278`). Gemini records SDK usage and public-list cost per attempt (`src/llm/gemini_backend.py:395-524`).
- `src/llm/transport.py` classifies quota, rate, overload, auth, config, timeout, malformed, and unknown failures; retry budgets and the cross-process quota breaker prevent repeated doomed calls. Setup and budget failures fail loudly; scheduled quota starvation defers and retries later.
- Ledger writes are intentionally best-effort so telemetry failure does not discard an expensive successful answer. That trade-off remains observable through warning logs and was not treated as an L1 blocker.

### Evals and prompt management

- `src/evals/coverage.py:121-173` separates graded, insufficient, and provider/judge-error outcomes; a receipt passes only with at least one graded verdict and zero errors.
- `execution/run_weekly_model_eval.py` persists a receipt and stops before switches or prompt experiments when the receipt is not passed.
- Registered coverage is complete (125/125), with golden, rubric/audit, capture-audit, outcome, or meta modes. Schema validation is not counted as quality coverage.
- Prompts have a version registry and opt-in capture; cached artifacts use prompt version plus input hashes. The application is explicitly single-tenant, so tenant-scoped cache-key requirements are not applicable at L1.

## External-practice check

| Area | Code/config seam | Decision | Evidence | Applicability and uncertainty |
|---|---|---|---|---|
| Claude automation | `src/llm/cli.py:1293-1325` | Run non-interactively with print mode and machine-readable JSON output. | Anthropic, [CLI reference](https://code.claude.com/docs/en/cli-usage), updated 2026-08-07, accessed 2026-08-11. The page documents `claude -p`, `--output-format json`, `claude auth login`, and `claude auth status`. | Applies directly to installed Claude Code 2.1.128. The repo's `-p --output-format json` invocation is supported. Remaining uncertainty: the audit did not spend quota on a live print-mode completion; compatibility is supported by source tests and current CLI auth, not a fresh completion envelope. |
| Claude setup/auth | Local CLI/auth state and `src/llm/cli.py:1031-1054` | Use an authenticated Claude subscription for membership billing and diagnose auth explicitly. | Anthropic, [Advanced setup / getting started](https://code.claude.com/docs/en/getting-started), updated 2026-07-28, accessed 2026-08-11; sanitized local check: Claude Code 2.1.128, `auth status` exit 0, logged in, `claude.ai`, Max subscription. | The installed CLI and subscription auth are currently healthy. This does not prove future subprocesses cannot inherit an API key; the medium billing-provenance finding remains applicable. |

## Closure requirements

1. Fix and test the four unvalidated/classifier output paths.
2. Rotate and validate the Gemini key, then produce a complete provider-backed sweep receipt with `graded > 0`, `errors = 0`, and `status = passed` on the deployed SHA.
3. Re-run the coverage gate and focused governance suite.
4. Re-audit the exact diff, then reconcile `.harden/state.json` and the activation receipt. Do not clear L1 from provider-free tests or registration coverage alone.
