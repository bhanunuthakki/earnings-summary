# Directive: model-downgrade eval loop

**Goal:** make per-purpose model selection a *measured, standing* thing instead of
a hand-tuned `LLM_MODELS` pin. The loop continuously asks, for each purpose: **is
a cheaper model at parity with the incumbent?** When one is, the purpose switches
down and saves cost. "Cheaper" spans cheaper Claude tiers AND Gemini — Gemini is
just one kind of cheaper candidate.

Decision (2026-06-11, user): test cheaper models per purpose; *when a task reaches
parity with a cheaper model, the switch happens*. Sampling = **scheduled batch**
(no hook in the live `call_llm` path).

This builds directly on the eval-gated Gemini work (`directives/gemini_backend.md`)
and reuses its brand-blind pairwise judge unchanged.

---

## 1. The core idea (and why the judge already supports it)

The pairwise judge (`src/llm/backend_judge.py`) is **brand-blind**: it scores
"Response A" vs "Response B" and never learns which model wrote which. So
"incumbent model vs cheaper candidate" is the *same* comparison as "Claude vs
Gemini" — just relabeled. The engine puts the incumbent response in slot A and the
candidate in slot B; `winner == "claude"` means incumbent won, `== "gemini"` means
the candidate won. PROMOTE_CANDIDATE becomes **SWITCH_DOWN**.

So the only genuinely new primitives are:
- **`src/llm/model_ladder.py`** — a cost-ranked registry (the definition of
  "cheaper"). Marginal $/MTok, output-weighted; subscription Gemini is $0 (cheapest)
  but `rate_limited` is surfaced. `cheaper_candidates(incumbent)`.
- **`src/llm/model_eval.py`** — `run_model` (family→backend dispatch, budget
  bypassed, `scope="model_eval"`), `judge_case` (the slot mapping), `decide_switch`
  (the conservative recommendation).
- **`execution/eval_model_downgrade.py`** — driver: for a purpose, reuse the
  incumbent's captured responses, run each cheaper candidate, dual-judge, decide.

## 2. Conservative switch rule (`decide_switch`)

A downgrade ships only when the cheaper model clearly holds up — the cost of a bad
downgrade (worse production output, silently) outweighs the saving:

- `KEEP_INCUMBENT` if ANY judge has the incumbent winning a majority.
- `SWITCH_DOWN` only if EVERY judge has the candidate at parity-or-better on
  ≥ `parity_threshold` (default 0.8) of cases AND cross-judge agreement ≥ 0.6.
- `HOLD` for mixed / judges-disagree.
- `INSUFFICIENT_DATA` below `min_n` (default 4).

A candidate that *fails* a case (errors/timeout) counts as an incumbent win — a
model that can't reliably produce the output isn't switch-worthy. Gemini's
`rate_limited` flag is surfaced so a high-volume purpose isn't switched onto a
quota it would blow.

## 3. Sampling: scheduled batch (no live hook)

The loop does NOT probabilistically tap live `call_llm`. Instead a scheduled job:
1. picks a random sample of (purpose, ticker) pairs from the tracked universe;
2. harvests their real prompts — `LLM_CAPTURE_DIR=… build_artifacts --enable-llm
   --force-refresh` (capture is the env-gated sink from #421; `--force-refresh`
   forces a real LLM call so the prompt is captured, not cache-hit);
3. runs `eval_model_downgrade` per sampled purpose against the captured prompts;
4. appends verdicts to the standing ledger (below).

Run the eval phase with capture OFF so eval traffic never re-enters the corpus.

## 4. Phased PR plan

| PR | Scope | Status |
|---|---|---|
| **1** | Foundation: `model_ladder` + `model_eval` (engine, reuses the judge) + `eval_model_downgrade` CLI + tests + this directive. Advisory verdicts to `data/model_eval/`. | **this PR** |
| **2** | Standing verdict ledger + scheduled cron: a `model_eval_verdicts` table (rolling per-(purpose, candidate) tally across runs), a `run_model_eval_sweep` job that samples purposes/tickers, harvests, evals, and accumulates; a weekly Scheduled-Task rung. | planned |
| **3** | Auto-switch with guardrails: a `model_pin_overrides` table (purpose→model, reversible) that `_model_for` consults BEFORE the code pin; the loop writes an override when a candidate clears a *high* bar (large min-n, both-judge agreement, margin), emits an alert, and **auto-demotes** (clears the override) if a later sweep regresses. Dashboard surface + manual override/lock. | planned |
| **4** | Coverage: golden anti-regression cases per downgraded purpose (so a switched-down purpose is re-checked on prompt changes), and a cost-savings rollup ("$X/mo saved by N downgrades"). | planned |

**Why advisory-first (PR1) before auto (PR3):** you can't auto-switch without the
measurement, and the measurement engine is unambiguous. Auto-switch flips
production model selection from a sampling loop — it needs the override table
(reversible, data not code), a high confidence bar, alerting, and auto-demote
before it earns that authority. PR1 produces the evidence; a human still flips
the pin (or PR3 lets the loop do it under guardrails).

## 5. Early findings (real, from the harvest that motivated this)

On `bear_case` (incumbent Sonnet, n=4 NU/MELI/NOW/BN), **Gemini-Pro lost 4/4**
under both judges (REJECT) — analytical reasoning needs the incumbent. `viewspec_compile`
(Haiku) showed Gemini at parity (PROMOTE_CANDIDATE, n=2). The point of the loop is
exactly this purpose-dependence: it finds the cheapest model that holds *per
purpose*, rather than one global choice.
