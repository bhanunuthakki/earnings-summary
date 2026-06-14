# How to use the workspace reports

Quick reference for the comment + chat workflow on rendered research reports,
plus everything you need to refresh data, regenerate reports, and run the
analytical pipeline.

All commands work from **any directory** in cmd.exe — the `.bat` launchers
self-locate the repo, so you don't need to `cd` first.

**The dashboard command center at `http://127.0.0.1:7421` is now the primary
interface** — status, cross-ticker analytics, per-ticker drill-down, refreshes,
and comment/thesis editing in one live app. The `.bat` / CLI workflow below
remains for scripting and automation.

**Jump to:**
- [Command center (start here)](#command-center-start-here)
- [One-time setup](#one-time-setup)
- [Daily workflow](#daily-workflow)
- [Slash-keywords in comments](#slash-keyword-shortcuts-fastest-path--skip-the-dropdown)
- [Refreshing data + regenerating reports](#refreshing-data--regenerating-reports)
- [Full CLI reference](#full-cli-reference)
- [Pinning a valuation multiple per ticker](#pinning-a-specific-valuation-multiple-per-ticker)
- [Analyst tips](#analyst-tips)
- [Troubleshooting](#troubleshooting)

---

## Command center (start here)

One live app — `execution/comments_server.py` on **http://127.0.0.1:7421** — is
the front door for status, analysis, refreshes, and editing. Launch it once and
leave it running:

```cmd
python execution\comments_server.py --port 7421
```

(add `--repo-root <path>` to point at a specific checkout). Every action below
is a click or a `POST`.

### What's where

| Page | What it shows |
|---|---|
| `/` | Portfolio + evaluation status — last FMP pull, last transcript, last build, open-comment count, breach badge. Each ticker links to its drill-down; **Open↗** opens the latest brief. Includes the IR-KPI refresh control. |
| `/analytical` | Cross-ticker overview, **live**: LLM **budget** (read-only here), the **decisions** ledger + calibration, the DCF **trigger ladder**, insider activity, prediction outcomes. Byte-identical to the static `build_analytical_dashboard.py` export — same renderer, always fresh from the DB. |
| `/ticker/<T>` | **Per-ticker command center**: identity + freshness; an **artifacts inventory** (every brief / DCF / holdings / transcript / IR / FMP / LLM-cache file with mtime + size); the **analyses-ran** log (thesis eval · time-series signals · trigger alerts · queued actions · Say-Do · DCF · LLM-call cost (30d) · brief renders); recent decisions; the read-only **thesis** (tier-1 KPIs + break rules + breakers); and your live **position** + an **Open in Portfolio Tracker ↗** deep link. |

JSON siblings for scripting: `GET /api/overview`, `GET /api/ticker/<T>`, `GET /api/dashboard`.

### Refreshes — with overrides

`POST /actions/refresh` `{ticker, mode, steps?, force?, force_budget_bypass?}`:

- **`mode`** — `stale` (skip FMP if pulled within the window) or `full`.
- **`steps`** — run only a subset; any of `fmp, transcripts, process_ir_docs, news, extract_kpis, saydo, dcf, thesis_eval, build_report`. Omit for the standard chain. (`news` / `dcf` / `thesis_eval` are opt-in — a routine refresh stays lean.)
- **`force`** — run FMP even if fresh (override the stale-skip).
- **`force_budget_bypass`** — ignore LLM budget caps for this run.

Output streams over `GET /actions/stream/<job_id>` (SSE); jobs are single-flight per ticker. (CLI equivalent: `python execution/refresh_dispatch.py --ticker NU --steps dcf,build_report --force`.)

### Process comments + change the thesis (preview → apply)

- **Preview a thesis edit** — `POST /api/thesis/<T>/preview` `{report_date, comment_ids?}` runs the Opus `edit_thesis` / `edit_structured` routers in dry-run and returns a **before/after diff** *without writing anything*. A budget/setup hard-stop surfaces as `402`/`503`; a transient LLM hiccup degrades to `{degraded: true}` (never a half-applied thesis).
- **Apply** — `POST /api/comments/process` `{ticker, apply:true, report_date?, clear?, no_rebuild?}` runs the real pipeline (mutations + auto-rebuild) as a streamed job. `apply:false` returns the dry-run resolutions inline. This is also the apply path for thesis edits.

### Budget

The overview surfaces spend / cap / headroom **read-only**. Editing caps, the
`skip` / `block` / `warn` modes, and the per-ticker "run anyway" bypass live in
the dashboard's budget panel and `execution/manage_llm_budget.py`.

### Two-app topology (research ↔ portfolio tracker)

The command center and the **portfolio-tracker** app stay separate (own repos,
own ports) and link **read-only**:

- **research → tracker** — each `/ticker/<T>` page shows a live position strip
  (shares / cost / value / unrealized P&L / last trade decision) read from the
  tracker's SQLite at `../portfolio-tracker/portfolio.db` (opened read-only),
  plus an **Open in Portfolio Tracker ↗** deep link to
  `<PORTFOLIO_TRACKER_URL>/trade-analysis?ticker=<T>` (default
  `http://localhost:5173`; the tracker pre-filters its table to the ticker).
  Set `PORTFOLIO_TRACKER_URL` if the tracker runs elsewhere.
- **tracker → research** — the tracker reads this repo's DB for next-earnings +
  thesis status and links to the latest brief via its
  `/api/earnings-summary/brief/<T>` passthrough.

Both sides degrade gracefully when the sibling isn't running — the strip hides,
the links still render.

---

## One-time setup

Make sure these are installed:

- **Python 3.11+** with the repo's `requirements.txt` (`pip install -r requirements.txt`)
- **Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`)
- Either `ANTHROPIC_API_KEY` in your env, or `claude auth login` for subscription billing

---

## Daily workflow

### 1. Build a report

```cmd
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\build_report.bat NU --enable-llm
```

- `--enable-llm` runs the full pipeline (bear case, news, valuation, company description). Omit for a faster build that reuses cached outputs.
- Output lands at `output\research\<TICKER>\<DATE>_workspace.html`.

### 2. Start the comments + chat server

```cmd
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\start_comments_server.bat
```

Or just **double-click** `start_comments_server.bat` in Explorer.

- Server runs on `http://localhost:7421`.
- **Keep this terminal window open** while you're reviewing the report.
- `Ctrl+C` to stop.

### 2a. (Optional) Open the dashboard

Once the server is running, visit `http://localhost:7421/` for the live
command center across portfolio + evaluation tickers — status (last FMP
date, last transcript quarter with Q&A marker, last build mtime, open
comment count, current breach state) **plus** per-ticker refresh,
per-step runs, comments-processing, and thesis editing. Click **Open↗**
on any row to load that ticker's latest workspace report. See
[Command center (start here)](#command-center-start-here) for the full
action surface.

### 3. Open the report in your browser

Open `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\output\research\NU\<DATE>_workspace.html`
(double-click in Explorer, or drag into Chrome).

### 4. Comment on anything

Two ways to comment:

**(a) Structured anchors** — hover over a KPI row, failure mode card, news
item, valuation rationale, thesis lede, or company overview panel. A small
`+` pin appears at the top-right corner. Click it.

**(b) Free-text (Google Docs style)** — highlight ANY text in the report.
A floating `+ Comment` button appears below your selection. Click it.

Either way, the comment sidebar slides in from the right. Type your
comment, optionally pick an intent (or leave on "Auto-classify"), and
click **Post**.

**Intents** drive how the processor handles each comment:

| Intent | What the processor does |
|---|---|
| `drop_kpi` | Removes the KPI from `micro_thesis/holdings/<T>.json` |
| `edit_thesis` | Asks Opus to revise the thesis paragraph using your comment as guidance |
| `ask_question` | Asks Opus your question with the thesis + bear-case as context; reply appears in the comment's follow-up thread |
| `fix_data` | Logs a TODO line in `directives/data_fixes.md` for manual fixing |
| `rewrite_section` | Emits cache-invalidation instructions for the targeted section |
| `platform_change` | Files a tagged entry in `directives/platform_backlog.md` (cross-workspace bug/feature, not a single-ticker edit) |
| `curate_peers` | Steers the comparable-company set for the ticker |
| (blank) | Haiku classifies into one of the above automatically |

### Slash-keyword shortcuts (fastest path — skip the dropdown)

Prefix your comment with one of these to set the intent inline and skip
both the dropdown and the Haiku auto-classify call. The keyword is
stripped from the stored comment text.

| Prefix | Routes to | When to use |
|---|---|---|
| `/kpi` | `drop_kpi` | Remove a KPI you don't want to track anymore |
| `/thesis` | `edit_thesis` | Modify the thesis paragraph |
| `/update` | `edit_thesis` | Same as `/thesis` — incremental thesis edit |
| `/q` | `ask_question` | Quick question (short alias for `/ask`) |
| `/ask` | `ask_question` | Ask Claude a question with full thesis + bear-case context |
| `/fix` | `fix_data` | Flag a data error for manual fixing |
| `/rewrite` | `rewrite_section` | Bigger rewrite of the artifact this comment is on |
| `/platform`, `/feature`, `/bug` | `platform_change` | Cross-workspace bug or feature — lands as a tagged entry in `directives/platform_backlog.md`, NOT a single-ticker brief edit |
| `/peers`, `/curate` | `curate_peers` | Steer the comparable-company (peer) set |

Rules:
- Keyword must be at the start of the comment, no leading whitespace
- Case-insensitive (`/KPI` works)
- Optional `:` or space after (`/ask: how does X work?` is fine)
- Explicit dropdown picks always win over the keyword

Examples:
```
/kpi SuperCore split made this irrelevant
/thesis revise to flag the FGTS regulation phasing
/q what's NU's NPL trend over the last 8 quarters?
/fix segments don't sum to total revenue
/rewrite this is too generic, focus on the cohort dynamics
```

Comment status colors on the report:
- **Amber underline** / amber pin = open
- **Green underline** / green pin = addressed
- No marker = no comments

### 5. Chat with Claude about the report

Click the **Chat** button in the bottom-right corner. The drawer opens
with a Claude chat session backed by the unified ask engine (model
chosen by the engine; Sonnet by default) that has the full report
context loaded:

- Your thesis, tier-1 KPIs, business-model rules
- Bear-case failure modes + most-underweighted callout
- Valuation multiple + rationale
- Company description elevator pitch
- Read-only filesystem access to `data/`, `micro_thesis/`, `.tmp/`,
  `transcripts/` (so it can pull a verbatim transcript quote, look up
  segment numbers from FMP, etc.)

Type a question, press `Cmd+Enter` (or `Ctrl+Enter`) to send.

If you ask for an edit ("rewrite the thesis assuming Mexico interchange
caps at 1.5%"), the response includes a **Preview** / **Apply** button —
click Apply to write the change to disk.

### 6. Process comments

When you're ready to act on the comments you've left:

```cmd
:: dry-run preview (default — won't touch files)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\process_comments.bat NU

:: actually mutate files (edits holdings JSON, runs LLM, etc.)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\process_comments.bat NU --apply

:: ...and drop addressed/dismissed comments after processing
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\process_comments.bat NU --apply --clear
```

After processing, **rebuild the report** to see the updates:
```cmd
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\build_report.bat NU --enable-llm
```

Then refresh the browser tab. Addressed comments turn green; their
resolution notes + chat threads are visible when you click the highlight.

---

## Refreshing data + regenerating reports

When you want fresh inputs flowing into the report, run the relevant
refresh in this order. Each step is independent — you can run any of
them in isolation.

### 1. FMP financial data (statements + segments + ratios + key_metrics)

```cmd
:: One ticker, last 8 quarters
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_fmp.bat NU

:: One ticker, deeper history
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_fmp.bat NU 20

:: All tracked tickers (uses FMP API quota — be mindful)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_fmp.bat --all
```

Writes JSON to `data/historical/fmp/<TICKER>_<endpoint>.json`. Updates
`tracked_companies.fmp_data_upto`. This is the source of truth for the
Financials tab, valuation multiples, and DCF inputs.

### 2. Earnings-call transcripts

```cmd
:: One ticker — pulls the last 6 fiscal quarters from the free aggregators
:: (roic.ai → stockanalysis.com → tickertrends.io)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_transcripts.bat NU

:: All active tickers
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_transcripts.bat --all
```

Drops new files into `transcripts/processed/<T>_Q<n>_<YYYY>.txt` and
registers them into the `transcripts` SQLite table. Skips quarters where
a file already exists.

If you have PDFs from IR (presentations, press releases, transcripts)
that aren't on the aggregators, **drop them into `_inbox/`** and run:
```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\intake_documents.py --process
```
The intake classifier files them into `ir_documents/<T>/<period>/` and
chains into the LLM summarizer.

### 3. LLM summarize the IR documents (transcripts / decks / press releases)

After step 2 (or after dropping PDFs in `_inbox`), run:
```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\process_ir_documents.py --ticker NU
```
Reads each unprocessed document, runs the LLM summarizer with the
thesis + bear-case anchor block, writes:
- `.tmp/<T>_Q<n>_<Y>_summary.txt` (transcripts)
- `.tmp/<T>_Q<n>_<Y>_press_release_summary.txt` (press releases)
- `.tmp/<T>_Q<n>_<Y>_presentation_brief.txt` (slide decks)

These feed the Earnings tab and the bear case.

### 4. KPI extraction (populate the Thesis tab's tracked KPIs)

```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\extract_kpis_from_summaries.py --ticker NU --source earnings --repo-root C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary
```

Reads the `.tmp/<T>_*_summary.txt` files, asks Haiku to extract values
for each tier-1 KPI defined in `micro_thesis/holdings/<T>.json`, persists
to the `kpi_facts` table. Run this after step 3 whenever you add new
quarters or update the tier-1 KPI list.

If you don't see any tracked KPIs in the rendered report, this step
hasn't run yet.

### 5. SayDo pairwise rebuild

```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\build_saydo_pairs.py --ticker NU --repo-root C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary
```

Walks the per-quarter summaries chronologically and generates one
`.tmp/SayDo_<T>_Q<prev>_<prev_y>_Q<curr>_<curr_y>.txt` per consecutive
pair. Idempotent — re-runs skip existing pairs. Pass `--refresh` to
force-regenerate every pair (use after a prompt change).

### 6. News (force-refresh the §News tab)

```cmd
:: Default 7-day lookback
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_news.bat NU

:: 14-day lookback
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_news.bat NU 14

:: Refresh news for everyone tracked
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\refresh_news.bat --all-tracked
```

Bypasses the 7-day cache, runs Claude WebSearch + WebFetch with the
thesis anchor so every news item names which tier-1 KPI it touches.
Regenerates the workspace report wholesale (calls `build_artifacts.py`
internally with `--refresh-news --enable-llm`).

### 7. Rebuild the report with refreshed LLM calls

```cmd
:: Workspace renderer + full LLM pipeline (bear case + valuation + news + company description)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\build_report.bat NU --enable-llm

:: Fast rebuild — reuse cached LLM outputs (just re-renders HTML from existing JSON)
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\build_report.bat NU
```

**Cache invalidation by deletion** — to force a specific section to
re-run, delete its cache file and rebuild with `--enable-llm`:

| Section | Delete | Then rebuild with |
|---|---|---|
| Bear case | `data/bear_case/<T>.json` | `build_report.bat <T> --enable-llm` |
| Valuation multiple | `data/valuation_basis/<T>.json` | same |
| Company description | `data/company_description/<T>.json` | same |
| News | `.tmp/news_cache/<T>.json` | `refresh_news.bat <T>` |
| Q&A roster | `data/qa_topics/<T>.json` | `build_report.bat <T> --enable-llm` |
| SayDo importance filter | `data/saydo_filter/<T>.json` | same |
| Per-quarter summary | `.tmp/<T>_Q<n>_<Y>_summary.txt` + reset `processed=false` in `.tmp/document_index.json` | re-run `process_ir_documents.py --ticker <T>` |

### 8. One-button full refresh

If you just want **everything** regenerated end-to-end:

```cmd
C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\full_refresh.bat NU
```

Runs all 6 steps in sequence: FMP → transcripts → IR doc processing →
KPI extraction → SayDo pairs → workspace report with `--enable-llm`.
Takes 5-15 minutes per ticker depending on how much is stale. Each step
is independent — a failure in one doesn't kill the rest.

---

## Full CLI reference

Every `.bat` is a thin wrapper around a Python script. Use the Python
script directly when you need flags the `.bat` doesn't expose.

### Launchers (cmd.exe-friendly)

| Launcher | What it does |
|---|---|
| `build_report.bat <T> [--enable-llm]` | Build the workspace report |
| `refresh_fmp.bat <T> [LIMIT]` | Pull fresh FMP financial data |
| `refresh_transcripts.bat <T>` | Backfill missing earnings transcripts |
| `refresh_news.bat <T> [DAYS]` | Force-refresh §News with fresh WebSearch |
| `full_refresh.bat <T>` | All of the above + KPI extract + SayDo + report |
| `start_comments_server.bat` | Start the Flask server on :7421 |
| `process_comments.bat <T> [--apply] [--clear]` | Process open comments |

### Python scripts (full surface)

| Script | When to use |
|---|---|
| `execution/build_artifacts.py` | Build report. Flags: `--ticker`, `--all-tracked`, `--enable-llm`, `--flavor {portfolio,evaluation}`, `--news-days N`, `--refresh-news` (`--renderer` is a compatibility no-op — workspace is the only renderer) |
| `execution/fetch_fmp_historical_data.py` | FMP refresh. Flags: `--ticker`, `--all`, `--limit N` |
| `execution/save_fmp_data.py` | Full FMP endpoint sweep (more granular than fetch_fmp_historical_data). Flags: `--tickers`, `--portfolio`, `--watchlist`, `--evaluation`, `--all`, `--skip-existing`, `--max-calls N` |
| `execution/backfill_transcripts.py` | Pull missing transcripts |
| `execution/process_ir_documents.py` | LLM-summarize IR docs. Flags: `--ticker`, `--quarter`, `--year`, `--all`, `--dry-run` |
| `execution/intake_documents.py` | Classify + file PDFs from `_inbox/`. Flags: `--process` to also chain into the summarizer |
| `execution/register_dropped_documents.py` | Register pre-filed PDFs from `micro_thesis/sources/<T>/` into the document index |
| `execution/extract_kpis_from_summaries.py` | Extract tier-1 KPIs from summaries. Flags: `--ticker`, `--source {earnings,ir}`, `--repo-root` |
| `execution/extract_kpis_from_ir.py` | Manual KPI extraction with explicit manifest |
| `execution/extract_company_description.py` | Regenerate the company-description JSON |
| `execution/build_saydo_pairs.py` | Rebuild SayDo pairwise commits. Flags: `--ticker`, `--refresh` |
| `execution/refresh_news.py` | Force-refresh §News. Flags: `--ticker`, `--all-tracked`, `--news-days N` |
| `execution/refresh_dcf.py` | Recompute DCF from workbook + live price |
| `execution/refresh_cache.py` | Tier-aware FMP refresh queue (run / audit / status / archive subcommands) |
| `execution/quarterly_refresh.py` | Full orchestrator — runs everything for all tickers. Used by the monthly cron. Flags: `--ticker`, `--dry-run`, `--json` |
| `execution/daily_fetch_and_brief.py` | Daily worker — drains the brief_dirty queue. Used by the cron |
| `execution/run_thesis_evaluator.py` | Run the thesis evaluator (writes a new `thesis_evaluations` row) |
| `execution/ingest_transcripts.py` | Register transcript files into the `transcripts` table |
| `execution/ingest_earnings_surprises.py` | Pull earnings surprise data |
| `execution/seed_kpi_definitions.py` | Seed the `kpi_definitions` table from holdings JSONs (run after adding KPIs) |
| `execution/sweep_output_history.py` | Archive old report HTMLs into a dated subdir |
| `execution/comments_server.py` | Flask server. Flags: `--port`, `--repo-root`, `--host` |
| `execution/process_report_comments.py` | Intent processor. Flags: `--ticker`, `--all`, `--report-date`, `--apply`, `--clear` |

Add `--help` to any script for its full flag list.

---

## Where things live

| File / directory | What it is |
|---|---|
| `data/report_comments/<T>/<DATE>.json` | Comment store (one per ticker+date) |
| `data/report_chats/<T>/<DATE>.json` | Chat thread store |
| `data/bear_case/<T>.json` | Cached bear case from last `--enable-llm` build |
| `data/valuation_basis/<T>.json` | Cached Opus-picked valuation multiple |
| `data/company_description/<T>.json` | Cached company narrative |
| `micro_thesis/holdings/<T>.json` | Your thesis + tier-1 KPIs (the editable source of truth) |
| `output/research/<T>/<DATE>_workspace.html` | The rendered report |
| `output/research/<T>/<DATE>_report.md` | Legacy markdown version |
| `output/research/<T>/<DATE>_dcf.xlsx` | DCF workbook |

---

## Pinning a specific valuation multiple per ticker

The valuation tab lets Opus pick the diagnostic multiple per ticker. To
override, add `valuation_multiple_override` to `micro_thesis/holdings/<T>.json`:

```json
{
  "ticker": "NU",
  "valuation_multiple_override": "P/E (NTM)",
  ...
}
```

Allowed values: `EV/NTM Revenue`, `EV/LTM Revenue`, `EV/NTM EBITDA`,
`EV/LTM EBITDA`, `P/E (NTM)`, `P/E (LTM)`, `P/B`, `P/TBV`, `P/FCF`,
`EV/FCF`.

Delete `data/valuation_basis/<T>.json` and rebuild with `--enable-llm` to
pick up the change.

---

## Analyst tips

### Reading the workspace report

Tab order (portfolio + watchlist flavor):
1. **Thesis** — your investment case + tier-1 KPI status + break-rule
   evaluation. Start here on a known name. The thesis lede is at the top
   under the identity strip; it's commentable.
2. **Earnings** — per-quarter analytical notes (newsletter-style, ordered
   newest-first). Use the quarter selector to scrub through historical
   prints. Q&A roster sits below each card.
3. **News** — last 7 days of material developments, ranked by thesis
   impact. Every item names which tier-1 KPI it touches.
4. **Say · Do** — print-vs-guide for the most recent quarter pair +
   verdict bar showing the trajectory of the last N quarters' attribution.
5. **Financials** — 12-quarter YoY% matrix for line items, segments,
   geographies, OI, tracked KPIs + 12Q level table with segment
   drill-down.
6. **Valuation** — Opus picks one diagnostic multiple per ticker; shows
   current value, 12Q sparkline, range, rich/cheap verdict, rationale.
7. **Bear case** — `most_underweighted` callout + named failure modes.
   Each card has Evidence / Leading indicator / Quant impact / Refutation.
8. **Company** — analytical business overview + revenue mechanics +
   segments + geographies + IR doc summaries.
9. **Position** (only when held) — your shares, cost basis, P&L, recent
   transactions, open vs closed decisions.
10. **Sources** — coverage matrix, validation issues, source-doc audit.

Evaluation flavor leads with **Company** instead of Thesis (you're new
to the name) and adds an **Eval Screen** tab with the 3y
quick-categorization data table.

### Editing your thesis

The thesis is the single source of truth for everything downstream
(KPIs, break rules, anchor injected into LLM prompts, valuation
multiple selection, bear case grounding).

```
micro_thesis/holdings/<TICKER>.json
```

Key fields:
- `thesis` — free-text paragraph (the lede shown on every tab)
- `verdict` — `intact` / `watch` / `broken` / `pending` (drives badge color)
- `key_driver` — one-line "what I'm watching"
- `tier_1_kpis` — list of `{name, break_condition, source, ...}`
- `business_model_rules` — quantitative tripwires with `narrative`,
  `kpi_name`, `comparator`, `threshold`, `unit`, `consecutive_periods`
- `qualitative_breakers` / `thesis_breakers_qualitative` — soft risks
- `competitive_watchlist` — rivals to monitor
- `valuation_multiple_override` (optional) — pin one of the 10 allowed
  multiples (see "Pinning a specific valuation multiple per ticker")

After editing the JSON, **rebuild with `--enable-llm` to flow the change
through**:
- Bear case re-grounds on the updated thesis
- News + per-quarter summaries re-injected with the updated anchor
- Valuation multiple re-evaluated (delete `data/valuation_basis/<T>.json` first)
- KPI ledger re-renders with the new break conditions

### Onboarding a new ticker

1. Create `micro_thesis/holdings/<NEW_TICKER>.json` (copy an existing
   one as a template; NU.json is a good bank/fintech example, GOOG.json
   is good for a profitable platform).
2. Add the ticker to `tracked_companies`:
   ```cmd
   python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\onboard_ticker.py --ticker <NEW_TICKER> --list-type portfolio
   ```
   (Or `--list-type watchlist` / `evaluation` / `archived`.)
3. Pull FMP data: `refresh_fmp.bat <NEW_TICKER> 20`
4. Pull transcripts: `refresh_transcripts.bat <NEW_TICKER>`
5. Process IR docs: `python execution\process_ir_documents.py --ticker <NEW_TICKER>`
6. Seed KPI definitions: `python execution\seed_kpi_definitions.py`
7. Extract KPIs: `python execution\extract_kpis_from_summaries.py --ticker <NEW_TICKER> --source earnings --repo-root <REPO>`
8. Build: `build_report.bat <NEW_TICKER> --enable-llm`

Or after step 1-2, just: `full_refresh.bat <NEW_TICKER>`

### Adding an IR document (investor day, off-cycle press release, etc.)

Two paths:

**(a) Auto-classify from `_inbox/`** — drop the PDF in
`<REPO>/_inbox/` then:
```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\intake_documents.py --process
```
The intake classifier identifies ticker + period + doc-type, files into
`ir_documents/<T>/<period>/`, registers in `document_index.json`, and
chains into the LLM summarizer. When a transcript is among the dropped
files, the chain additionally bridges it into `transcripts` +
`transcript_segments` (so the Say-Do tab sees it) and runs the
forward-looking commitment extractor — matching what
`refresh_transcripts.bat` does for auto-fetched transcripts.

**(b) Manual registration** — if a PDF is already filed but not in the
index (e.g., bulk-imported from another source), reset its `processed`
flag and re-run the summarizer. See [examples in execution/intake_documents.py](../blob/main/execution/intake_documents.py).

Either path, finish with a rebuild so the new document flows into the
report: `build_report.bat <T> --enable-llm`.

### When to refresh vs rebuild

| Situation | Right command |
|---|---|
| Just edited the HTML rendering / CSS / JS | `build_report.bat <T>` (no `--enable-llm` — fast) |
| Just edited the thesis JSON | `build_report.bat <T> --enable-llm` |
| New quarter just landed | `refresh_fmp.bat <T>` then `refresh_transcripts.bat <T>` then `build_report.bat <T> --enable-llm` |
| Want to re-prompt the bear case on the same data | delete `data/bear_case/<T>.json`, then `build_report.bat <T> --enable-llm` |
| News feels stale | `refresh_news.bat <T>` |
| Just want everything fresh | `full_refresh.bat <T>` |
| Quarterly catch-all for all tracked tickers | `python execution\quarterly_refresh.py` |

### Comment hygiene

- Use the `/`-keywords aggressively — skipping the dropdown + Haiku
  classifier saves time and is more reliable.
- Comments are **per-report** (per ticker+date). They don't carry across
  rebuilds with a new date. If a comment is important, address it
  before rebuilding.
- Free-text highlights survive if the underlying text is unchanged.
  When you edit the company overview or thesis, old highlights on the
  changed text silently drop (the comment stays in the JSON store).
- Run `process_comments.bat <T>` in `--dry-run` mode first to preview
  what each intent will do, then re-run with `--apply` when satisfied.

### Subscription billing vs API billing

The Claude CLI honors whichever auth is configured:
- `ANTHROPIC_API_KEY` set in env → API metered billing
- `claude auth login` + no `ANTHROPIC_API_KEY` → Pro/Max subscription

For this repo, **API key is the documented default**. You don't need
to unset it. Cron jobs and `.bat` launchers all route through the same
`llm_client.py` wrapper.

---

## Troubleshooting

**`python: can't open file '...\execution\comments_server.py'`**
You're trying to run the bare `execution/...` path from `C:\Users\Bhanu`.
Use the `.bat` launchers — they self-locate the repo.

**`'#' is not recognized as an internal or external command`**
`#` is a bash comment marker. cmd.exe doesn't understand it. Skip any
line that starts with `#` when copying instructions from chat.

**Server unreachable — chat / new comments don't work**
Make sure `start_comments_server.bat` is running in another window.
Read-only comment display works without the server (existing comments
+ highlights show); posting new ones and chat need it.

**Highlight didn't appear on a free-text comment**
The selected text either contains an element boundary mid-selection
(rare) or the underlying text in the panel changed since the comment was
posted. The comment stays in the store — click the pin on the structured
anchor it lives under, or look in `data/report_comments/<T>/<DATE>.json`.

**Tracked KPIs are empty in the report**
Run the KPI extractor first:
```cmd
python C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\extract_kpis_from_summaries.py --ticker NU --source earnings --repo-root C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary
```

The single canonical procedure lives under
[Adding an IR document](#adding-an-ir-document-investor-day-off-cycle-press-release-etc) in the Analyst tips section.
