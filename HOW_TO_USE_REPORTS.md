# How to use the workspace reports

Quick reference for the comment + chat workflow on rendered research reports.

All commands work from **any directory** in cmd.exe — the `.bat` launchers
self-locate the repo, so you don't need to `cd` first.

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
with a Claude Sonnet 4.6 session that has the full report context loaded:

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

---

## Adding a new IR document for a ticker

1. Drop the PDF into `_inbox/` (or wherever you keep them)
2. Run `python execution/intake_documents.py --process` (classifies +
   files into `ir_documents/<T>/<period>/` + registers in `document_index.json`
   + chains into the LLM summarizer)
3. Rebuild the report: `build_report.bat <T> --enable-llm`
