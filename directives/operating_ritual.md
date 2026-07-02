# Operating ritual — the owner's weekly loop

**Why this exists (owner directive, 2026-07-02):** the coach only learns from what
flows *through* it. The 2026-07-02 full-program review found every learning circuit
built and armed — and almost none of them fed: zero `/review` runs ever, one
`claude_session` landing, ~18 organic captures. The corpus-freshness rule is the
owner's own: *"if it becomes stale, I just won't even use it because it will be
shit."* This page is the antidote — the minimum routing discipline that keeps the
Ledger a live corpus instead of a museum. It is a ritual, not a feature: nothing
here builds anything.

The success bar it serves: **the coach changes ≥1 real decision by end of Q3'26**
(`project_thought_partner_decisions_2026_07`, decision 8).

## Daily (~10 min, usually mornings)

- **Read the morning digest / inbox chips.** Approve, dismiss (with a reason — the
  dismissal is signal), or tap a wondering chip into research.
- **Capture musings the moment they occur** — Telegram voice or text, tray on
  desktop. The bar for "worth capturing" is LOW: half-formed hunches are exactly
  what the wondering-detector and the tenet distiller feed on.
- **Answer coach pings same-day** (governor caps them at 1/day, so each one was
  judged worth your attention). Dismissing is fine — three dismissals auto-mutes
  that moment class, which is also signal.

## Before every trade — the two pledges

- **Buys ≥ $1k: pledge first.** Telegram "buying X ~$Nk" *before* the order. The
  catalyst-test challenge that comes back is the entry-coaching moment; answer it
  honestly. (The retro net catches unannounced fills within ~24h, but post-hoc
  coaching is strictly weaker — you already own the position.)
- **Trims/sells: `/review <TICKER>` first.** This is the surface that carries your
  own empirics — the behavioral guard exists because your graded history shows the
  sell-winners-too-early pattern (5 wrong sells: MU, GOOGL, TSM, NVDA, AMZN). A
  trim that survives the pre-analysis + guard + (soon) tax block is a decision;
  one that skips them is a mood.
- **Annotate when the follow-up asks.** Conviction + falsifier on new decision
  stubs — an unfalsifiable decision can never be graded, and ungraded decisions
  never improve the calibration that makes the coach worth listening to.

## Weekly (~30–45 min, e.g. Sunday)

- **Ledger tab pass:**
  - *Reconcile section* — status anything resolved/superseded; ratify or edit
    pending falsifiers (unratified falsifiers are deliberately excluded from the
    tripwire engine, so this queue is armed alerts waiting on you).
  - *Research proposals* — approve/dismiss the week's tapped wonderings.
  - *Worldview section* — accept/edit/reject newly distilled Tenets. Nothing
    reaches decision prompts without your accept (and until you flip
    `LEDGER_WORLDVIEW_ANCHOR`, nothing reaches them at all).
- **Health glance:** cron-health panel (red dots?), Optimizer panel (anonymous-
  purpose alarm, infra flags). Two minutes; silent failure is the platform's known
  weakness.

## After deep Claude Code sessions

- **Land the session** (`/ledger-land` → `execution/land_session_notes.py`) whenever
  a session resolved a decision, closed an intent, or produced a real musing.
  Topics that resolve in chat and never land are the canonical staleness case
  (the NVDA-LEAP ghost: rejected in a session, haunted the corpus for weeks).

## Quarterly (earnings season)

- Rebuild briefs for reporting names (`--enable-llm`, right `--flavor`); run real
  LLM extraction for new quarters (never hand-insert KPI values).
- Check falsifier tripwires against the fresh quarter's KPIs — the falsifiers you
  wrote are only as good as the data under them.
- Grade decisions whose outcomes are now known; skim the calibration read.

## Anti-patterns (each one starves a specific circuit)

| Anti-pattern | What it starves |
|---|---|
| Deciding in your head / in a chat, never landing it | corpus freshness → the freshness gate blocks coaching on it |
| Buying first, pledging never | entry coaching (retro-net is the weaker fallback) |
| Trimming without `/review` | the behavioral guard — your best-evidenced protection |
| Leaving decision stubs without conviction/falsifier | Brier calibration + tripwires |
| Letting the Reconcile queue age | tripwire coverage + "you said X" trust |
| Ignoring the cron-health panel | everything (failures here are silent by design flaw) |
