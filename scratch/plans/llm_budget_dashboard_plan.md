# Plan: dashboard-managed LLM budgets + "forgone due to budget" attribution

> Status: PLAN FOR REVIEW — not implemented. This document is the only artifact in this branch.
> Author: planning session. Created 2026-06-01.
> Scope: make the per-purpose LLM budget caps editable from the dashboard (with a persistent editor AND a
> one-shot override), and make the brief + dashboard explicitly identify any analysis that was **forgone
> because of a budget cap** — instead of silently overspending or aborting the whole build.

## 0. Decisions locked (from the requirements discussion)

1. **Cap-exceeded behavior = "skip & attribute, keep building"** (a NEW third mode, `skip`). When a purpose
   set to `skip` would exceed its monthly cap, skip that call (no spend), mark the section *forgone due to
   budget* with the cap/spend numbers, finish the rest of the brief, and roll the skips up into a banner.
2. **Budget is OFF by default for now** ("ignore budget at the moment"). The migration leaves every existing
   cap **non-enforcing** (`warn` mode = today's behavior: proceed, just track spend). `skip` (and `block`)
   are **opt-in per purpose** via the dashboard. So PR 1 ships the machinery dormant — nothing starts getting
   forgone until you set a purpose to `skip` with a cap. This keeps PR #210 untouched in practice (see §3).
3. **Attribution model = a dedicated `BudgetSkip` row** (purpose / cap / spend / headroom).
4. **Override is per-ticker** — a one-shot "run anyway (ignore caps)" rebuild AND a **persistent per-ticker
   "ignore budget caps" flag** saved from the dashboard (with a Save button). Caps themselves are edited
   per-purpose in the budget panel; the *bypass* is per-ticker.
5. **Commit this plan** as a `Plan: …` PR (like #190).

---

## 1. Summary

The budget *engine* already exists and is good: caps live in a `llm_budgets` SQLite table, spend is tracked
per-purpose in the `llm_calls` ledger, and `src/llm_budget.py` exposes `check_budget()` / `set_cap()` /
`list_budgets()` / `month_report()`. The dashboard is already an **interactive Flask server**
([execution/comments_server.py](execution/comments_server.py)) with live POST/PATCH/DELETE routes. And a
per-call escape hatch (`force_budget_bypass=True`) already threads end-to-end through `call_llm`.

What's missing is three things, none of which require new infrastructure:

- **A third cap mode.** Today a cap is binary: `hard_block=True` → abort the build (PR #210), or
  `hard_block=False` → overspend (warn + proceed). Neither *skips* the work and reports it. We add an
  `on_exceed` mode: `skip` (skip + attribute) | `block` (PR #210 abort) | `warn` (overspend / non-enforcing).
  **Default + migration backfill = `warn`** so budgets stay OFF until you opt a purpose into `skip` (§0.2).
- **Section + brief attribution.** A new `SectionStatus.BUDGET_SKIPPED`, a budget-shaped reason carrying
  `purpose / cap / spend / headroom`, and a `ReportSpec.forgone_due_to_budget` rollup the renderers surface.
- **Dashboard wiring.** A budget panel (read spend-vs-cap, edit cap + mode) backed by a `POST
  /api/llm-budgets/<purpose>` route, plus a one-shot "run anyway" button on `/actions/refresh`.

**Net behavioral change:** none until you opt in. The machinery ships dormant (every purpose `warn` =
non-enforcing). The moment you set a purpose to `skip` with a cap from the dashboard, a cap hit stops being a
build-killer: you still get a brief, the brief + dashboard name exactly which analyses were forgone to stay
under budget, and a per-ticker override runs them anyway.

---

## 2. Current state (grounded)

### 2.1 Budget storage + API
- Table `llm_budgets` — [alembic/versions/0052_llm_budgets.py:86-114](alembic/versions/0052_llm_budgets.py):
  `purpose` (unique), `monthly_cap_usd`, `warn_threshold_pct` (default 0.80), `hard_block` (bool), `notes`.
  Seeded with 26 purpose caps + a `__default__` (bear_case=$50, transcript_summary=$80, `__default__`=$25, …).
- Read/write API — [src/llm_budget.py](src/llm_budget.py): `check_budget(purpose) -> BudgetCheck` (`:200-278`),
  `current_month_spend(purpose)` (`:90-129`), `list_budgets()` → rows + live spend + headroom% (`:338-387`),
  `set_cap(purpose, usd)` (`:390-428`), `month_report(month)` (`:431-519`).
- `BudgetCheck` (`:49-66`) already carries `allowed / warn / current_spend / cap / headroom_pct / hard_block /
  reason` — i.e. everything the "forgone" banner needs.
- Spend ledger: `llm_calls` ([alembic/versions/0034_llm_calls_ledger.py:39-88](alembic/versions/0034_llm_calls_ledger.py))
  — one row per call with `cost_estimate_usd`, `purpose`, `ticker`, `run_id`.
- Caps set today via CLI only: `python execution/manage_llm_budget.py --set bear_case --cap 75`
  ([execution/manage_llm_budget.py](execution/manage_llm_budget.py)).

### 2.2 Enforcement + the PR #210 policy we're amending
- Pre-call gate `_enforce_budget_pre_call(purpose, force_budget_bypass)`
  ([src/llm/cli.py:268-338](src/llm/cli.py)): on a `hard_block` cap it raises `LLMBudgetExceeded` (with the
  `BudgetCheck` attached); on a soft cap it warns + proceeds; `force_budget_bypass=True` skips the gate.
- PR #210 added `is_hard_stop(exc)` (`src/llm/cli.py`, single source of truth) and wrapped the LLM call in all
  6 sections with `if is_hard_stop(exc): raise` else degrade. `is_hard_stop` returns True for
  `LLMBudgetExceeded` + `LLMSetupError`. Tests: `tests/test_llm_call_exception_resilience.py`.
- The 6 LLM-driven sections (the budget gate must cover all): `bear_case`, `recent_developments`,
  `earnings` (themes split), `qa_roster`, `exec_compensation`, `valuation` (LLM in `compute/valuation_basis.py`).

### 2.3 Dashboard
- Interactive Flask app `create_app()` — [execution/comments_server.py:62-382](execution/comments_server.py),
  default `http://127.0.0.1:7421/`. Already has POST routes (`/actions/refresh`, `/comments`, `/chat/...`).
- Dashboard HTML rendered read-only today by `render_dashboard_html(rows)`
  ([src/pipeline/dashboard_html.py](src/pipeline/dashboard_html.py)) from `build_dashboard_rows()`
  ([src/pipeline/dashboard_status.py](src/pipeline/dashboard_status.py)).
- `/actions/refresh` already dispatches a per-ticker rebuild subprocess (the one-shot override hooks here).
- Per-ticker workspace brief is a static HTML written to `output/research/<TICKER>/<DATE>_workspace.html` by
  [execution/build_artifacts.py](execution/build_artifacts.py) → `src/report/renderers/workspace_html.py`.

### 2.4 Section / brief model
- `SectionStatus` enum (`OK / MISSING_DATA / PARTIAL / LLM_PENDING / NOT_APPLICABLE`) +
  `MissingReason(stage, fix_command, detail)` — [src/report/models.py:18-46](src/report/models.py).
- `build_report()` ([src/report/builder.py](src/report/builder.py)) assembles every section into `ReportSpec`.

---

## 3. Reconciliation with PR #210 (the sensitive part)

PR #210 deliberately made a hard-block `LLMBudgetExceeded` **propagate** ("re-running won't help; a
'transient, re-run' banner would lie"). Because we default every purpose to `warn` (non-enforcing, §0.2),
**PR #210 is untouched in practice** — nothing is `block` or `skip` until you opt in. When you DO opt a
purpose into `skip`, a cap hit becomes a loud per-section **BUDGET_SKIPPED** banner ("forgone — $X of $Y cap;
override to run") instead of an abort — which *preserves* #210's "don't lie / don't silently mask" principle,
just per-section rather than whole-build:

- `block` mode keeps **exactly** PR #210's behavior for any purpose an operator opts into hard-failing.
- `skip` mode is the opt-in user-chosen behavior: continue + attribute.
- `warn` mode (the default) = today's soft cap: proceed + track spend; no skip, no abort.
- `is_hard_stop` stays the single source of truth for `LLMSetupError` (CLI missing → still always propagate;
  that genuinely can't be re-run and isn't "forgone work"). Only the **budget** branch of the
  propagate-vs-degrade decision becomes mode-driven.

Implementation keeps PR #210's per-section `try/except` around the call; we change what happens *inside* it
for `LLMBudgetExceeded` (consult the purpose's mode), and leave `LLMSetupError` propagating untouched. PR
#210's existing tests that assert `LLMBudgetExceeded` propagates will be updated to assert the
mode-conditioned behavior (propagate iff mode=`block`).

---

## 4. PR breakdown (one PR per phase, cherry-picked onto fresh main)

### PR 1 — Budget engine: the `skip` mode + attribution primitives (backend only, no UI)
**Migration** `alembic/versions/00NN_llm_budget_on_exceed.py`: add `on_exceed TEXT NOT NULL DEFAULT 'warn'`
to `llm_budgets` with a CHECK in (`'skip'`,`'block'`,`'warn'`). Backfill existing rows:
`hard_block=1 → 'block'`, `hard_block=0 → 'warn'` — i.e. **leave budgets non-enforcing** ("ignore budget at
the moment", §0.2); `skip` is opt-in per purpose via the dashboard. Keep the `hard_block` column for
back-compat reads; document that `on_exceed` is now authoritative.

**`src/llm_budget.py`:**
- `budget_mode(purpose) -> Literal['skip','block','warn']` (reads `on_exceed`, `__default__` fallback).
- `set_mode(purpose, mode)` + extend `set_cap` callers; surface `on_exceed` in `list_budgets()` rows.
- `budget_decision(purpose, *, bypass: bool) -> BudgetDecision` — one place that returns
  `ALLOW | SKIP(check) | BLOCK(check)` from `check_budget()` + `budget_mode()` + `bypass`. This is the
  classifier the sections consult (mirrors how `is_hard_stop` centralizes its decision).

**`src/llm/cli.py`:** `_enforce_budget_pre_call` only raises for `block` mode now (so `skip`-mode purposes
never raise mid-call); `skip`/`warn` keep proceeding/warning. `is_hard_stop` unchanged for `LLMSetupError`;
for `LLMBudgetExceeded` the section layer (below) decides.

**`src/report/models.py`:** add `SectionStatus.BUDGET_SKIPPED = "budget_skipped"`; add a small
`BudgetSkip(purpose, cap_usd, spend_usd, headroom_pct)` model (or fold those onto `MissingReason` as optional
fields); add `ReportSpec.forgone_due_to_budget: list[BudgetSkip]` (default `[]`).

**The 6 sections + `compute/valuation_basis.py`:** before the LLM call, consult `budget_decision(purpose,
bypass=...)`. `SKIP` → return the section in `BUDGET_SKIPPED` with a `BudgetSkip` detail (no call, no spend);
`BLOCK` → raise (PR #210 path); `ALLOW` → proceed. Factor a shared
`report.sections._common.budget_skip_reason(purpose, repo_root)` so all 6 build the detail identically.
`build_report()` scans the built sections and fills `ReportSpec.forgone_due_to_budget`.

**Tests:** extend `tests/test_llm_call_exception_resilience.py` + a new `tests/test_budget_modes.py`:
per-section, `skip` mode → `BUDGET_SKIPPED` (no raise, no ledger write); `block` mode → raises (PR #210
parity); `warn` mode → proceeds; `force_budget_bypass` → ALLOW. `budget_decision` unit matrix.

### PR 2 — Renderer surfacing of "forgone due to budget"
- `src/report/renderers/{markdown,html,workspace_html}.py`: a distinct per-section banner for
  `BUDGET_SKIPPED` ("⏭ Forgone — monthly budget reached ($X of $Y). Override & rebuild to run."), visually
  separate from a MISSING_DATA pipeline-gap banner; plus a brief-level header rollup driven by
  `ReportSpec.forgone_due_to_budget` ("3 analyses forgone due to budget: bear case, news, exec-comp").
- Tests: renderer snapshot/assertion tests for the banner + rollup; absent when nothing forgone.

### PR 3 — Dashboard budget panel + persistent override
- `execution/comments_server.py`: `GET /api/llm-budgets` (→ `list_budgets()` incl. spend/cap/headroom/mode)
  and `POST /api/llm-budgets/<purpose>` (JSON `{cap_usd?, on_exceed?}` → `set_cap` / `set_mode`). Mirror the
  existing comments-CRUD patterns (validation, JSON errors).
- `src/pipeline/dashboard_html.py`: a budget panel — table of purposes with live spend-vs-cap bar, headroom%,
  an editable cap field + mode dropdown (skip/block/warn) + Save; and a per-ticker "N forgone (budget)"
  indicator sourced from the latest build's `forgone_due_to_budget`.
- Tests: route tests (set cap persists via `set_cap`; set mode persists); panel render test.

### PR 4 — Per-ticker override: one-shot + persistent (§0.4)
- **One-shot:** `execution/comments_server.py` `/actions/refresh` accepts `force_budget_bypass: bool`; thread
  a `--force-budget-bypass` flag through the rebuild subprocess → `build_artifacts.py` → `build_report(...,
  force_budget_bypass=True)` → `call_llm(force_budget_bypass=True)` for that run only. Dashboard: a per-ticker
  "Run anyway (ignore caps)" button.
- **Persistent:** a per-ticker `bypass_budget` flag the dashboard saves (a **Save** toggle) — stored in a
  small dashboard-writable per-ticker settings store (new `ticker_settings(ticker, bypass_budget, updated_at)`
  table, OR a `bypass_budget` key in `micro_thesis/holdings/<TICKER>.json`; table preferred so the dashboard
  owns it and the thesis file stays user-authored). `build_report` reads it and sets `force_budget_bypass`
  for that ticker's builds until cleared.
- Tests: refresh passes the one-shot flag; the persistent flag round-trips via the dashboard and a build for
  that ticker bypasses caps; an integration test that a bypass rebuild populates a previously-forgone section.

---

## 5. What does NOT change
- The `llm_budgets` cap values, `check_budget`, `current_month_spend`, `month_report`, the `llm_calls`
  ledger, and `force_budget_bypass` plumbing — all reused as-is.
- `LLMSetupError` still always propagates (CLI-missing is not "forgone work").
- The CLI `manage_llm_budget.py` stays valid (gains `--mode` for parity, optional).
- Non-LLM sections, and the `enable_llm=False` dev path, are untouched.

## 6. Risks / edge cases
- **Default-mode migration** changes prod behavior (caps stop aborting). Mitigated by per-purpose `block`
  opt-in + the migration being the explicit, reviewed switch.
- **`__default__` mode** governs uncapped purposes — make sure `budget_mode` falls back to it.
- **Mid-run spend crossing the cap** between sections: pre-gate via `budget_decision` per section means later
  sections see the updated spend and skip — desired (graceful), and attributed.
- **Bypass + ledger**: a bypass run still records spend (good — overspend is visible in `month_report`).

## 7. Resolved decisions (sign-off 2026-06-01)
1. **Cap default / backfill**: **non-enforcing** — existing `hard_block=0` rows migrate to `warn`, not
   `skip` ("ignore budget at the moment"). `skip` is opt-in per purpose. Column default = `warn`.
2. **Attribution shape**: a dedicated **`BudgetSkip`** model (purpose / cap / spend / headroom).
3. **Override**: **per-ticker** — a one-shot "run anyway" rebuild AND a persistent per-ticker `bypass_budget`
   flag saved from the dashboard (Save button). Per-section override is out of scope.
4. **This plan is committed** as a `Plan: …` PR.
