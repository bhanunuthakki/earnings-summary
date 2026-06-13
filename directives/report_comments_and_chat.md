# Report Comments + In-Report Chatbot — design scope

Two new features that turn the workspace report from a one-shot artifact into a
**conversation surface** the analyst can interrogate and steer.

---

## Feature 1: Inline comments

### User-facing flow

1. While reading the report, the analyst clicks **"Comment"** on any element
   that has a `data-commentable` attribute (KPI row, segment definition,
   bear-case failure mode, news item, SayDo bullet, line of business
   overview, etc.).
2. A side panel slides open. The user types a comment ("this KPI is
   immaterial, drop it"; "what's the actual ARPAC trend by cohort?";
   "Brazilian tax-step-up framing is wrong — phase is 3 years not 2";
   "should this section also include FGTS-secured originations as a
   sub-line?").
3. Comment is persisted to disk immediately. A pin marker shows on the
   element, with hover-preview of the comment text + status (`open` /
   `addressed` / `dismissed`).
4. Later, user runs **"Process comments"** which loops through every open
   comment, takes the appropriate action (edit thesis JSON, refresh
   section, re-call LLM with the user's note in context, etc.), then marks
   each comment as `addressed` with a one-line resolution.
5. **"Clear comments"** wipes the slate after the user has reviewed
   resolutions.

### Architecture (4 layers, clean FE/BE split)

**Storage (BE)** — `data/report_comments/<TICKER>.json`. One file per
ticker, single source of truth. Each comment is:
```json
{
  "id": "cmt_2026-05-18_a3f1",
  "ticker": "NU",
  "anchor": {
    "type": "kpi_ledger_row",
    "selector": {"kpi_name": "Monthly ARPAC (USD)"},
    "tab": "thesis",
    "report_date": "2026-05-18"
  },
  "selected_text": "Monthly ARPAC (USD) ... break_condition: ...",
  "comment": "this KPI is immaterial after the SuperCore split, drop it",
  // closed vocabulary — see "Intent taxonomy" below. ALWAYS includes needs_triage.
  "intent": "drop_kpi" | "edit_thesis" | "edit_structured" | "extract_kpi"
          | "curate_peers" | "ask_question" | "fix_data" | "rewrite_section"
          | "platform_change" | "needs_triage" | null,
  "status": "open" | "addressed" | "dismissed",
  "created_at": "2026-05-18T22:14:00Z",
  "addressed_at": null,
  "resolution_note": null,
  "follow_up_thread": []  // optional Q&A back-and-forth
}
```

The `anchor` block is the key design choice: instead of fragile DOM XPath,
each commentable element exposes a **structured selector** (KPI name,
failure-mode index, segment name, news item ID). The render layer emits
`data-anchor-type` + `data-anchor-key` attributes; the comment system
stores those, not the XPath. Comments survive re-renders and even
ticker/quarter changes (KPI named "ARPAC" is still "ARPAC" next quarter).

**Renderer (FE-only)** — emit `data-commentable="true"` and
`data-anchor-type=... data-anchor-key=...` on every annotatable element.
A new module `src/report/renderers/workspace_comments.js` (vanilla JS, no
framework) handles:
- Loading `data/report_comments/<T>.json` via a small endpoint OR
  inlining as a `<script type="application/json">` block at render time
- Rendering pins / side panel
- POST-ing new comments to `data/report_comments/<T>.json` via a tiny
  Python HTTP server OR (simpler) via a "Save to clipboard → paste to
  CLI" fallback when no server is running

**Processor (BE)** — `execution/process_report_comments.py`:
```bash
python execution/process_report_comments.py --ticker NU
python execution/process_report_comments.py --ticker NU --dry-run
python execution/process_report_comments.py --all
```

For each open comment, route by `intent`:
- `drop_kpi`: edit `micro_thesis/holdings/<T>.json` to remove the KPI;
  log the diff to `resolution_note`.
- `edit_thesis`: open Opus with the comment + current thesis paragraph
  in context, get a revised version, write it back, log the diff.
- `ask_question`: open Opus with the comment + report context, get an
  answer, write the answer to `follow_up_thread` so the next render can
  show it inline next to the original comment.
- `fix_data`: log the comment as a TODO in `directives/data_fixes.md`
  (some fixes need manual intervention).
- `curate_peers`: steer the comparable-company set (see "Steerable peers"
  below).
- `needs_triage`: the closed-under-no-fit terminal — park the comment for
  human disposition (see "Closed under no-fit" below).
- `null` (intent not classified): run a first-pass Haiku classifier to
  bucket into one of the above, then route.

After processing, the next `--enable-llm` rebuild picks up the thesis/KPI
edits automatically; comment status flips to `addressed` with
`resolution_note` showing what changed.

### Intent taxonomy — closed under no-fit (S5)

The classifier (`execution/process_report_comments.py::_classify_intent`) is
forced-choice over a **closed** vocabulary, but the vocabulary ALWAYS includes
`needs_triage`. This is the "closed under no-fit" rule (Instrument Paradigm §1;
`directives/design_language.md` §10):

- The Haiku bucketer is told it may pick `needs_triage` when NONE of the
  actionable buckets fit — "it is always better to triage than to mis-route."
- The hard fallback for an unparseable / out-of-vocabulary answer is
  `needs_triage`, NOT the old inert `ask_question`.
- `_route_needs_triage` parks the comment in `directives/data_fixes.md` (the
  existing backlog — a dedicated triage panel is deferred to S11) AND the notes
  mirror records it as an open `question` (`notes._INTENT_TO_KIND`), never an
  inert `observation`. The owner's "remove this section unless you select
  better peers" was previously filed as a memo precisely because the classifier
  had no no-fit terminal and the notes mirror collapsed unmappable directives
  to `observation`.

### Steerable peers — `curate_peers` + a re-evaluable override (S5)

The peers panel (`_peer_comp_panel`) carries a `peer_comp` anchor, so a comment
on the comparable set classifies as `curate_peers` and routes to STRUCTURED
artifacts in `micro_thesis/holdings/<T>.json` — not a memo:

- **Pins** APPEND to the existing `competitive_watchlist` (reusing its +3
  "named rival" scorer boost in `p3_data.load_peer_comp`). A pin given as a
  bare TICKER absent from the upstream FMP pool is **injected** into the pool so
  an explicitly-chosen rival the screen omitted still renders.
- **Exclusions** write the new `peer_exclude` field (the one thing the
  watchlist can't express) — `load_peer_comp` drops those rows by ticker or
  name.
- **The conditional** ("remove this section UNLESS you show better peers /
  computed multiples") is modelled as the new `peers_section_override`
  artifact — a persisted, machine-checkable condition, NOT logged text.
  `p3_data.evaluate_peers_override` re-checks it on every build: the panel
  hides while fewer than `min_quality_peers` credible comps qualify (a quality
  peer = a named rival WITH at least one computed multiple) and returns on its
  own once enough are pinned. The system **acts on the condition**.

No full pin/exclude/hidden/note block is invented — only `peer_exclude` and
`peers_section_override` are new (see `directives/holdings_json_schema.md`).

**Clear** — `python execution/process_report_comments.py --ticker NU --clear`
drops all `addressed` + `dismissed` comments. `open` comments survive
clears.

### Key design questions to resolve before coding

1. **Server or no server?** Cleanest UX needs an HTTP endpoint to POST
   new comments. Three options:
   - (a) **Bundled Flask/FastAPI dev server** (`python execution/comments_server.py --ticker NU`) — small, ~100 LOC, runs on `localhost:7421`, the rendered HTML calls it. Best UX. Requires the user to start the server before opening the report.
   - (b) **Localhost file write via `fetch('file:///...')`** — blocked by most browsers' security model. Not viable.
   - (c) **Copy-to-clipboard fallback** — comment dialog gives you a one-line `claude-cli` command with the comment payload encoded; user pastes into terminal. Zero server, but two-step UX.
   Recommend (a) as the default with (c) as fallback. The server can also serve `data/report_comments/<T>.json` so the comment pane stays in sync across browser refreshes.

2. **Per-report anchoring vs cross-report anchoring?** A KPI comment
   probably applies until the KPI is removed from the thesis (cross-
   report). A SayDo comment probably applies only to that quarter's pair
   (per-report). Recommend `anchor.scope: "ticker" | "report_date"` so
   each comment declares its lifetime.

3. **Bidirectional editing — does processing a comment edit the
   underlying source (thesis JSON, holdings KPI), or just produce a
   suggested change for the user to apply?** Recommend "produce a diff,
   apply if `--apply` flag is passed, else dry-run". Same pattern as
   `intake_documents.py --process`. Avoids accidentally trashing the
   holdings JSON.

---

## Feature 2: In-report chatbot (Claude CLI)

### User-facing flow

1. Persistent chat panel docked to the bottom of every tab (collapsible).
2. User asks free-form questions: "what's the GCP margin trajectory if
   capex stays at $185B?", "compare NU's NPL trajectory to MELI Credit",
   "what would Vélez say to my concern about the FGTS regulation?",
   "rewrite the thesis assuming Mexico interchange caps at 1.5%".
3. Chat threads are persisted to `data/report_chats/<T>.json` so
   conversations survive browser refresh and can be re-loaded later.
4. The chatbot has full context of the rendered report (it's literally
   running with the report's `ReportSpec` as system context), can
   reference any KPI / failure mode / SayDo card by name, and can
   propose edits the user can apply with one click ("apply this change
   to the bear case JSON").

### Architecture (3 layers, reuses comment-server scaffolding)

**Chat server (BE)** — extension of the `comments_server.py` from
Feature 1. New endpoints:
- `POST /chat/<ticker>` — appends user turn, calls Claude CLI with the
  report context as system prompt, streams response back via SSE.
- `GET /chat/<ticker>` — returns the persisted thread.
- `POST /chat/<ticker>/apply` — applies a proposed edit (same diff
  mechanism as the comment processor).

The Claude CLI call uses the canonical `claude_cli.py` subprocess wrapper
(per CLAUDE.md), with:
- System prompt: `"You are an analyst assistant for ticker {T}. Here is
  the latest workspace report context: {ReportSpec as compact JSON}.
  Answer based on this context; cite the specific section / KPI / failure
  mode you're referring to. When the user asks you to edit a section,
  return a JSON diff proposal the operator can apply."`
- Streaming via `claude -p --output-format stream-json` so the panel can
  show tokens live instead of waiting for the full response.

**Frontend (FE-only)** — `src/report/renderers/workspace_chat.js`:
- Collapsible bottom drawer (matches existing dark theme)
- SSE-consuming text streaming
- Markdown rendering for code blocks / lists / tables in responses
- "Apply this change" buttons next to LLM-proposed diffs

**Context budget management** — the full ReportSpec JSON is ~50-200KB.
For each chat turn:
- First turn: full ReportSpec in system prompt (~20K tokens)
- Subsequent turns: a compact summary (KPI ledger + failure modes +
  current valuation multiple + last 3 SayDo cards) + the conversation
  thread
- User can pin specific tabs/sections to keep in context via "@earnings"
  / "@bear-case" mentions

### Key design questions

1. **Where does the chat run?** Same `comments_server.py` extension is
   simplest. Could also be a separate `chat_server.py` for cleanliness.
   Recommend single server, multiple endpoints — the FE complexity is
   already manageable, no point splitting BE.

2. **Billing model** — every chat turn is a Claude CLI call. Per CLAUDE.md
   this routes through subscription (Pro/Max). At 1-5K-token turns,
   subscription handles this fine. For paid API mode, surface a token
   counter in the panel so the user can see what they're spending.

3. **Persistence — full thread or summarized history?** Recommend full
   thread up to N=20 turns, then auto-compact older turns to a summary so
   context budget stays bounded. Compact via a separate LLM call.

4. **Tool use — should the chatbot have read-access to the database, the
   holdings JSONs, the raw transcripts?** If yes, it becomes a true
   research assistant ("look up NU's actual Q2 2025 NPL ratio from the
   transcript"). If no, it's limited to whatever's in the ReportSpec
   context. Recommend **yes** with a constrained file-system tool that's
   scoped to `data/`, `micro_thesis/`, `.tmp/`, `transcripts/` — same
   filesystem visibility as the `Explore` agent has.

---

## Implementation cadence

If both features are greenlit, recommended order:

**Phase 1 — Comments anchor infrastructure** (~4 hours)
- Add `data-commentable` + `data-anchor-type/key` to every annotatable
  element in the renderer (KPI rows, failure mode cards, news items,
  SayDo bullets, segments, geographies, line items)
- Build `data/report_comments/<T>.json` schema + reader/writer in
  `src/comments.py`
- Build the JS sidebar UI in `workspace_comments.js` (read-only at first
  — pins + side panel + display; no posting yet)

**Phase 2 — Comments POST + processor** (~4 hours)
- Build `execution/comments_server.py` (Flask, ~150 LOC)
- Wire JS to POST new comments
- Build `execution/process_report_comments.py` with the 4 intent routers
- Smoke-test on 5 representative comments across NU + GOOG

**Phase 3 — Chat panel** (~6 hours)
- Add streaming endpoint to `comments_server.py`
- Build `workspace_chat.js` (drawer + SSE consumer + markdown render)
- Wire ReportSpec → system prompt assembly
- Persist threads to `data/report_chats/<T>.json`

**Phase 4 — Apply-diff workflow** (~3 hours)
- Diff proposer in LLM response format
- Apply button in the FE
- Apply handler in the BE that does the actual file edit + logs

**Total: ~17 hours** of focused work, split into 4 PR-sized chunks. Each
phase is independently shippable — phases 1+2 alone deliver real value
even without the chatbot.

---

## Open questions for the user

1. **Server requirement** — OK to require running a small `localhost:7421`
   Flask server alongside the browser? Or do you want the zero-server
   "copy → paste into CLI" fallback as primary UX?
2. **Comment lifetime** — are comments per-report (this build only) or
   per-ticker (carry forward until KPI is removed)?
3. **Chat scope** — should the chatbot have read access to raw transcripts
   and the SQLite DB, or stay sandboxed to the rendered ReportSpec?
4. **Phasing** — ship Phase 1+2 (comments) first and validate UX before
   building Phase 3+4 (chat)? Or build all four in one push?
