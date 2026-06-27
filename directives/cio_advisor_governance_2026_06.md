# Decision: the tracker's CIO advisor stays on its own stack (Track B seam 10b)

**Status:** decided 2026-06 · **Scope:** `portfolio-tracker/src/portfolio_tracker/services/cio_advisor.py`

Track B seam 10 asks us to "retire or wrap the TRACKER's ungoverned CIO advisor
… so the localhost CIO chat/brief defers to THIS repo's governed LLM stack
(`llm_call_ledger` + `llm_budget` + `evals/`) — **or document why it stays**."

It stays. This records why, and what closing the gap should look like if/when it
becomes warranted.

## What the tracker's CIO advisor is

A localhost convenience in the **separate** portfolio-tracker repo: per-session
chat (Sonnet) + monthly HTML briefs (Opus), single-shot via the out-of-repo
`claude_cli.py` subprocess wrapper. It already carries *some* governance native
to it:

- **per-purpose model pins** — `claude-sonnet-4-6` for chat, `claude-opus-4-7`
  for the brief;
- **schema'd output** — the brief requests JSON with one HTML string per of six
  locked sections, validated before the stylesheet scaffold wraps it;
- **a deterministic facts block** — `coaching.py` (rule-based thresholds) is the
  source of truth fed into the prompt, so the numbers aren't the model's.

What it lacks vs. earnings-summary's stack: the call ledger (cost/latency
logging), budget-cap enforcement, and the eval harness.

## Why NOT route it through earnings-summary's governed stack

1. **Billing regression — the decisive reason.** `claude_cli.py` bills the
   user's **Pro/Max subscription** (zero marginal cost), which is a *deliberate*
   architectural choice (`CLAUDE.md` / `GEMINI.md`: the subscription wrapper vs.
   the metered in-app `src/llm/cli.py`). earnings-summary's governed stack runs
   on the **metered API**. Delegating the tracker's CIO chat/brief to it would
   convert free subscription calls into per-token API spend — a direct cost
   regression the user explicitly architected against.

2. **Runtime coupling breaks the clean seam.** The tracker is an independently
   deployable service that earnings-summary consumes **read-only over REST**
   (`src/integrations/portfolio_tracker_client.py`; the tracker owns all
   benchmark/return/risk math). Making the tracker's CIO defer to
   earnings-summary would invert that: the tracker's chat/brief would now require
   earnings-summary to be running. Two localhost services the program keeps
   cleanly separated would become co-dependent.

3. **The governance gap is the tracker's to close, in its own stack.** The right
   fix — if/when warranted — is to port the three primitives (ledger + budget +
   evals) to the tracker's `claude_cli` call site, preserving subscription
   billing and service independence. That is a tracker-side change, out of scope
   for this earnings-summary PR, and is **not** the same as delegating across the
   repo boundary.

## Net

Seam 10's in-repo half (routing earnings-summary's ad-hoc fence-strip blocks
through `call_llm_structured`) is the governance win that belongs in *this* repo.
The tracker's CIO advisor is governed by its own seams (subscription billing,
model pins, schema'd output) and stays there; full ledger/budget/eval parity, if
pursued, is a tracker-side follow-up that must not sacrifice billing or
independence.
