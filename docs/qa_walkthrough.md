# QA walkthrough — every surface, every action

**Mapped against:** `origin/main` @ `c46a21c` (2026-07-04). Main merges ~10 PRs/day — spot-check line refs against HEAD before filing renderer bugs.
**How this doc was built:** 9 read-only mapping agents over the actual renderers/routes/client JS; copy strings are exact and safe to assert on.
**Want the human version first?** Read [`guided_tour.md`](guided_tour.md) — a plain-words, sit-beside-you demo of a normal week in the app, with the *why* woven in. This doc is the exhaustive click-by-click manual; the tour is the story. Read the tour, then come here when you want every button.

## How to use
- Walk Parts in order; Part 0 lists the env flags, services, and data prerequisites each surface needs — set up once.
- **Pri** column: `P0` = core loop (capture -> coach -> review -> calibration + money-path rendering); `P1` = important; `P2` = polish. A P0 failure blocks; file P2s in bulk.
- Every surface section lists **States to verify** — empty/starved/thin-DB renders are first-class contracts here (the design language mandates honest empty states, never blank or 500).
- Two runtime processes serve everything interactive: the **:7421 comments server** and the **capture poller** (Telegram). Both are NSSM services on the owner box and **require a restart after pulling src/ changes**. Per-ticker reports are **build artifacts** — regenerate with `execution/build_artifacts.py` after a pull or the workspace Part reflects the old build.
- Appendix A lists defects that are already known and chipped for fix — verify their status but do NOT file them as new findings.

## Contents

- [Part 0 — Cross-cutting preconditions (read first)](#part-0-cross-cutting-preconditions-read-first)
- [Part 1 — Command Center shell (global chrome, router, overlays)](#part-1-command-center-shell-global-chrome-router-overlays)
- [Part 2 — Home: open-loops band, cockpit, inbox rail, /feed](#part-2-home-open-loops-band-cockpit-inbox-rail-feed)
- [Part 3 — Companies panels (Holding, Discovery, Diet, Journal, Triage)](#part-3-companies-panels-holding-discovery-diet-journal-triage)
- [Part 4 — The Ledger panel (capture, On My Mind, Worldview, Research, Reconcile)](#part-4-the-ledger-panel-capture-on-my-mind-worldview-research-reconcile)
- [Part 5 — Portfolio panels (Decisions, Risk, Triggers, Memos, lifecycle)](#part-5-portfolio-panels-decisions-risk-triggers-memos-lifecycle)
- [Part 6 — System / Provenance console, Settings, Actions](#part-6-system-provenance-console-settings-actions)
- [Part 7 — Per-ticker workspace report (build artifact)](#part-7-per-ticker-workspace-report-build-artifact)
- [Part 8 — Telegram bot (capture, coaching, callbacks)](#part-8-telegram-bot-capture-coaching-callbacks)
- [Appendix A — Known open issues](#appendix-a--known-open-issues-verify-dont-re-file)


---

# Part 0 — Cross-cutting preconditions (read first)

## Environment flags & gates
**Reach:** process environment of whichever process renders the surface — the `:7421` comments server, the capture poller scheduled task, or a one-off `python execution/...` build. No UI; flags are read at call time via `os.environ.get`. **Preconditions:** none (all have code defaults).
**Renders (top→bottom):** not a visual surface — this is the gate matrix every other surface inherits. Flag inventory (grounded in `src/`):

| Flag | Default | What it gates | QA note |
|---|---|---|---|
| `LEDGER_RESEARCH_TAP` | **on** (`"1"`; off = `0/false/no/""`) | wondering-detect tap on captured musings → inert research chips (`src/research/proposals.py:45`) | regex pre-gate keeps it cheap; only wondering-shaped musings hit the LLM |
| `LEDGER_RESEARCH_RUN` | **off** (`"0"`) | the expensive web research pass — both the run route and the "Research it" button (`proposals.py:55`, `ledger_panel.py:507`) | flag-off must hide/inert the button, not 500 |
| `LEDGER_ONMYMIND` | **off** in code (`onmymind/feed.py:39`) | On My Mind feed section + ladder verbs (`dismiss/save/discuss/incorporate/worldview`) | LIVE in prod env — QA must test both env states; renderer of record is `ledger_panel.py:826` |
| `LEDGER_WORLDVIEW` | **off** (`worldview_panel.py:29`) | Worldview section inside Ledger panel: Tenets list, approval queue, tensions, "add a Tenet" box, "Distill from flagged musings" | returns `""` when off — Ledger tab must look unchanged, no gap |
| `LEDGER_WORLDVIEW_ANCHOR` | **off** (`llm/anchors.py:543`) | Tenet-anchor injection into LLM prompts (socratic, chat_session, workspace_data, anchors) | prod has 0 Tenets → anchor inert even when on; verify no prompt bloat when empty |
| `LEDGER_ARTIFACT_BRIEF` | **on** (`capture/poller.py:170`) | auto web-fetch + LLM brief when the intent tap stamps `engage_intent` on a captured URL/artifact musing | this is the kill switch for capture-time web/LLM spend; failure must never affect capture (fire-and-forget) |
| `REVIEW_FULL_VERDICT` | **on** (`ask/commands.py:43`) | `/review <TICKER>` full verdict body | off → shortened output; verify Telegram copy both ways |
| `ANALYST_NOTES_SYNC` | **on**; `=0` disables (`comments.py:349`) | comment→analyst_notes mirror sync | off silently skips sync — FTS search of musings misses new comments |
| `CAPTURE_TELEGRAM_TOKEN_FILE` | unset (`token_store.py`) | overrides bot-token path (default `data/secrets/telegram_bot_token`) | token file absent → poller exits 0 "not configured", not a crash |
| `CAPTURE_WHISPER_MODEL` | default model (`transcribe.py:46`) | voice-note transcription model | ffmpeg auto-prepended to PATH from `C:\ffmpeg\bin` |
| `CLAUDE_WEB_MAX_BUDGET_USD` | `2.0` (`llm/cli.py:566`) | hard ceiling per web-enabled Claude call; per-call `max_budget_usd` can only lower it | |
| `CLAUDE_CLI_TIMEOUT_SECONDS` | `1200` | CLI transport subprocess timeout | |
| `LLM_FALLBACK_DISABLED` | unset (`llm/fallback.py:60`) | disables Gemini fallback path | Gemini credentials must also be configured externally; no prod purpose routes there (dormant) |
| `GEMINI_BACKEND_PURPOSES` | empty (`gemini_backend.py:153`) | which purposes route to Gemini backend | |
| `OPENROUTER_API_KEY` / `OPENROUTER_PROVIDER_ONLY` / `OPENROUTER_DATA_COLLECTION` | key live; collection `deny` | OpenRouter third backend | |
| `LLM_CAPTURE_DIR` / `LLM_CAPTURE_PURPOSES` | unset (`llm/capture.py`) | prompt/response capture-to-disk tap | QA can use this to snapshot prompts without prod side-effects |
| `FMP_API_KEY` | unset in some contexts (`peer_selection.py:437`) | live FMP calls | missing key → WebSearch+Opus fallback for stage-0 news |
| `PORTFOLIO_TRACKER_API_URL` / `PORTFOLIO_TRACKER_URL` | baked defaults | tracker client + command-center tracker links | |
| `COMMENTS_SERVER_CORS_WHITELIST` | unset | extra allowed Origins on :7421; default echoes only `null` (file://) + loopback | cross-site Origin must get **no** CORS header |
| `EDGAR_USER_AGENT`, `DCF_GSHEETS_CREDENTIALS/TOKEN`, `SOURCE_COST_PER_CALL_USD`, `CIO_USER_ID` (`"bhanu"`), `LOG_LEVEL`/`LOG_FORMAT` | various | SEC watch, Sheets round-trip, source-cost accounting, identity, logging | peripheral to UI QA |

Budget gating is **DB, not env**: `llm_budgets`/`llm_budget_alerts` (migration 0052) enforced pre-flight in `llm_client._call_claude`; fresh repo with no migrations returns "allowed". Bypass is the `force_budget_bypass=True` kwarg, not an env var. Hard block surfaces in reports as `SectionStatus.BUDGET_SKIPPED`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Flip a `LEDGER_*` flag | set env, **restart the owning process** (poller for capture-time flags; :7421 for panel routes; rebuild for build-time HTML) | gated section appears/disappears; gated routes 404/no-op | editing env without restart changes nothing — flags read per-call but processes inherit env at start | P0 |
| `LEDGER_RESEARCH_RUN=0` + click "Research it" | Ledger panel proposal chip | button absent or inert; POST route refuses; no research $ spent | must not 500 | P0 |
| `LEDGER_WORLDVIEW=1`, 0 tenets | open Ledger tab | Worldview section renders with empty-state + "add a Tenet" box | approval queue empty; no crash on empty `tenets` table | P1 |
| `LEDGER_ARTIFACT_BRIEF=0`, send a URL to the bot | Telegram capture | musing lands, **no** brief pushed back | brief fetch/LLM failure with flag on must still land the capture | P0 |
| Set purpose budget to $0 in `llm_budgets` | run a report build | section renders BUDGET_SKIPPED copy, build completes | budget-table missing → allowed (degrade-open) | P1 |

### States to verify
- Every flag-off state renders `""`/absent, never a broken fragment or 500.
- Prod-vs-code default divergence: ONMYMIND/WORLDVIEW/WORLDVIEW_ANCHOR are 0 in code but LIVE in prod env — QA env must mirror prod to see the real surface set.
- Budget enforcer on a migrations-less DB: calls allowed, DEBUG log only.

## Services & processes
**Reach:** :7421 → `python execution/comments_server.py` (Flask, loopback). Poller → Windows scheduled task `cron/capture_poller.task.xml` running `execution/capture_poller.py`. Scheduled fleet → `cron/*.task.xml` (UTF-16; edit via Python, and editing the XML ≠ the live registered task). **Preconditions:** Flask installed; `data/secrets/telegram_bot_token` for the poller; tasks registered in Windows Task Scheduler.
**Renders (top→bottom):** :7421 serves every request-time surface: `POST/GET/PATCH/DELETE /comments`, `POST /chat/<ticker>` (+ `/apply`), `POST /api/ask` + `/api/ask/stream` (SSE), `/api/panel/<name>` lazy panel fragments, `/api/peek/*`, `/api/tenets`, `/socratic/<T>`, `/review` doorways, `/actions/<name>` + `EventSource /actions/stream/<id>`, `/source/<doc_id>` viewer, `GET /healthz`. The capture poller long-polls Telegram: ingests musings/voice (Whisper), runs the intent tap, artifact brief, wondering-detect, `/review <T>` command, coach-ping callbacks (`cp:review:`/`cp:dismiss:`), ledger jump-chips. **Both are long-lived Python processes: edits under `src/` require a restart to take effect (PR #815 lesson — poller especially).** Scheduled tasks that feed QA surfaces: `run_morning_pipeline` (04:00 window — keep 03:00–05:00 PT clear; ends by running `execution/verify_daily_chain.py`, which writes `.tmp/daily_chain_status.json` — the source of the shell's System-icon status dot, `command_center_shell.py:444`), `coach_pings` (daily governed-initiation pass, zero-LLM, ≤DAILY_CAP pings with Dismiss/Answer buttons; idempotent per moment), `ledger_synthesis`, `weekly_p2_lens_refresh` (lens artifacts incl. five_min_reread), `monthly_calibration_scorecard` (writes `data/calibration_scorecard/<period>.json`), `grade_calibration`, `backup_db`, `refresh_dirty_artifacts`, `daily_fetch_and_brief`, `weekly_score_stances`. `verify_cron` audits registration; `cron_health_panel` renders `ingestion_runs` dots (green/red/grey per day, `backup_db` + `run_morning_pipeline` pinned first).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `GET http://localhost:7421/healthz` | curl/browser | 200 health JSON | server down → every workspace comment/chat/ask affordance degrades (see next section) | P0 |
| Start poller manually | `python execution/capture_poller.py --once` | drains one Telegram batch, prints counts to stderr, exit 0 | token file absent → "not configured (…); exiting cleanly", exit 0 | P0 |
| Restart poller after `src/capture/*` edit | Task Scheduler restart / kill+rerun | new code active | stale poller silently runs old ingest logic — classic QA trap | P0 |
| Run pipeline verifier | `python execution/verify_daily_chain.py` | writes `.tmp/daily_chain_status.json`; exit code = verdict | file absent/unparseable → shell renders System button with **no dot** (not an error) | P1 |
| Run coach pings | `python execution/run_coach_pings.py --dry-run` | collects+gates moments, sends nothing | rerun same day: moments considered exactly once (no dup pings) | P1 |
| CORS probe | request with `Origin: https://evil.example` | response has no `Access-Control-Allow-Origin` | `null`/loopback Origins echoed back | P1 |
| Ping "Answer: review NU" button | Telegram inline keyboard `cp:review:<id>` | in-place `/review NU` verdict; falsifier_breach class only | other moment classes: Dismiss-only keyboard | P1 |

### States to verify
- :7421 down: workspace opens fine (static HTML) but comments/chat/DCF-edit/ask affordances fail gracefully.
- Poller down: Telegram messages queue server-side; next poll drains them (offset file `data/capture/telegram_offset.json`).
- `.tmp/daily_chain_status.json` stale (yesterday's): dot tone reflects the artifact, not live state — acceptable by design; absent → no dot, no crash.
- Cron-health panel with empty `ingestion_runs`: grey dots + note block, no 500.

## Build artifacts vs served
**Reach:** per-ticker workspace reports = static HTML files under the artifacts tree, opened via **file://** (regenerated by `run_morning_pipeline` daily, `refresh_dirty_artifacts`, or manually via `execution/build_artifacts.py` — the `--enable-llm` sweep playbook); the command-center dashboard shell (`build_analytical_dashboard.py`) is likewise build-time static HTML. Request-time surfaces live only on :7421. **Preconditions:** a completed build for BUILD-time surfaces; :7421 for everything interactive.
**Renders (top→bottom):** BUILD-time: workspace 6-tab report (Overview/Quarter/Financials/Research/Position/Sources), analytical dashboard shell with home band + lazy panels, ledger/worldview/coach/calibration/evals/cron-health panel HTML, DCF sheets. Each report inlines a `workspace-boot` JSON blob with `server_url: http://localhost:7421` (`workspace_sections/boot.py:39`) — the static page then *phones* the server for everything live. Request-time (dies without the server): comment create/list/patch/delete, chat + apply-diff, Ask tab (`/api/ask`, SSE stream), panel lazy-loads (`/api/panel/<name>` — only the first sub-tab is inlined at build; every other sub-tab is an empty placeholder until activation, `command_center_shell.py:301`), peek popovers (`data-peek-url` → `/api/peek/*`), tenet writes (`/api/tenets`), action buttons + SSE progress, socratic memo pages (`/socratic/<T>` is a server route — the report links to it at `boot.py:133`), `/review` doorways, source viewer (file:// viewer hrefs `/source/<doc_id>` need the server, `workspace_chat.py:276`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Open report via file:// with server up | double-click HTML | tabs render from inlined data; comments/chat/dcf widgets hydrate from :7421 | `Origin: null` must be CORS-accepted | P0 |
| Same with server down | kill :7421, reload | static content fully readable; live widgets show fetch-failure state, not blank page | no console-error cascade breaking tab JS | P0 |
| Activate a non-default dashboard sub-tab | click tab | XHR to `/api/panel/<name>`, fragment injected | server down → placeholder/error copy; double-click must not double-inject | P0 |
| Rebuild after data change | `python execution/build_artifacts.py …` (match `--flavor` to list_type, keep `--enable-llm`) | fresh HTML; stale report otherwise | thesis edits need `run_thesis_evaluator.py` first — build alone won't re-encode | P1 |
| Peek hover/click | element with `data-peek-url` | popover from `/api/peek/*?fragment=1` | offline → silent fail or inline error, no page jump | P2 |

### States to verify
- Build-time surfaces are **stale by design** between pipeline runs — QA must check the `updated …` stamp (`stamp_html`) rather than assume liveness.
- file:// report with no server: zero uncaught JS exceptions.
- Preview-MCP note: CSS-transition UI makes `preview_screenshot` time out; verify via DOM eval instead.

## Data prerequisites for a full QA pass
**Reach:** `data/portfolio.db` (prod SQLite) + file artifacts (`data/calibration_scorecard/<period>.json`, `.tmp/daily_chain_status.json`, `data/holdings` JSON, `data/ir_narrative/<T>/`). **Preconditions:** migrations at head (alembic); a fresh/thin DB is a *supported* state — most surfaces deliberately render starvation stubs (PR7/PR8 shipped explicit starvation states) rather than 500.
**Renders (top→bottom):** to light **every** surface the DB needs: **graded decisions** (decision rows + grades from `grade_calibration` — feeds Coach P&L, calibration scorecard panel, receipts); **coach_pings rows** (from `run_coach_pings` — feeds pings/mutes/digest rendering in the coach panel and Telegram); **research proposals** (`research_tasks` in proposed/drafted states — feeds Ledger proposal chips + 4-action row); **tenets** (0 in prod — Worldview list, approval queue, and the anchor all need ≥1 row incl. one `proposed` for the queue); **armed falsifiers** (decision_conditions with `signature_key_evidence` — feeds the armed-falsifier table, falsifier_breach ping class, /review doorways); **scorecard file** `data/calibration_scorecard/<period>.json` (panel renders the latest; also the rubric-judge corpus); **five_min_reread lens cache** (cached in the `llm_artifacts` table keyed by input-sha; populated by `weekly_p2_lens_refresh` or an `--enable-llm` build — cold cache means the reread section is absent/regenerating); **dcf_runs with `is_latest=1`** (valuation section, over_under_pct, freshness view `v_decision_freshness`); plus musings (`analyst_notes` — with FTS triggers intact, see the `batch_alter_table` gotcha: broken triggers make `search_musings` silently return `[]`), `ingestion_runs` (cron-health dots), `llm_calls` (budget panel/optimizer), KPI `financial_facts` (cockpit columns need ≥2 facts ≤today per tier_1_break def), holdings JSON ⇄ `thesis_state` mirror in sync.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Seed a full-QA DB | run pipeline once + `grade_calibration` + `run_coach_pings` + `monthly_calibration_scorecard` + one `--enable-llm` build + a DCF build | every panel lights with real rows | partial seeding → each unlit panel must show its named starvation stub, not blank | P0 |
| Empty-DB smoke | fresh migrated DB, open dashboard + one report | all panels render empty states; zero 500s across `/api/panel/*` | portfolio_risk panel previously 500'd on thin DBs (hotfixed) — regression-watch | P0 |
| Tenet queue QA | insert one `status='proposed'` tenet | Worldview approval queue shows one-tap approve/reject | 0 tenets → anchor injects nothing even with flag on | P1 |
| Scorecard-absent QA | remove/rename `data/calibration_scorecard/*.json` | scorecard panel renders its no-scorecard-yet state | must not crash rubric-judge corpus loaders | P1 |
| dcf_runs-absent QA | ticker with no run | valuation renders MISSING/stale copy; over_under_pct blank | mixed-convention over_under (bank/holdco writers diverged) — verify sign | P1 |
| FTS integrity check | `search_musings` for a known captured phrase | hit returned | silent `[]` = dropped fts triggers; recreate 3 + rebuild | P1 |

### States to verify
- Deliberate starvation renders (no data, by design): Coach P&L before any graded decision; receipts before any approve/ratify/dismiss; digest with zero pending pings; On My Mind with zero musings; Worldview with 0 tenets; reread section on cold lens cache; System dot absent before first `verify_daily_chain` run.
- Absence of 500s on every `/api/panel/<name>` against both empty and thin DBs.
- TTM caveat: no TTM rows exist in prod — any TTM-dependent render must compute by summing 4 quarters, not read a view.
- Naive-UTC convention: seeded test rows must be naive-UTC or datetime comparisons crash.

---

# Part 1 — Command Center shell (global chrome, router, overlays)

## Topbar (global chrome)

**Reach:** `GET http://localhost:7421/` (execution/comments_server.py `dashboard_page`, renders `render_shell` from src/pipeline/command_center_shell.py). **Preconditions:** comments_server running (`python execution/comments_server.py --port 7421`); `data/portfolio.db` present for cockpit rows; System status dot needs `.tmp/daily_chain_status.json` (written by `execution/verify_daily_chain.py`) — absent file = no dot, not an error.

**Renders (top→bottom):** Sticky bar: brand "Command Center" → primary section nav tabs **Home / Companies / Ask / Portfolio** (System excluded — it's a utility icon) → link "Alert feed" (`/feed`) → `⌘K` button (title "Jump to a ticker, tab, note, or saved view (Ctrl+K / Ctrl+Space)") → System `▦` button (title "System · Provenance" + status summary; optional dot `.cc-system-dot-{ok|warn|bad}`) → `✎` notes button (title "Quick note + open notes (scoped to the open holding)") → `◑` theme toggle → "⚙ Settings" button → "updated …" stamp. Below: exactly one sub-tab row for the active section (Companies: Holding·Discovery·Diet·Journal·Triage·Ledger; Portfolio: Performance·Risk·Synthesis·Decisions·Memos·Triggers); single-sub-tab sections (Home/Ask/System) render no row (`data-single="1"`). Then `.cc-panels` with the server-inlined Overview and hidden lazy panels. Also boots: skip-link "Skip to content" (`#cc-main`), `#cc-live` aria-live region, offline banner "Offline — data panels cannot reload until you reconnect".

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Section tab (Home/Companies/Ask/Portfolio) | click / arrow-key roving tabindex (Left/Right/Home/End activate on focus) | hash set to theme's last-active sub-panel else first (`#overview`, `#holding`, `#explore`, `#portfolio`); sub-row swaps | hover ≥80ms prefetches landing panel fragment into sessionStorage cache | P0 |
| Sub-tab button | click | `location.hash = '#'+panel_id`; panel lazy-loads via `GET /api/panel/<name>` on first activation, skeleton (`_SKELETON_KINDS` shape) shown meanwhile, aria-announce "<pid> loading…"/"ready" | fetch TypeError retried 3× (200/400/600ms); persistent failure renders "Couldn't reach the server — it may still be starting." or "Failed to load (HTTP n)." + **Retry** button | P0 |
| SWR panel cache | revisit a loaded tab / reload | cached fragment paints instantly; background `If-None-Match` revalidate (server ETags every `/api/panel/`); 304 = no swap; fresh fragment never swapped while focus is inside an input/textarea/select in the panel | timing samples POST `/api/metrics/panel` (in-memory ring, max 500), visible in System console | P1 |
| "Alert feed" link | click | full navigation to `/feed` | hidden ≤900px viewport (also stamp) | P1 |
| System `▦` | click | activates System section → Provenance panel (`#provenance`); dot tone: ok=last chain run ok, bad=failed/missing ("Morning pipeline has not run today"), warn=unreadable artifact | no `.tmp/daily_chain_status.json` → button renders with no dot | P1 |
| `◑` theme toggle | click | flips `data-theme` dark⇄paper on `<html>`, persists via CCState key `theme` | boot honors stored theme; default dark | P2 |
| ⚙ Settings | click | opens Settings drawer (see Notes/Settings drawers below); click again closes | — | P1 |
| Offline banner | browser offline event | banner unhides; online hides it | — | P2 |
| Skip link | Tab from page top, Enter | focus jumps to `#cc-main` | visible only on :focus-visible | P2 |

### States to verify
- Fresh checkout, pipeline never run: no System dot, page still 200s.
- Empty DB: cockpit/inbox/open-loops bands render empty-safe (open_loops "never raises on a thin DB"); no 500 on `/`.
- ≤900px: stamp + Alert-feed link hidden; sub-rows scroll horizontally.
- Only ONE sub-row visible at a time (regression: "four stacked menus" if `.cc-tabs[hidden]` CSS lost).

## Hash router, legacy redirects & the Holding view

**Reach:** `/#<panel>` for every panel id (`#overview #holding #discovery #diet #journal #triage #musings #explore #portfolio #portfolio_risk #portfolio_synthesis #decisions_record #advisor_memos #holdings #provenance`); ticker form `/#holding=NU`. **Preconditions:** none beyond server.

**Renders:** `parseHash()` splits on first `=`; `REDIRECTS` (mirrors `_LEGACY_PANEL_REDIRECTS`) remaps: `prereads/insiders/predictions→overview`; `decisions/thesis_ledger→decisions_record`; `budget/actions→provenance` **and auto-open the Settings drawer** (`DRAWER_OPENERS`); section names `home/companies/ask/system→overview/holding/explore/provenance`; all 8 old diagnostics ids + `model_eval→provenance`; ritual aliases `#ledger→musings`, `#triggers→holdings`, `#health→provenance`. Unknown hash falls back to `#overview`. Hash-less reload restores last section/tab/ticker from CCState (session-scoped; `location.replace` keeps Back clean). hashchange also closes any open peek + hovercard.

`#holding=<T>`: Holding panel fetches `/api/panel/holding?ticker=T` and, while a ticker is open, the **Companies sub-row is suppressed** (`holdingOpen && theme==='companies'`). The fragment (src/pipeline/ticker_command_center.py `_holding_band`) is one ~40px utility band above the embedded `/reports/<T>` iframe: left = type-ahead combobox `.cc-combo` (value = bare ticker, muted company-name overlay hidden on focus; placeholder "Search holdings — ticker or name…"; list from `/api/tickers` on first focus); right = identity badges + freshness dot `●` (worst-of build/FMP age: ok ≤7d, warn ≤21d, bad >21d/never; click peeks `/api/peek/provenance?ticker=T`, real href `/#system`) + link row **"Report ↗ · DCF ↓ · Review · [Tracker ↗ ·] Ledger"** + "⚙ Ops" + "✎ Notes" buttons.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Legacy hash | navigate `/#budget` | lands on Provenance panel AND Settings drawer opens | `#actions` same; ticker part of a redirected hash discarded | P1 |
| Alias hash | `/#ledger`, `/#triggers`, `/#health` | musings / holdings / provenance panels | palette shows canonical ids only | P1 |
| Combobox select | type + click / Arrow+Enter | sets `#holding=<T>`; shell re-fetches fragment server-side (skeleton `band` re-shown on ticker switch) | no-ticker state (`render_holding_picker_band`): combobox + hint "Search a ticker or name to open a holding." | P0 |
| Ticker link anywhere | click `a.ticker-link` / `td.ticker a` | intercepted → `#holding=<T>` (no navigation) | links inside report iframe unaffected (separate document) | P0 |
| "Report ↗" | click | new tab `/reports/<T>` | — | P0 |
| "DCF ↓" | click | navigates `/dcf/<T>` | — | P1 |
| "Review" | click | peeks `/api/peek/review/<T>` ("Position review · T": pre-analysis + graded-sells base rate + escalation to full LLM review); middle-click keeps `/ticker/<T>` | thin DB: peek should render degraded, not 500 | P0 |
| "Ledger" | click | `#musings` — the doorway back while Companies sub-row is suppressed | — | P1 |
| Freshness dot | click | peeks provenance card (ages + inline refresh) | never built → red dot, tooltip "Never built · No FMP pull" | P1 |
| "⚙ Ops" | click | opens Ops drawer: 5-min reread, position-lifecycle timeline, attribution, Refresh ("Refresh" / "Run anyway (ignore caps)" → POST `/actions/refresh`, SSE log link), persistent bypass toggle (`/api/ticker-settings/<T>`), DCF⇄Sheets export/import, analyses log, artifacts | Sheets buttons surface job error when Google creds absent | P1 |
| "✎ Notes" (band, `data-cc-notes-open`) | click | opens the SHARED shell notes drawer, ticker-scoped | delegated listener — works from lazily-injected fragments | P1 |
| Back/forward | browser buttons | hashchange re-activates panels; peek/hovercard closed | double hashchange (boot replace) is idempotent | P1 |

### States to verify
- Unknown hash `#zzz` → Overview, no console error.
- `#holding` with no ticker → picker band, Companies sub-row visible.
- `#holding=NU` → sub-row hidden; switching to Discovery restores it.
- Combobox with unknown ticker text: no crash; list filters to nothing.

## Command palette

**Reach:** Ctrl/Cmd+K, Ctrl+Space (`ev.code==='Space'`, layout-independent), or the `⌘K` topbar button. Same keystroke toggles closed. **Preconditions:** none; corpora degrade independently.

**Renders:** modal dialog (`#cc-palette`, CCOverlay PRIORITY.PALETTE=50, group `cc-primary`, pop motion, scrim, focus-trap, corner ×): input placeholder "Jump to a ticker, tab, note, or view — or just ask…" + listbox. Corpus filled on every open: sections + all sub-tabs (hint = theme name), static actions "Settings & maintenance" (opens drawer), "Alert feed" (`/feed`), "Export CIO workbook" (`/export/cio`); then async: tickers from `/api/tickers` (two-part mono symbol + muted name row, runs `#holding=<T>`), open notes from `/api/notes?status=open` (label `✎ <first 64 chars>`, hint `note · <T>`, runs `#journal`), saved views from `/api/views` (label `▤ <name>`, runs stash `askViewId` → `#explore` + `cc-view-id` event). Failed corpus fetches silently omit that slice.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Filter | typing | prefix match scores 3, substring 2, max 12 rows; empty query lists all | no matches → "No matches." | P0 |
| Ask fallback | query ≥3 chars | appended last row `Ask: "<q>"` (hint "ask") → stashes CCState `askQ`, jumps `#explore`, dispatches `cc-ask-q` | explore panel consumes stash at wire-up if loading lazily | P0 |
| Navigate | ArrowUp/Down, Enter | selection moves (aria-activedescendant), Enter runs item after closing | click on row runs it too | P0 |
| Dismiss | Esc / scrim click / corner × | closes; focus restored to opener | opening palette closes any open drawer (group exclusion) | P0 |

### States to verify
- Server-down corpora: static entries still work.
- Palette over an open peek: Esc closes palette first (priority 50 > 40), second Esc closes peek.

## Capture tray (Ctrl/Cmd+.)

**Reach:** Ctrl/Cmd+`.` from any tab (toggle). **Preconditions:** POSTs `/api/capture/text` (ingest itself is LLM-free, channel `tray`); wondering-chip creation requires `LEDGER_RESEARCH_TAP` on (`tap_enabled()`); pledge challenge/receipt from `research.pledge` (failures swallowed — capture never breaks). **Cost note (QA-verified 2026-07-04):** a PLEDGE-SHAPED capture is NOT free — `detect_and_capture_pledge` routes through `extract_decision` → `call_llm_structured(purpose='musing_decision_extract')` (one governed metered LLM call per pledge); only the challenge reply itself (`build_challenge`/pre-analysis) is LLM-free.

**Renders:** modal pop dialog (`#cc-capture-tray`, PRIORITY.PALETTE, group `cc-primary`): head "Capture" + optional muted `$TICKER` chip + corner ×; textarea placeholder "Think out loud — lands in the Ledger. (Ctrl+Enter to capture)"; row: primary button "Capture" + status span; coach mount below.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Ticker prefill | open while Holding tab active with ticker | chip shows `$T`; textarea pre-filled `"$T "` only if empty | not on Holding tab → no chip/prefill | P1 |
| Capture | Ctrl+Enter in textarea or "Capture" click | POST text; on success textarea clears, notes drawer reloads if open, tray closes — UNLESS response carries `pledge_challenge` or `annotated_decision_id` (tray stays open) | empty text → msg "write something first"; HTTP error → "error: <e>"; network → "network error" | P0 |
| Challenge card | response has `pledge_challenge` | card with escaped text (newlines→`<br>`), input placeholder "conviction + falsifier — one line completes the record" + "Send" (re-POSTs `/api/capture/text`) + dismiss × | empty annotation refocuses input; Send disabled while in flight | P0 |
| Receipt | response has `annotated_decision_id` | "Noted — recorded on decision #<id>" linking `/#decisions_record` | — | P1 |
| Dismiss | Esc / scrim / × | closes (CCOverlay) | coach card × clears only the card | P0 |

### States to verify
- Tap flag off: capture still lands, `wondering_task_id` null, no chip.
- Double Ctrl+Enter: second POST of empty textarea → "write something first" (no dup).

## ✎ Notes drawer

**Reach:** topbar `✎` button, any `[data-cc-notes-open]` button (Holding band), toggle-click closes. **Preconditions:** `GET /api/panel/notes_drawer[?ticker=T]` re-fetched on EVERY open.

**Renders:** right drawer (`#cc-notes-drawer`, PRIORITY.DRAWER=30, slide-right, group `cc-primary`, head "Notes" + ×): "Quick note" panel — kind `<select>` (default `observation`), ticker input "ticker (optional)" (pre-filled when Holding-scoped, editable), textarea "What did you notice? Enter saves · Shift+Enter for a newline.", **"musing" checkbox** (title "Route to the Ledger capture spine — wondering/pledge taps run.", reroutes save to `/api/capture/text` via `data-musing-endpoint`), "Add note" button + msg span; then open-notes list (ticker-scoped adds that name's recent alerts + brief provenance); footer "Resolve · reclassify · supersede live in Companies → Journal." linking `/#journal`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Add note | button / Enter in textarea | POST `/api/notes` (source manual) → `window.ccReloadNotesDrawer()` refreshes list in place | musing checked → POST `/api/capture/text` instead | P0 |
| Scope | open while Holding tab active | fetch carries `?ticker=T`; ticker input prefilled; alerts section appears | scope only while Holding panel is visible (`p.hidden` check) | P1 |
| Dismiss | Esc / scrim / × | closes; opening Settings drawer or palette closes it (group) | fetch failure → "Failed to load (…)" + Retry | P0 |

### States to verify
- Empty DB / no open notes: list renders empty-state, no 500.
- Tray capture while drawer open reloads the list.

## ⚙ Settings drawer

**Reach:** topbar "⚙ Settings"; auto-opens via `/#budget` or `/#actions`. **Preconditions:** section fragments `/api/panel/{budget,ticker_settings,dcf_globals,actions}`.

**Renders:** right drawer "Settings & maintenance", four collapsed `<details>`: "LLM budgets", "Ticker settings", "Global DCF assumptions", "Maintenance actions & job streams". Each lazy-loads on first open **while drawer visible**; open/closed state persists (CCState `drawer:<endpoint>`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Section toggle | click summary | fragment fetched once, injected with script re-execution (budget Save buttons, actions SSE keep working) | failure → Retry button, `data-loaded` reset | P1 |
| Dismiss | Esc / scrim / × | closes | boot-restored open sections load on drawer open, not at boot | P1 |

### States to verify
- Sections previously open reload correctly on next drawer open after reload.

## Ask dock

**Reach:** always-mounted bottom-right chrome (src/pipeline/ask_dock.py), outside `.cc-panels`. **Preconditions:** `POST /api/ask/stream` (SSE frames session/stage/delta/fragment/final/citations/error); threads via `/api/ask/sessions` CRUD.

**Renders:** `#ask-dock` `data-mode` min|float|split (persisted CCState `dockMode`; boot default min). Head: title "Ask", hint "tables for metric questions · cited answers for open ones", controls `⇆`(threads) `▁`(min) `◫`(split, title "Split view beside the page (Esc exits)") `⇗`(pop-out, "Continue in the Ask tab") `×`(collapse). Body: thread (empty state "Ask about any tracked name without leaving this tab."), input "Ask…" + "Ask" submit. z-index 35; split reflows `.cc-panels` right margin 440px (overlay-only ≤900px).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Pill/head click | click (not on a ctl) | min ⇄ last expanded mode; expanded focuses input | — | P0 |
| Submit | Enter/Ask | user bubble; "working…" card; stage notes ("compiling the view"/"running the view"/"researching"); deltas stream; final = prose w/ inline [n] cite chips + citation-chip row + "⚠ N unverified" chip, or a view fragment inline | error frame → red text "failed — try again"; network → "network error — try again"; no answer → "no answer — try again"; busy guard blocks double-submit | P0 |
| Slash commands | type `/review <T or company name>`, `/help`, `/discovery …` | deterministic reply, no LLM (src/ask/commands.py `COMMAND_PREFIXES`); `/review` resolves company names → ticker | unsupported surface → "Commands aren't available on this surface — try /help in the report chat." | P0 |
| `◫` split | click / again | split column under measured topbar height; registers CCOverlay open; Esc or × exits split→float | scrim:false by design; every shell overlay covers the dock | P1 |
| `×` | click | split→float, float→min (one level) | — | P1 |
| `⇗` pop-out | click | stashes `askThread` (+ pending input as `askQ`), min, `#explore`, `cc-ask-q` event — Ask tab replays turns | — | P1 |
| `⇆` threads | click | overlay over dock body: "Saved threads" + "+ New thread" + rows (title/date/✕); row click resumes (renders turns + citations); double-click title = inline rename (Enter commits, Esc cancels, empty reverts); ✕ deletes (deleting current → new thread) | empty: "No saved threads yet."; fetch fail: "Could not load threads."; Esc closes threads before exiting split (priority DOCK+5) | P1 |
| Session persistence | reload | 12-turn tail replays from CCState `askTail`; `askSessionId` re-attaches server thread | — | P1 |

### States to verify
- Split at ≤900px overlays instead of squeezing panels.
- Esc with palette open never touches dock (priority 10 lowest).

## Peek overlays, hovercard & overlay discipline

**Reach:** any `a[data-peek-url]` left-click; any `a[href^="/source/"]` auto-peeks its `?fragment=1` variant (preserving `#L<n>` anchor highlight); ticker hovercard on hovering `a.ticker-link, td.ticker a, [data-peek-ticker]` (240ms intent, hover-capable pointers only) from `/api/peek/ticker/<T>`. `data-ask-q` elements outside the Ask panel jump-ask on click (`data-fact-ref` wins if both present; `data-panel="explore"` excluded). **Preconditions:** `/api/peek/*` routes.

**Renders:** `#cc-peek` (PRIORITY.PEEK=44/40, rise motion, scrim): head = title, "open full ↗" (real href), × ; body = fetched fragment (scripts re-executed).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Open peek | plain left-click on opted-in link | popover positioned near anchor; href untouched (middle/ctrl/shift/alt-click navigate normally) | fetch fail → "Failed to load (…)"; stale responses dropped via seq guard | P0 |
| In-peek link | click `/source/` or data-peek-url link inside peek | retargets body in place, updates "open full ↗" | source-chip `<details>` popover folded first | P1 |
| Approve/dismiss in peek | click `a[href^="/approve"]` | fetch the GET, re-fetch fragment (status pill updates in place) | 409 (double-click/stale) tolerated — refetch shows truth; other errors prepend "Action failed (…)" | P1 |
| Hovercard | hover ticker 240ms | mini price/verdict/next-ER card; cached per pageload; flips above when near bottom | unknown ticker (fetch error) → card closes silently; scroll closes; click on a link inside closes | P1 |
| Escape ladder | Esc | order: non-modal popovers (hovercard/source-chip/cite-marks) first → then top modal by priority PALETTE(50) > PEEK(40) > DRAWER(30) > DOCK-threads(15) > DOCK(10) — never recency | drawer opened after palette must NOT steal Esc | P0 |
| Scrim click | click `.k-scrim` | closes the visually topmost scrim-requesting surface only | one shared scrim; z-index tracks top surface | P0 |
| Focus trap | Tab/Shift+Tab in modal | cycles within surface; close restores focus to opener | dock exempt (trapFocus:false) | P1 |
| Group exclusion | open palette while drawer open | drawer closes (group `cc-primary`: palette, tray, both drawers) | peek is NOT in the group — can layer over drawers | P1 |

### States to verify
- Esc with hovercard + peek both open: first Esc kills hovercard, second the peek.
- hashchange closes peek + hovercard.
- Reduced-motion: no animations, overlays still open/close.
- Touch device: hovercard suppressed (`hover: none`); 44px touch targets on chrome buttons.

---

# Part 2 — Home: open-loops band, cockpit, inbox rail, /feed

## Open-loops band (Home, ritual-debt strip)

**Reach:** `http://127.0.0.1:7421/` — first element inside the Overview panel, above the cockpit. **Preconditions:** `comments_server.py` running (default `--port 7421`); rendered server-side by `pipeline.open_loops.render_open_loops_band(db_path)` on every `GET /`. No flags to render the band itself; the Tenets line additionally requires `worldview_enabled()`.

**Renders (top→bottom):** One flex row (`.cc-open-loops`, caption size). Non-empty state: muted head "Open loops" followed by one anchor per non-empty queue, in fixed order: `Reconcile: N` → `/#musings`; `Tenets proposed: N` → `/#musings` (only when worldview flag on); `Research proposals: N` (pending) → `/#musings`; `Decisions missing conviction/falsifier: N · oldest Xd` → `/#decisions_record`; `Coach digest: N · oldest Xd` → `/#musings`. Counts render mono (`.cc-ol-count`); the `· oldest Xd` suffix appears only when the oldest row's `created_at` parses and is >0 days old. Empty state: single muted line, exact copy `Ritual clear - nothing waiting on you.`

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Reconcile / Tenets / Research proposals / Coach digest line | click | Shell hash-routes to `#musings` (Ledger tab); no server call | Hash router must treat these as panel ids; broken if panel renamed | P0 |
| Decisions line | click | Routes to `#decisions_record` (Portfolio > Decisions panel) | — | P0 |
| Line hover | hover | Text color → `--accent` | — | P2 |

### States to verify
- All queues empty → exact "Ritual clear - nothing waiting on you." line renders (never blank space).
- Pre-migration / stub DB missing `decisions` or `coach_pings` tables → those lines silently absent, no 500 (each query is independently try/excepted; `render_open_loops_band` never raises).
- Worldview flag OFF → no "Tenets proposed" line even with proposed tenets in DB.
- Unparseable `created_at` → count renders without the `· oldest Xd` suffix.
- `GET /` returns 200 with the band present in `#panel` HTML on a fully-populated prod DB.

## Research cockpit (Home main tables)

**Reach:** `http://127.0.0.1:7421/` Overview tab (server-inlined for first paint); the whole cockpit fragment self-refreshes via HTMX `GET /api/cockpit` every 90s inside `#cc-cockpit-live`. **Preconditions:** :7421 server; `data/portfolio.db` with `tracked_companies`; on-disk FMP caches (`data/historical/fmp/<T>_profile.json`, `_earnings_calendar.json`), `data/valuation_basis/<T>.json` for PEG; morning-pipeline caches (`fundamentals`, `candidate_fit.json`) optional — falls back to live DB scan / no Fit chip.

**Renders (top→bottom):** `<section class='list-section cockpit-section'>` "Portfolio (N)" then "Evaluation (N)". Each: living-grid filter bar ("Filter by ticker / name…", "N holdings"/"N evaluations" count), then table. Portfolio columns: Ticker (link `/ticker/<T>`, company name in `title`) · Thesis (k-pill verdict badge, breach/warn rule names + "evaluated Xh ago" in hover) · Tier-1 moves (up to 3 `k-chip` KPI-delta buttons, largest movers first, toned by break-rule status, `data-ask-q` doorway, values/periods in hover) · Price (`$X.XX +Y%`, "last FMP quote …" hover) · vs DCF FV (signed %, recomputed `live/fair−1`; `neg` tone when price above FV; hover "DCF FV $A vs $B — run DATE") · PEG · Next ER (date; `er-soon` warn tone ≤7d; "in Nd"/"today" hover) · Inbox (pill cluster, see actions) · ops dot (`●` ok/warn/bad; FMP>3d/14d or build>10d/30d; hover "FMP Xd ago · build Yd ago · transcript …"). Portfolio sorted by attention (breach → pending alerts → new docs → ticker). Evaluation table (thin, tighter padding, no Tier-1 column): adds Score (next-dollar attractiveness chip, factor math verbatim in hover, dashed border = partial data) and Fit chips + Rev YoY / FCF mgn columns; sorted score-descending. Below cockpit: tier-coverage strip (`render_tier_coverage_strip`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Column header (`.lg-sortable`) | click / Enter | Alpine `sortBy(key,type)` re-orders rows client-side; arrow indicator + `aria-sort` update | Header tooltips on Score/Fit/RevYoY/FCF explain the column | P1 |
| Filter bar input | type | Rows filtered by ticker + company name (`data_text`) | Empty result shows 0-count | P1 |
| Ticker cell | click | Navigate `/ticker/<T>` full workspace page | — | P0 |
| Tier-1 KPI chip | click | `data-ask-q` opens Ask dock with "`<metric>` for `<T>`, last 12 quarters" (chart) | No deltas → muted "—"; needs 2 non-superseded facts with `period_end ≤ today` | P0 |
| Score chip (eval) | click | Peek popover `GET /api/peek/score?ticker=T` — factor breakdown (DCF upside · Rev growth · FCF margin · PEG multipliers); middle-click → `/ticker/<T>` | Missing score → "—"; partial data → dashed chip; hover title = full factor math `dcf 1.50 (+32.0% upside) x … = 2.1` | P1 |
| Fit chip (eval) | click | Peek `GET /api/peek/fit?ticker=T`; toned ok(>1)/warn(<1) | Absent candidate_fit cache → no chip ("—") | P1 |
| "N alerts" pill (bad tone) | click | Peek `GET /api/peek/alerts?ticker=T&status=pending` in place; real href `/feed?ticker=T&status=pending` for middle-click | Only renders when pending_alerts>0; singular/plural label | P0 |
| "N new docs" pill (accent) | click | Peek `GET /api/peek/documents?ticker=T`; href `/#holding=T` | Requires `tc.last_built_at` set; julianday bridge for both timestamp spellings | P1 |
| "N comments" pill (warn) | — | Static pill, hover "open report comments" — no doorway | — | P2 |
| "review" pill | click | Peek `GET /api/peek/review/<T>` (instant position review) | Portfolio rows only — never on evaluation rows (PR5) | P0 |
| Ops dot | click | Peek `GET /api/peek/provenance?ticker=T` (per-source ages + inline refresh); href `/#system` | `bad` when either age missing; hover carries FMP/build/transcript detail incl. "(no Q&A)" | P1 |
| HTMX 90s poll | timer | `GET /api/cockpit` swaps `#cc-cockpit-live` innerHTML | Server down → tiles freeze silently; verify no console error storm | P1 |

### States to verify
- Empty list → exact `<p class='empty'>No portfolio tickers.</p>` / `No evaluation tickers.`
- Partial DB (missing `dcf_runs`/`thesis_evaluations`/`kpi_facts` tables) → sparser row, all enrichment cells "—", no 500 (`_safe_rows` swallows `OperationalError`).
- Missing profile JSON → Price "—"; PEG cache absent → "—"; negative/zero PEG treated missing.
- Guidance rows with future `period_end` excluded from Tier-1 deltas (as_of filter).
- Evaluation row with no inputs at all → score = 0.85⁴ ≈ 0.52 chip still renders (dashed), sinks below full-data names.
- fv gap recomputed, not read from `over_under_pct` — verify BN/NU (bank/holdco writer names) show consistent sign.

## Inbox rail (Home)

**Reach:** `http://127.0.0.1:7421/` — right-hand `<aside class="cc-home-rail">` on Overview (only when `collect_inbox` returned HTML). **Preconditions:** :7421 server; `alerts`/`queued_actions`/`thesis_ledger`/`analyst_notes` tables (each source degrades independently); synthesis cards need a fresh (≤7d) `llm_artifacts` row for `lens:cross_portfolio_synthesis`.

**Renders (top→bottom):** Upcoming-earnings strip (separate surface below) → rail head `<h2>Inbox` + unread badge (`data-ix-badge="home"`, hidden until INBOX_JS counts fresh cards) + link `full feed` → `/feed` → compact stream (`.ix-stream.ix-compact`, `data-ix-surface="home"`, top 14 items, ranked score-desc): category filter chips row ("All N" + one chip per present category from News/Earnings/Press releases/Rating changes/Thesis changes/Drafts/Watch items/Synthesis, with counts), then uniform `.ix-card`s: ticker doorway (`/#holding=T`, `data-peek-ticker` hover mini-card) · kind chip (humanized — "Earnings tone", "News", "KPI inflection", "Thesis drift", "Say/do due", "Condition met", "Restatement"; ranking factor breakdown in `title` "ranked: …", cursor:help) · status k-pill (pending=warn, applied/approved=ok; "open" suppressed) · hover-revealed ✓/✕ quick actions · relative stamp · body clamped 2 lines (unclamps on hover) · footer "review →" (peeks `/api/peek/alert/<id>`) on pending alert/draft cards; "article ↗" (new tab, noopener) on news cards; advisor-memo cards instead carry always-visible `open memo` + `dismiss` chips. Unread cards get inset accent bar (`.ix-new`); badge shows count; localStorage mark `ix-last-seen:home` advances only once the stream is on-screen (IntersectionObserver).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Category chip | click | Client-side: `.is-on` moves to chip, non-matching cards get `.ix-hide`; "All" restores | Scoped per-stream; no server call | P1 |
| Ticker link | click | `/#holding=T` opens holding in shell; hover → peek mini-card | — | P0 |
| ✓ approve (hover) | click | HTMX `POST /approve` `{action_id}` → swaps `.ix-quick` for `✓ applied` chip + consequence receipt detail (truncated 60ch, full in title, doorway when `detail_href` present) | 409 on double-click/stale (already applied); 403 cross-site; only present when a pending queued action exists | P0 |
| ✕ dismiss alert (hover) | click | HTMX `POST /api/alerts/<id>/dismiss` → cancels pending drafts, alert→dismissed, chip `✕ dismissed` + inline `why?` affordance | Terminal (no Undo — cascade); 404 unknown, 409 already terminal | P0 |
| `why?` → reason input | click, type, Enter | Toggle reveals one-word input (maxlength 40, placeholder "one word"); Enter hx-posts `{reason}` to same dismiss route; input removes itself (empty HTML swap) | Reason-only round-trip must NOT 409 on already-dismissed alert; skippable — no submit leaves nothing | P1 |
| ✕ dismiss draft (standalone) | click | HTMX `POST /approve&dismiss=1` → `✕ dismissed` chip with **undo** button | Undo `POST /api/actions/<id>/uncancel` restores ✓/✕ pair; 409 if not cancelled | P1 |
| ✕ archive note | click | HTMX `POST /api/notes/<id>/archive` → chip with undo (restores note button) | Only plain notes (not advisor memos) | P1 |
| `open memo` chip (memo card) | click | Navigates `/#advisor_memos` | Ledger-echo survivor (no note_id) gets open-memo only | P1 |
| `dismiss` chip (memo card) | click | Vanilla fetch `POST /api/notes/<id>/archive`; card fades (`.ix-dismissed`), chips replaced by `✓ dismissed` | On HTTP error: button re-enabled, `.ix-act-fail` red, error in title | P1 |
| "review →" footer | click / hover | Peek `/api/peek/alert/<id>` (evidence drawer detail); real href `/feed?ticker=T` | Absent when no alert/action id | P1 |
| "article ↗" | click | Opens evidence `url` in new tab | Only http(s) URLs from `material_news` evidence; malformed JSON → no link | P1 |
| "full feed" link | click | Navigate `/feed` | — | P0 |
| Unread badge | page load | Count of cards with `data-when` > localStorage mark; hidden at 0 | localStorage blocked → init aborts silently, no badge, no crash | P2 |

### States to verify
- No items → rail still renders with empty copy `Nothing new — alerts, drafts, thesis changes, and watch items land here.`
- Cross-kind dedupe: advisor memo appearing as both ledger echo and journal note → ONE card (richest kind wins).
- Standing kinds: a 10-day-old pending draft/open note still present (ignores `since`), sunk by recency decay.
- Note pending reconciliation → "Reconcile · " prefixed label + pending pill.
- Stale (>7d) synthesis memo → no synthesis cards.
- Ledger/synthesis cards: NO hover quick actions (informational; age out).
- Missing DB file → `collect_inbox` returns [] → empty state, no 500.
- Approve 409 (double-click GET link in second tab) surfaces error JSON, not a crash.

## Upcoming earnings strip

**Reach:** Top of the Home rail (above "Inbox" head), same `GET /`. **Preconditions:** :7421 server; `expected_earnings` table (0082, refreshed daily by `execution/refresh_expected_earnings.py`) or `earnings_surprises` history for the +91d estimate fallback; strip renders only if a tracked (portfolio/evaluation, non-archived) name reports within 14 days.

**Renders (top→bottom):** `.up-strip` card: head "Upcoming earnings" + right-aligned sub "next 14d"; then one `<li>` per name sorted by date then ticker: ticker (mono, `data-peek-ticker` hover mini-card) · `est.` chip on the fallback path · right-aligned date (`~`-prefixed when estimated; row title "est. next earnings"/"next earnings"). Beneath each row: up to 3 open analyst notes for that name (watch kind first, then question, then others), each a full-width button: kind `k-chip` + one-line ellipsized body (64-char label cap, full `kind: body` in title); overflow line `+N more open item(s)`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Ticker | hover | Peek mini-card via `data-peek-ticker` (not a click doorway — no href) | — | P1 |
| Watch/question item | click | `data-ask-q` → shell `goAsk` opens Ask dock with "`<note body>` (`<T>`)" | Hover: color→accent, body underlines | P0 |
| — | — | — | — | — |

### States to verify
- Nothing within horizon → strip absent entirely (returns `""`, no empty shell).
- Calendar-owned ticker whose real date is outside 14d → NOT re-estimated (must not appear via fallback).
- Fallback-only ticker → `~YYYY-MM-DD` + `est.` chip.
- Name with zero open notes → row renders ticker+date only (hide-don't-stub).
- Missing `expected_earnings` table → estimate path only; missing DB → no strip; never a 500.

## /feed page (full inbox feed)

**Reach:** `http://127.0.0.1:7421/feed`; params `?ticker=`, `?trigger_kind=`, `?status=`, `?limit=` (default 200, non-int falls back 200), `?user_id=`. `/alerts` 302-redirects to `/feed` preserving the query string. **Preconditions:** :7421 server; same DB tables as the rail.

**Renders (top→bottom):** Standalone dark-theme document (`<title>Portfolio · inbox feed</title>`, Google-fonts links). Header `<h1>Inbox feed</h1>`; subtitle `N shown · ranked by what matters now — newest and most material first. Filter the stream by category below, or hover a card to expand it.`; when server filters are active, a removable-chip band (`ticker: X ✕`, `trigger: Y ✕`, `status: Z ✕`) — no band when unfiltered (no "ALL·ALL·ALL"). Then the full stream (`surface="feed"`, `show_filters=True`): category chips + non-compact cards. Differences vs rail: bodies clamp 3 lines (vs 2); NO hover ✓/✕ header buttons; instead pending cards carry footer text links `approve` / `dismiss` (no-JS `GET /approve?action_id=N[&dismiss=1]`); no "review →" link; unread tracking keyed separately (`ix-last-seen:feed`) but no visible badge (no `[data-ix-badge="feed"]` element). Footer: "Inbox feed" + "generated <relative stamp>". Setting `trigger_kind` or `status` narrows collection to alerts-only kinds.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Category chip | click | Client-side show/hide, same as rail | — | P1 |
| Filter chip ✕ | click | Navigates to `/feed` minus that one param, others preserved (quote_plus-encoded) | — | P1 |
| `approve` footer link | click | `GET /approve?action_id=N` → applies action (ledger/sizing write) → 303 back to Referer path (else `/feed`); card re-renders with `applied` pill | Double-click → 409 JSON page; cross-site Referer or `Sec-Fetch-Site: cross-site` → 403; bad id → 400/404 | P0 |
| `dismiss` footer link | click | `GET /approve?action_id=N&dismiss=1` → cancels, 303 back | Same guards; no undo on this path (status pill flips to cancelled) | P0 |
| Ticker link | click | `/#holding=T` back into the shell | `data-peek-ticker` present but peek JS may not be mounted on this standalone page — hover is shell-only | P0 |
| `article ↗` | click | Source story, new tab | — | P1 |
| Kind chip hover | hover | `title="ranked: severity … x recency … = score"` factor breakdown | — | P2 |
| `?ticker=RBRK` etc. | URL | AND-composed server filtering; alerts+drafts scoped to ticker; synthesis excluded when ticker set | Unknown ticker → empty state | P1 |

### States to verify
- Empty / over-filtered → exact copy `No items match the current filters.`
- `?limit=abc` → silently 200; `?status=pending` → alerts-only view with filter chip.
- Back-navigation after approve: 303 Referer round-trip lands back on `/feed` with the same query string.
- Missing DB → empty state renders, 200 not 500.
- HTMX attributes on this page are inert (no htmx script in `_document`) — the GET links are the intended path; verify approve links work JS-free.
- Page loads with no console errors despite INBOX_JS expecting optional elements (badge absent).

---

# Part 3 — Companies panels (Holding, Discovery, Diet, Journal, Triage)

## Holding view (Companies → Holding)
**Reach:** `http://localhost:7421/#holding` (no ticker → picker band only) or `#holding=<T>` (e.g. `#holding=NU`); alias `#companies`; `GET /ticker/<T>` 302-redirects to `/#holding=<T>` (400 on invalid ticker). Fragment served by `GET /api/panel/holding[?ticker=T]`. **Preconditions:** comments_server on :7421; `data/portfolio.db`; a built workspace brief (`output/research/<T>/<DATE>_workspace.html`) for the report embed; portfolio-tracker at `PORTFOLIO_TRACKER_URL` (default `http://localhost:5173`) for Tracker link/position data.
**Renders (top→bottom):** One ~40px utility band: left, the type-ahead combobox (`.cc-combo`, mono ticker value, muted company-name overlay hidden on focus, placeholder "Search holdings — ticker or name…"); right, identity badges (list_type `.k-chip`, breach-status `.k-pill` toned intact/ok→ok, watch→warn, broken/breach→bad), the freshness dot (● worst-of build/FMP age: ok ≤7d, warn ≤21d, bad >21d/never; tooltip "Build … · FMP pull … · Transcript …"), link cluster "Report ↗ · DCF ↓ · Review · Tracker ↗ · Ledger", then "⚙ Ops" and "✎ Notes" buttons. Below: the embedded `/reports/<T>` iframe (the report IS the page — carries its own tab strip: Overview [thesis, valuation] · Quarter [earnings, saydo, news] · Financials · Research [bear/company/exec_comp/synthesis; Research leads on eval flavor] · Position [position, decisions, only when held; standalone Decisions when exited; hidden when neither] · Sources, each multi-section group with a subtab pill row; the iframe also carries comment pins/chat/apply pipeline). Hidden until opened: the Ops drawer (right slide-in, order: 5-minute reread → position-lifecycle timeline → attribution ("what drove window alpha") → freshness strip (Last build / Last FMP pull / Last transcript) → Refresh panel → DCF ⇄ Google Sheets panel → Analyses run (+ Recent alerts + Recent LLM calls w/ 30d cost) → Artifacts tables by category). No-ticker state shows the combobox + hint "Search a ticker or name to open a holding." While a holding is open the Companies sub-tab row is suppressed in the shell.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Combobox open | focus input | select-all + lazy `GET /api/tickers`, list of ≤50 `<li>` (mono ticker + muted name) flush under input, `aria-expanded=true` | fetch fail → empty list "No match."; list absolute-positioned, band height stable | P0 |
| Combobox filter | type | substring match on ticker OR name, re-render | no hits → "No match." row (non-clickable) | P0 |
| Combobox pick | click li / ↑↓+Enter (Enter with exactly 1 match also picks) | sets `location.hash = #holding=<T>` → shell re-fetches fragment | picking the current ticker just restores display + closes | P0 |
| Combobox Escape | Esc in input | restores display value, closes list, blurs | blur alone (150ms delay) also closes + restores | P1 |
| Freshness dot | click ● | peek `GET /api/peek/provenance?ticker=T` (per-source ages + inline refresh buttons); real href `/#system` for middle-click | always 200; missing data = em-dash ages | P1 |
| Report ↗ | click | new tab `GET /reports/<T>` (latest `*_workspace.html` via send_file) | 404 if never built; 400 bad ticker | P0 |
| DCF ↓ | click | `GET /dcf/<T>`: 302 to linked Google Sheet if `dcf_defaults.gsheet_id` set, else streams `dcf/<T>.xlsx`, else latest dated `*_dcf.xlsx` | 404 when none exists | P1 |
| Review | click | peek `GET /api/peek/review/<T>` (instant LLM-free pre-analysis: facts, mechanical read, tax, graded-sells base rate, escalate-to-full-LLM footer button); real href `/ticker/<T>` for middle-click | always 200; degrades tracker-offline/no-thesis | P0 |
| Tracker ↗ | click | new tab `<tracker>/holdings?ticker=T` (matching row highlighted and scrolled into view) | link absent only if tracker_url None (never — env default) | P2 |
| Ledger | click | `#musings` hash — shell lands on Ledger tab (doorway while sub-row suppressed) | — | P1 |
| ⚙ Ops | click | Ops drawer + scrim un-hidden (slide-in-right) | Escape or scrim click or × closes; multiple opens idempotent | P0 |
| ✎ Notes | click (`data-cc-notes-open`) | shell's SHARED notes drawer opens ticker-scoped (see next surface) | — | P0 |
| Refresh (Ops) | click "Refresh" | `POST /actions/refresh {ticker, mode:"stale", force_budget_bypass:false}` → msg "starting…" then "started — view log" link to `/actions/stream/<id>` | error → "error: <e>"; network → "network error"; concurrent job → registry conflict error | P0 |
| Run anyway (ignore caps) | click | same POST with `force_budget_bypass:true` | same | P1 |
| Persistent bypass toggle | check "Always ignore budget caps for this ticker (persistent)" | `POST /api/ticker-settings/<T> {bypass_budget}` → "saved ✓"; initial state fetched via GET | 500 "could not persist (ticker_settings table missing?)"; msg "error: …" | P1 |
| Push to Sheets | click | `POST /actions/dcf-export {ticker}` → job stream; "Open in Google Sheets ↗" link appears after ~2s via `GET /api/dcf-sheet/<T>` | no Google creds → job errors in log; link stays hidden when unlinked | P1 |
| Re-ingest from Sheets | click | `POST /actions/dcf-import` → "pulling + recomputing…" then job stream | same | P1 |
| Position-lifecycle grading form (Ops) | submit | `POST /api/position-entries/<id>` {exit_reason?, lessons?, outcome_vs_thesis?}; section re-fetches `/api/position-lifecycle/<T>` | 400 unknown outcome label; 404 missing row; 500 pre-0088 DB | P1 |
| Report tabs/subtabs (iframe) | click tab/pill | pane switch, group badge = summed section counts | Position group absent when not held; empty sections hidden (hide-don't-stub) | P0 |
| Drawer dismiss | Escape / scrim / × | all open `.tcc-drawer` hidden | — | P0 |

### States to verify
- No ticker: picker band + hint only, no 500.
- Never-built ticker: "No workspace brief built yet for this ticker — open ⚙ Ops above and hit Refresh to build one." instead of iframe.
- Tracker offline: position strip "Portfolio-tracker not connected. Set PORTFOLIO_TRACKER_URL…"; attribution degrades.
- Missing tables: analyses "No analysis tables present yet."; thesis "No holdings JSON for this ticker."
- Double-click Refresh → second job 409/RegistryConflict surfaces as error text, no crash.

## Shared ✎ Notes drawer
**Reach:** ✎ button on the Holding band (ticker-scoped) or shell topbar ✎ (unscoped). Fragment `GET /api/panel/notes_drawer[?ticker=T]`. **Preconditions:** server; `analyst_notes` table (0074) else unavailable-state.
**Renders (top→bottom):** "Quick note" panel — kind `<select>` (NOTE_KINDS, "observation" preselected) · ticker input (placeholder "ticker (optional)", pre-filled when scoped, editable) · textarea (placeholder "What did you notice? Enter saves · Shift+Enter for a newline.") · "musing" checkbox (title "Route to the Ledger capture spine — wondering/pledge taps run.") · "Add note" primary button · status span. Then "Open notes" panel (≤20, newest first, kind + date stamp + prose body + "via <source>" meta); when ticker-scoped also "Recent alerts" panel (≤5 alert cards, evidence drawer collapsed, "full feed ↗" link to `/feed?ticker=T`). Footer: "Resolve · reclassify · supersede live in Companies → Journal." linking `/#journal`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Add note | click "Add note" or Enter in textarea | `POST /api/notes {kind, body, ticker?}` (source=manual) → "saved ✓", drawer fragment re-fetched (form resets, note appears) | empty body → "write the note first"; API error → "error: <e>"; offline → "network error" | P0 |
| Musing routing | check "musing" + save | same text `POST /api/capture/text` `{text: "$TICKER body"}` (ticker prefixed as $-mention so roster matcher links it) | blank ticker → bare text → needs_ticker musing | P0 |
| Shift+Enter | in textarea | newline, no save | — | P2 |
| Alert card actions | approve/dismiss links on queued actions | standard alert-card verbs (POST approve / dismiss routes) | — | P1 |

### States to verify
- No open notes: "No open notes on this name. Notes arrive from report comments, chat, and alert flows."
- Table missing: "Notes substrate unavailable — the analyst_notes table is not in this DB." / alerts analog.
- No alerts: "No alerts fired on this name yet."

## Discovery (Companies → Discovery)
**Reach:** `/#discovery`. Fragment `GET /api/panel/discovery[?fragment=list|sources&status=&min_score=]`. **Preconditions:** server; `discovery_candidates`/`discovery_sources` tables (0081+, alembic-seeded); jobs run `execution/run_discovery.py` / `discovery_build.py`.
**Renders (top→bottom):** ONE toolbar band: count span (lifted from list fragment by JS) · status chips acting as radio (`live`, then each CANDIDATE_STATUSES: new/queued/building/built/dismissed; tones: new=accent, queued/building=warn, built=ok) · "min score" number input (0–10 step 0.5) · buttons "Sources", "Run discovery", "Build selected" (primary). Then the ranked queue `.p-table` capped at top-60 (count line "N candidate(s) (top 60 of M)"): checkbox (new/queued rows only) · ticker_label linking `/api/panel/holding?ticker=T` · score `.k-pill` (accent ≥3.0, warn ≥2.0) · status chip · "Why surfaced" one-liner + "details" peek button · per-status actions. Hidden Sources well ("Source weight registry — editing a weight re-ranks the queue") with per-source rows (name, class chip, tier, weight number input 0–3 step .05, Save + "saved" flash, CIK). A `<pre>` job-log pane (appears on first job). Hint paragraph: "Build = promote to the evaluation list + onboard … Chat knows the same verbs: /discovery list · /discovery queue T · /discovery dismiss T · /discovery build T."

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Status chip | click | radio-toggles `is-on`, re-fetch list fragment with status | unknown status param → treated as live/None | P0 |
| min score | change | re-fetch list filtered `score >= value` | non-numeric → 0.0 | P1 |
| Row click-to-expand | click row (not on a/button/input/label) | toggles hidden evidence row (per-signal contribution table `class:source_key · contribution · detail`; legacy rows fall back to verbatim evidence; else "no evidence recorded") | "details" peek button = keyboard path, same toggle | P1 |
| Queue | click (new rows) | `POST /api/discovery/candidates/<id>/status {status:"queued"}` → refresh | 400 for building/built (owner can't hand-wave built); 404 unknown id; 500 table missing | P0 |
| Build (row) | click | `window.confirm("Start eval build for T?\n1 build(s) x ~25 min + LLM spend each")` → `POST /actions/discovery-build {tickers:[T]}` → SSE stream into log pane, refresh on done | cancel = no-op; 400 not-buildable (must be new/queued live candidate), >MAX_BUILD_BATCH, empty list; 409 job conflict → "build rejected: …" | P0 |
| Dismiss | click | `prompt` "Passing on T? Optionally note WHY — records a gradeable avoid…" ; if reason given, second prompt "What would make you revisit T?…"; POST status=dismissed (+reason/revisit_if → first-class gradeable avoid decision) | prompt cancel = abort; blank = queue-state-only dismiss; dismissed names never resurface on re-runs | P0 |
| Re-open | click (dismissed/built) | POST status "new" → refresh | — | P1 |
| Build selected | click | confirm + POST all checked tickers (one sequential job, slot "DISCOVERY-BULK") | none checked = no-op | P1 |
| Run discovery | click | `POST /actions/discovery-run` → SSE job log ("=== discovery-run <id> started ===" … "=== done (exit N) ===") + refresh; deterministic, no LLM | 409 conflict → "discovery run rejected: …" | P0 |
| Sources toggle | click | reveals well + fetches `?fragment=sources`; toggles is-on | table missing → "No source registry (run alembic upgrade to seed it)." | P1 |
| Save weight | click Save | `POST /api/discovery/sources/<key>/weight {weight}` → "saved" flash 1.5s + list refresh (re-rank) | 400 non-number; 404 unknown key; 500 table missing | P1 |

### States to verify
- Empty queue: "No candidates match. Run discovery (button above) to sweep the screens + adjacency miners, or relax the filter."
- Pre-0081 DB degrades to that empty state (no 500).
- >60 candidates → elision disclosed in count; count sits on toolbar (never a row in the list).

## Diet (Companies → Diet)
**Reach:** `/#diet`. Fragment `GET /api/panel/diet`. **Preconditions:** server; `signals` table (alembic 0095), populated by news + yf_grades + IR-events feeds. Pure read, no writes.
**Renders (top→bottom):** Panel "Information diet" with sub "The pull lane — what to **read** on your names…". Section "Ingest stream" (sub "Recent sell-side ratings + news on tracked names, newest first. Not ranked by urgency — this is reading, not triage."): living-grid filter bar (placeholder "Filter by name / source / text…", "N signals" count) over a `.p-table` — sortable When/Name/Type/Source headers + Signal column; rows: date · ticker_label · type pill (Rating/News/Podcast, quiet) · title (external link `target=_blank` when URL) · firm. Section "Forward agenda" (upcoming investor/analyst days, soonest first): filter bar ("Filter by name / event…") + table Date · In (today/tomorrow/Nd) · Name · Event · Source. Footer scaffold note: "**Coming as fast-follows:** buy-side ratings (the 13F + ARK layer) and sell-side estimate / model revisions…".

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Filter box | type | client-side living-grid substring filter on data-text; count updates | no matches → zero rows, no error | P1 |
| Column sort | click sortable th | living-grid text sort asc/desc | — | P2 |
| Signal/event link | click title | opens source URL in new tab (`rel=noopener`) | URL-less rows render plain text | P1 |

### States to verify
- Empty stream: "No diet signals yet — they populate from the news + yfinance-grades feeds."
- Empty agenda: "No investor days on the calendar — these land as the IR-events feed records them."
- Pre-0095 DB (no `signals` table): both empty states, no 500. Diet rows never appear in the Inbox (no decay/veto).

## Journal (Review → Journal)
**Reach:** `/#journal`. Fragment `GET /api/panel/journal[?ticker=&kind=&status=&fragment=list|reconcile]`. **Preconditions:** server; `analyst_notes` (0074), links 0093.
**Renders (top→bottom):** `<h2>Journal</h2>`; new-note form (textarea placeholder "New note… (a watch item, a question to answer, an assumption to check)", kind select, ticker input placeholder "TICKER" title "Blank = portfolio-level note", "Add note" primary). Pending-reconciliation strip (only when non-empty): warn chip "pending reconciliation" + "N open note(s) whose linked object concluded", warn-well cards with body, "decision/position #id · label — conclusion" line, buttons "Resolve with conclusion" and "Keep open (unlink)". Filter row: ticker input · kind select ("any kind"+NOTE_KINDS) · status select (open/resolved/superseded/archived/all, default open) · "Filter" button · count span. Note list: owner cards (kind chip · ticker sym or PORTFOLIO chip · colored status · date · source · anchor `@ type · key` · linked-object chips `→ decision #7` mono, warn-toned + "— conclusion" when concluded, "· auto-resolve" suffix) with body prose, resolution line "↳ …" or "supersedes note #N", and (open notes only) action row: Resolve · Supersede · Archive · reclassify select · Link controls (target select grouped Decisions/Position stints + "auto-resolve" checkbox + Link button; or Unlink when linked). Below owner notes: collapsed `<details>` "Advisor synthesis — N machine-authored memo(s)" silo with recessed memo cards ("Advisor memo" chip, body, "Open in Memos" link `#advisor_memos`, Archive). Footer hint: "Resolve closes an item … Superseded and archived notes are kept forever — memory is the point."

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Add note | submit form | `POST /api/notes {body, kind, ticker|null}` 201 → clears body+ticker, refreshes list+reconcile fragments | empty body = silent no-op; bad kind → 400 (form stays) | P0 |
| Filter | submit | re-fetch `?fragment=list` + `?fragment=reconcile` with ticker/kind/status | unknown kind/status dropped server-side (no 500) | P0 |
| Resolve | click | `window.prompt("Resolution note (optional):")` → cancel aborts; `POST /api/notes/<id>/resolve` → refresh | 404 unknown id | P0 |
| Supersede | click | prompt "Replacement note text:"; empty/cancel aborts; `POST …/supersede {body}` creates chained replacement | 400 missing body | P1 |
| Archive | click | `POST …/archive` → refresh (kept forever, drops from live recall) | — | P1 |
| Reclassify | change select | `POST …/reclassify {kind}` → refresh | 400 bad kind | P1 |
| Link | pick target + optional auto-resolve, click Link | `POST …/link {decision_id|position_entry_id, auto_resolve}` → chip appears | no selection = no-op; 404 dangling target | P1 |
| Unlink | click | `POST …/unlink` → link controls return | — | P1 |
| Resolve with conclusion | click (reconcile card) | prompt pre-filled with `data-suggest` suggested resolution → `POST …/resolve` | cancel aborts | P0 |
| Keep open (unlink) | click | `POST …/unlink`; note leaves strip, stays open | — | P1 |
| Synthesis silo | click summary | expands/collapses memo cards | — | P2 |

### States to verify
- Empty list: "No notes match this filter. Notes arrive from report comments, chat, alert reviews, advisor memos — or directly from the form above."
- All-synthesis result: owner silo shows "No notes of your own match this filter."
- Missing/pre-0074 DB → empty state, no 500. Reconcile strip absent entirely when nothing pending.

## Triage (Review → Triage)
**Reach:** `/#triage`. Fragment `GET /api/panel/triage[?fragment=list]`. **Preconditions:** server; `analyst_notes`; rows = `source='comment'` notes with `context_json['intent']=='needs_triage'`.
**Renders (top→bottom):** panel_toolbar "Triage" with "N open" chip; sub-copy "Comments the classifier could not map to an actionable router (unmappable or conditional directives) park here instead of being force-bucketed. Route each to the real intent it meant, resolve it once handled, or dismiss it." Living-grid filter bar ("Filter by name / anchor / text…", "N parked") + `.p-table`: sortable When/Name/Anchor · Comment (truncated ≤160 chars as a button, title "Open detail") · Disposition cell ("Route to…" select with intents Question/Thesis edit/Structured edit/Drop KPI/Extract KPI/Curate peers/Data fix/Rewrite section + Resolve + Dismiss buttons). Hidden right-rail drill-in drawer (`CCOverlay`, role=dialog, aria-modal, focus-trap): "Parked comment" h3 + × close, mono meta line (ticker/PORTFOLIO · anchor · report DATE · tab · filed DATE), quoted selected-text block, full body (textContent, pre-wrap), and the same Route/Resolve/Dismiss action row.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Filter/sort | type / click th | client-side living-grid filter + sort | — | P1 |
| Open detail | click comment text | drawer populated from row data-* (no second fetch), CCOverlay slide-right open | route select resets to blank each open | P0 |
| Route | pick intent in select (row or drawer) | `POST /api/notes/<id>/route {intent}` — note re-types + leaves queue; underlying comment best-effort updated when the report build still exists; drawer closes; list re-fetches; "N open" chip recounts | blank selection no-op; unknown intent → 400 | P0 |
| Resolve | click | prompt "Resolution note (optional):" (cancel aborts) → `POST …/resolve` → close + refresh + recount | 404 unknown id | P0 |
| Dismiss | click | `POST …/archive` → close + refresh + recount | — | P0 |
| Drawer dismiss | Escape / scrim / × | CCOverlay closes, focus restored | — | P1 |

### States to verify
- Empty queue: "Nothing to triage. Comments the classifier cannot route land here for disposition; the queue is clear."
- Missing/pre-0074 DB → same empty state + "0 open" chip, no 500.
- After any disposition, row disappears on refresh and count chip matches remaining `tr[data-note-id]` rows.

---

# Part 4 — The Ledger panel (capture, On My Mind, Worldview, Research, Reconcile)

## Jump-chip toolbar
**Reach:** `http://127.0.0.1:7421/#musings` (shell hash alias `/#ledger` → `musings`); the shell lazy-loads the panel via `GET /api/panel/musings`. **Preconditions:** :7421 server (`execution/comments_server.py`) running; none else — chips always render.
**Renders (top→bottom):** Below the `Ledger` `<h2>` (tutorial sub-line visible only when the front-of-funnel feed is empty, else folded into `title=`), one wrapping row of `k-chip k-chip-btn` buttons in order: **Capture**, **On My Mind** (only when `LEDGER_ONMYMIND` on), **Worldview**, **Stances**, **Research**, **Reconcile**, plus **Musings** appended only when On My Mind is OFF. Research/Reconcile/Worldview chips carry a mono count span when their pending count > 0 (counts from `pipeline.open_loops` private helpers, each degrading to 0).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Any chip | click `[data-ledger-jump]` | `preventDefault` + `scrollIntoView({smooth})` to `ledger-jump-<anchor>` div. No network. | Never an `href="#anchor"` (would trip the hash router to Overview). Chip to a flag-off Worldview section scrolls to an empty div — verify no navigation away. | P1 |
| On My Mind chip presence | `LEDGER_ONMYMIND=1` | Chip appears; Musings chip disappears (list suppressed) | Flag off: "Musings" chip appears instead | P1 |
| Count badges | data present | e.g. `Research 3` mono span | Count-query failure ⇒ chip renders without count, no 500 | P2 |

### States to verify
- Empty DB: all chips render, no counts, panel `<p class="sub">` tutorial visible.
- Chips wrap on narrow width; no horizontal scroll.
- No 500 when `open_loops` counts throw.

## Capture box (+ entry-coach card)
**Reach:** first section of `/#musings` (`#ledger-jump-capture`). **Preconditions:** :7421 server; pledge coaching needs `research.pledge` detection to fire; wondering tap needs `LEDGER_RESEARCH_TAP` ≠ 0 (default on).
**Renders (top→bottom):** `.ledger-cap` card: 3-row textarea `#ledger-cap-text` placeholder `"Think out loud - a musing, a wondering, a worry. Mention a name and it links itself. (Cmd/Ctrl+Enter to capture)"`; row with primary **Capture** button `#ledger-cap-btn` + status span `#ledger-cap-status`; empty `#ledger-cap-coach` mount below.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Capture | click `#ledger-cap-btn` or **Cmd/Ctrl+Enter** in textarea | Button disables, status `Capturing...`; `POST /api/capture/text` `{text}` (channel=`tray`; ingest LLM-free, but pledge-shaped text triggers one governed `musing_decision_extract` LLM call at the detect layer — see the tray section's cost note). On success textarea clears; status `Captured - <TICKER>` / `Captured - needs ticker` / `Captured`; then re-fetch `GET /api/panel/musings?fragment=list` into `#ledger-list`. Status auto-clears after 4 s. | Empty text ⇒ focus only, no POST. Server down ⇒ status `Could not reach the server - try again.`, button re-enables. Blank body ⇒ 400 `{"error":"text required"}`. Double-click guarded by `disabled`. | P0 |
| Coach challenge card | response has `pledge_challenge` | Card renders in `#ledger-cap-coach`: escaped plain text (newlines→`<br>`), quiet `×` dismiss (`data-coach-dismiss`, top-right), input placeholder `conviction + falsifier — one line completes the record` + primary **Send**. | Plain text only — no markdown render. Card persists until dismissed/replaced. | P0 |
| Coach Send | click **Send** | Empty note ⇒ focus. Else button disables, `POST /api/capture/text` with the annotation; response re-renders card (receipt or another challenge). | Fetch failure re-enables Send. | P0 |
| Receipt | response has `annotated_decision_id` | Card: `Noted — recorded on decision #<id>` where `#<id>` links `/#decisions_record`. Also has `×` dismiss. | Link must navigate shell to Decisions panel. | P1 |
| Dismiss coach | click `×` | `coach.innerHTML=''` — no server call. No Escape handler (not an overlay). | — | P2 |

### States to verify
- Ledger-list fragment refresh after capture shows the new musing at top without full reload.
- Capture while `LEDGER_RESEARCH_TAP=0`: still lands; no wondering chip appears later.
- Neither challenge nor receipt ⇒ only the status line, coach div stays empty.

## On My Mind feed
**Reach:** `#ledger-jump-onmymind` on `/#musings`. **Preconditions:** `LEDGER_ONMYMIND=1` (default off ⇒ section is empty string and plain Musings list renders instead); notes with `source='capture'` in `analyst_notes`.
**Renders (top→bottom):** `On My Mind` h3 (tutorial `"What you're thinking about and reading, newest first. Dismiss it, save it for later, talk it through, or send it into research."` as `<p>` only when empty, else `title=`). `#onmymind-list`: keyset page of 30 cards newest-first. Each card: ticker chip (or `reading` for doc/link, else `unattributed`), channel/type chip, optional amber `wondering` badge, accent ladder badge (`saved`/`discussing`/`in research`/`in worldview`), timestamp; body (musing = prose; link = anchor `target="_blank" rel="noopener noreferrer"`; doc = **filename** + caption); optional collapsible `<details>` "Brief attached" / "Stress-test attached" (takeaways `<ul>`, Bull/Bear lines, stress adds "What would change your mind"/"Second-order"/"Your book", `from <source>` line); ladder button row. Then `#onmymind-more` with **Load more** (absent text on last page).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Incorporate (primary) | click `[data-om-verb="incorporate"]` | `POST /api/onmymind/<id>/incorporate` → idempotent research task (`ensure_task_for_note`), ladder badge → `in research` | Missing note ⇒ 404 `{ok:false}`, button re-enables. Re-click never dupes a task. | P0 |
| Discuss | click | `POST .../discuss`; badge → `discussing`; opens `res.thread_url` (`/ticker/<T>` or `/`) in a new tab (`window.open _blank noopener`) | Popup blocker may eat the tab; state still saved | P1 |
| Save for later | click | `POST .../save`; badge → `saved` | — | P1 |
| To Worldview | click (only when `LEDGER_WORLDVIEW=1`; slot just before Dismiss) | `POST .../worldview` → stages `proposed` Tenet from note body (no LLM); badge → `in worldview` | Flag off: button absent. `Nothing to stage.` ⇒ 404 | P1 |
| Dismiss (danger, last) | click | `POST .../dismiss` → archives note; `res.removed` ⇒ card removed from DOM | 404 re-enables button | P0 |
| Load more | click `[data-om-more]` | `GET /api/panel/musings?fragment=onmymind&cursor=<c>`; response outerHTML-replaces `#onmymind-more` (cards + fresh control) | Last page ⇒ empty `#onmymind-more`; fetch fail re-enables | P1 |
| Brief expand | click `<details>` summary | Native toggle, no network | — | P2 |
| Unknown verb | direct POST | 400 `{"error":"unknown verb ..."}` | — | P2 |

### States to verify
- Empty feed: `"Nothing on your mind yet — capture a thought, or send a reading (a link, a deck) to your Telegram bot."`
- Flag off: no section, no `#onmymind-list`, Musings list + chip present instead.
- Buttons `disabled` while in flight (double-click safety).

## Worldview section (Tenets)
**Reach:** `#ledger-jump-worldview`. **Preconditions:** `LEDGER_WORLDVIEW=1` (off ⇒ empty string, chip still rendered); distill needs flagged (saved/incorporated) musings + LLM budget.
**Renders (top→bottom):** `Worldview` h3 (tutorial `<p>` only when zero Tenets). `#worldview`: add-box (`.wv-add`) — textarea `#wv-tenet-text` placeholder `A belief about HOW you invest — e.g. 'I sell my winners too early; let a working thesis run.'`; input `#wv-tenet-scope` placeholder `topic slug (optional) — e.g. exit-discipline; reuse one to revise`; row: primary **Add Tenet** (`#wv-add-btn`), **Distill from flagged musings** (`#wv-distill-btn`), status span `#wv-status`. Then `Proposed — approve to adopt` h4 with proposed cards (scope slug, optional amber `tension` badge + note `Overlaps a standing Tenet on this topic — approving revises it (supersede chain).`, `proposed` badge, `from N musings`/`owner-stated`, body, **Approve**/**Reject**); then `Your Worldview` h4 with current cards (scope, `you`/`distilled`, from-N, body — no buttons).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Add Tenet | click `#wv-add-btn` | Status `Adding...`; `POST /api/tenets` `{body_md, scope_key}` → lands `current` immediately (provenance `owner`); reload via `GET /api/panel/musings?fragment=worldview` outerHTML-swaps `#worldview` | Empty body ⇒ focus, no POST (server: 400). Reused scope_key supersedes prior Tenet. Fetch fail: `Could not reach the server.` | P0 |
| Distill | click `#wv-distill-btn` | Status `Distilling from flagged musings...`; `POST /api/tenets/distill`; on success status `<N> proposed from <M> flagged.` + reload | Nothing flagged ⇒ `0 proposed from 0 flagged.` ($0, no LLM). Server exception ⇒ 500 `{"error":"distill failed: ..."}`, JS shows `Distill failed.` | P1 |
| Approve proposed | click `[data-tenet-action="approve"]` | `POST /api/tenets/<id>/approve` → status `current` (supersedes same-scope prior); reload moves card to Your Worldview | 404 unknown id re-enables button | P0 |
| Reject proposed | click `[data-tenet-action="reject"]` | `POST /api/tenets/<id>/reject` → retired; card gone after reload | 404 re-enables | P0 |
| Edit | — | No in-card edit exists; "edit" = reuse the scope slug in the add-box (revise via supersede) | Unknown action ⇒ 400 | P2 |

### States to verify
- Empty: `"No Tenets yet — state a belief about how you invest above, or distil one from the musings you've flagged."`
- Flag off: section absent; Worldview jump chip still scrolls to empty div; `To Worldview` ladder rung absent.
- Tension badge + note render only when `meta.tensions` non-empty.

## Stances ("What you think now")
**Reach:** `#ledger-jump-stances`. **Preconditions:** synthesis stage has produced `insights` rows with `kind='stance'`; else section renders nothing (hide-don't-stub).
**Renders:** h3 `What you think now`, sub `Your current stance per holding, synthesized from your musings and grounded in the ones it cites.`, then left-bordered cards: ticker label + `from N musing(s)` meta + prose body.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| (none — read-only) | — | No buttons, no endpoints | Zero stances ⇒ whole section absent (empty div, chip still scrolls) | P2 |

### States to verify
- No stances: no heading, no empty-state text — verify no orphan heading.

## Research section
**Reach:** `#ledger-jump-research`. **Preconditions:** wondering tap `LEDGER_RESEARCH_TAP` (default on) creates tasks; **Research it** button + `/run` route both gated on `LEDGER_RESEARCH_RUN=1` (default off).
**Renders (top→bottom):** `Research` h3 (tutorial `"Wonderings I detected in your musings, and the inert proposals they produced — approve, dig further, steer, or reject. Nothing acts until you say so."` — visible `<p>` only when list empty, else `title=`); runs-off line when RUN flag off: `Research runs are off — wonderings are collected and run when you enable research.`; tap-health line `Tap health (7d): N tapped · N chips …` or `Tap health (7d): no musings tapped — capture something and this line should move.`; `#ledger-research`: `Proposals to review` h4 — ONE group card per run (ticker, meta `tier · kind` or `saved view`, bold title, prose body, `Also drafted: … (approve applies it too).` rider per companion, action row); then `Open wonderings` h4 — chips (ticker/`unattributed`, `wondering` label, claim text, buttons).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Approve (memo group) | click `[data-verb="approve"]` | `POST /api/research/proposal/<pid>/approve` per pid in `data-pids` (Promise.all) → status approved + `apply_approved_proposal` (`applied` in JSON); then `GET /api/panel/musings?fragment=research` outerHTML-swaps `#ledger-research` | Apply exception ⇒ `applied:"apply failed: …"`, never a 500. Unknown verb ⇒ 400 | P0 |
| Research further | click `[data-verb="further"]` | Same POST with verb `further`; status flips; card leaves pending list | — | P1 |
| Steer | click `[data-verb="steer"]` | In-card editor inserted after button row: textarea `.ledger-rewrite-ta` placeholder `How should I steer this research?` + primary **Steer** + quiet **Cancel** (`data-editing=1` guard blocks a second editor) | Empty text ⇒ focus. Cancel removes editor. No Escape handler. Save POSTs `{steer_text}` per pid → body appended `**Owner steer:** …`, then reload | P0 |
| Reject | click `[data-verb="reject"]` (danger) | POST verb `reject`; card gone on reload | — | P0 |
| Save view / Discard | view-only group | Same endpoints, verbs approve/reject; labels **Save view** / **Discard**; body in owner words via `describe_view_spec` | Malformed artifact_json falls back to stored title/body | P1 |
| Research it | click `[data-run-task]` (only when RUN on) | Button → `Researching...` disabled; `POST /api/research/task/<id>/run` → two-pass engine → proposal; reload | RUN off: route 403 `research run disabled; set LEDGER_RESEARCH_RUN=1` (and button not rendered). Failure ⇒ 500 `research failed: …`, button restores `Research it`. Already researched ⇒ 409 | P0 |
| Dismiss wondering | click `[data-reject-task]` (danger) | Button → `Dismissing...`; `POST /api/research/task/<id>/reject` → task `rejected`; reload | 404 unknown id; cross-site `Sec-Fetch-Site` ⇒ 403; failure restores label `Dismiss` | P1 |

### States to verify
- Empty: `"No open wonderings or proposals yet. Capture a wondering — "do NU's margins still hold?" — and it shows up here to research."`
- RUN off: only **Dismiss** on wondering chips + section-level runs-off line (no env-var text on cards).
- One card per run even with memo+view companions (no duplicate cards).
- Tap-health line absent if `capture.audit` read throws (no 500).

## Reconcile section
**Reach:** `#ledger-jump-reconcile`. **Preconditions:** seed notes/themes awaiting verdicts or inferred falsifiers (`synthesis.reconcile.list_unreconciled`); armed table needs `db_path` + open decisions with conditions.
**Renders (top→bottom):** `Reconcile` h3; sub `Only what genuinely needs you — falsifiers I would quote back at you must be in your own words.`; optional auto-line `Auto-resolved N for you: X played out · Y kept live · Z moot falsifiers dropped (positions closed).`; hidden receipt div `#ledger-receipt` (sibling of the list — survives fragment swaps); `#ledger-reconcile`: optional missing-falsifier line `N live decisions need a falsifier: <label> — [add]…`; falsifier cards (label + `inferred falsifier` chip, body ≤400 chars, **Ratify as mine** / **Rewrite** / **Drop**); note/theme cards (source-ref/label chip, body, **Still live** / **Superseded** / **Rejected** / **Played out**); then `Armed falsifiers (N)` h4 table — columns Ticker / Falsifier (100-char truncated, full text in `title=`) / Since (date) / Decision `#<id>` linking `/#decisions_record`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Verdict buttons | click `[data-rec-verdict]` | `POST /api/reconcile/<note|theme>/<id>/<live|superseded|resolved-rejected|done>`; reload `?fragment=reconcile` outerHTML-swaps `#ledger-reconcile` | Unknown kind/verdict ⇒ 400; unknown id ⇒ 404 `{ok:false}` | P0 |
| Ratify as mine | click `[data-falsifier-action="ratify"]` | `POST /api/reconcile/falsifier/<decision_id>` `{action:"ratify"}`; receipt into `#ledger-receipt`: `armed — now watched by the tripwire engine` or `ratified — queued for arming (next extraction pass)` (read-only `arming_status`, zero LLM); reload | Receipt must survive the list swap (sibling div). 404 unknown decision | P0 |
| Rewrite | click `[data-falsifier-action="edit"]` | In-card swap: `.ledger-editable-body` → textarea PRE-FILLED with current text (cursor at end) + **Save**/**Cancel**; `data-editing=1` blocks re-entry | Cancel restores original HTML. Empty save ⇒ focus. No Escape/overlay. Save POSTs `{action:"edit", text}` → 400 if text empty server-side; receipt (if any) + reload | P0 |
| Drop | click `[data-falsifier-action="drop"]` (danger) | POST `{action:"drop"}`; card gone on reload | 404/400 per route | P1 |
| add (missing-falsifier) | click primary **add** | Same edit flow — beginRewrite on… note: the `add` button is NOT inside a `[data-rec-card]`, JS `f.closest('[data-rec-card]')` — verify it opens (QA watch item) | If no enclosing card, click is a no-op — flag as bug if observed | P1 |
| Decision # link | click `#<id>` in armed table | Navigates to `/#decisions_record` panel | — | P2 |

### States to verify
- Empty + auto-line: section collapses to h3 + auto-line + receipt div + armed table (no sub, no JS).
- Fully empty: `"Corpus reconciled — nothing awaiting a verdict."`
- Pre-0130 DB (no `decided_by` column): degrades to empty state, no 500.
- Armed table hidden at N=0 or `db_path=None` or read failure.

## Set-ticker chips (needs_ticker musings)
**Reach:** inside plain Musings list cards (`#ledger-list`, On My Mind OFF). **Preconditions:** musing with `context.needs_ticker` + non-empty `ticker_candidates`.
**Renders:** card head shows amber `NEEDS TICKER: A, B` badge; below body a wrapping row of `k-chip` buttons, one per uppercased candidate.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Ticker chip | click `[data-set-ticker]` | Button disables; `POST /api/notes/<note_id>/set_ticker` `{ticker}`; on ok re-fetch `GET /api/panel/musings?fragment=list` into `#ledger-list` — card now shows ticker label, chips gone | Missing ticker ⇒ 400 `ticker required`; note already has ticker ⇒ 400; unknown note ⇒ 404; failure re-enables chip | P1 |

### States to verify
- No candidates: plain `needs ticker` badge, no chip row.
- Chips absent once attributed.

## Musings list + note lifecycle
**Reach:** `#ledger-jump-musings` (only when `LEDGER_ONMYMIND` off); fragment `GET /api/panel/musings?fragment=list`. **Preconditions:** :7421 server; `kind='musing'` notes (limit 200).
**Renders:** `Musings` h3; `#ledger-list` of cards newest-first: ticker label / needs-ticker badge / `unattributed`, uppercase channel chip (e.g. `tray`, `telegram`), mono timestamp right-aligned, prose body, optional set-ticker chips.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| (cards themselves are read-only besides set-ticker) | — | — | — | P2 |
| Lifecycle REST (no panel buttons; Journal panel/API surface) | `POST /api/notes/<id>/<action>` | `resolve` (`{resolution_note?}`), `archive`, `unarchive`, `set_ticker` (`{ticker}`), `reclassify` (`{kind}`), `supersede` (`{body, kind?}` → returns chained replacement), `link`/`unlink` (`{decision_id?/position_entry_id?, auto_resolve?}`), `route` (`{intent}`, mirrors onto source comment). Returns `{note:…}` JSON. | Unknown action ⇒ 404; bad kind / missing supersede body / dangling link target ⇒ 400/404; HTMX archive returns done-chip HTML `✕ archived` + Undo (`/api/notes/<id>/unarchive`) | P1 |

### States to verify
- Empty: `"No musings yet - capture a thought above, or send one (voice or text) to your Telegram bot."`
- On My Mind ON: this whole section absent (empty `#ledger-jump-musings` div) — no duplicate feed.
- Whole panel `GET /api/panel/musings` returns 200 on a fresh/thin DB with every section in a degraded-but-rendered state (no 500 from counts, tap health, reconcile, armed table, or auto-reconcile reads).

---

# Part 5 — Portfolio panels (Decisions, Risk, Triggers, Memos, lifecycle)

## Portfolio → Decisions (allocation-decisions record)

**Reach:** `http://127.0.0.1:7421/#decisions_record` (Portfolio sub-row "Decisions"; legacy hashes `#decisions`, `#thesis_ledger` redirect here). Fragment: `GET /api/panel/decisions_record[?user_id=]`. **Preconditions:** :7421 comments_server; portfolio tracker on :8000 for weight/α columns (degrades otherwise); tables `position_sizing_intent`, `thesis_ledger_entries`, `analyst_notes`, `advisor_memos`/`stance_scores` (0077+), `coach_pings`/`coach_mutes` (0131+) — every read tolerates a missing table.

**Renders (top→bottom):** Calibration KPI hoist strip (`N decisions · N graded · X% hit rate · N reversed (v vindicated · c cost)`) — hidden when 0 decisions; **Sizing audit** panel (h2 "Sizing audit", sub "Stated posture … nothing here is a directive.") with notes (tracker-offline note, alpha-window/coverage note, or "No sizing intents recorded yet — use **record** on a row…") and a table `Ticker | Thesis | Conviction | Target | Weight | vs DCF FV | α vs SPY | Mismatch | [record]`, ranked worst mismatch first, each score point rendering a reason chip, "aligned" when none; **Decision calibration** section (same KPI line, "Am I getting better?" trend sparkline + per-period table with thin-n `*`, hit-rate-by-conviction table with Wilson 95% CI, Brier line, batting-vs-slugging expectancy line, Process × outcome matrix, response-mix line, time-to-outcome line, Errors-of-omission block, **Reversals** table (Made/Ticker/Call/How it graded, max 8, "reversal vindicated"/"reversal cost")); **Skill decomposition** ($ KPI strip total/selection/sizing/timing, Jensen α line with "could be luck" hedge, edge/leak read, Conviction → outcome table) — hidden when tracker offline; **Coach's read** scorecard (latest persisted card; starvation stub "Coach's read: no scorecard yet — generated monthly once graded decisions accrue (currently N graded)."; exception → "Coach's read: failed to load — see logs."); **Coach P&L** ("Reviews run: **0** — the guard has never been exercised." or counts line + "Decisions changed by the coach: **N** · Q3'26 target: **1**" with title tooltip "v1 heuristic: a guard_override review counts as changed when no owner sell/trim … within 30d … a proxy, not a causal claim." + "reviews →" peek link when reviews_run>0); **Coach pings (this month)** (one line per ping: class · ticker · status pill · date; empty: "No pings this month."); **Active mutes** (per-row Unmute button; empty: "No active mutes."); **Digest queue** (class · ticker · age lines; empty: "Digest is empty."); **Decisions timeline** (living-grid filter bar "Filter by ticker / kind / text…", sortable Date/Ticker/Kind + Decision prose; empty: "Nothing recorded yet. Approving an alert action, recording a sizing intent above, or capturing a decision note all land here.").

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| "record" button per audit row | click `.ad-edit-btn` | Toggles hidden editor row (Conviction 1–5 select, Target % number 0–100 step .5, Why text, Save intent) | Second click re-hides; pre-fills current values | P0 |
| "Save intent" | click `.ad-save-btn` | POST `/api/sizing-intents` `{ticker, conviction?, target_weight_pct?, narrative?}` → 200 `{created_ids}` → whole fragment refetched (`/api/panel/decisions_record`), scripts re-executed | Neither field set → inline status "set a conviction and/or a target first" (no POST); server 400 on out-of-range (conviction 1–5, target 0–100); network fail → status "failed (…)"; refetch fail → "Failed to reload (…)" | P0 |
| "unmute" button per mute row | click `.cpnl-unmute-btn` | Button disables, POST `/api/coach/unmute` `{class_}` → row removed from DOM (server clears coach_mutes row) | Non-OK → button re-enables (row stays); empty class_ → 400; double-click blocked by disabled | P1 |
| "reviews →" link (Coach P&L) | click | Peek overlay of `/api/peek/memo/position_review` (title "Latest position review"); href fallback `#advisor_memos` | Only rendered when reviews_run > 0 | P1 |
| Ticker link in audit row | click | Navigates `/ticker/<T>` | — | P1 |
| Timeline filter box / sortable th | type / click | Client-side living-grid filter/sort on data-* keys | Empty result set | P2 |
| Hover: conviction/target/weight cells, FV-gap cell, sparkline dots, CI/reversal-cost th | hover | Tooltips: "stated <date>", market value, "vs bear case ±N% · vs bull case ±N%", "<period>: N% (n=…)" | FV tooltip only when scenario range exists | P2 |

### States to verify
- Empty DB: audit "No research portfolio holdings found (tracked_companies has no portfolio rows)."; calibration "No decisions recorded yet. The morning pipeline's stage 0b extracts…"; no KPI hoist; all four coach sections still render (stubs, never absent); timeline empty line. No 500.
- Tracker offline: weight/α dashed, note "Tracker offline — weight and alpha columns are dashed (<api_url>…)", skill-decomposition section hidden.
- Pre-0077/0131 DB: Coach P&L all-zero line; pings/mutes/digest empty states; no exception.
- Trend block hidden with <2 graded periods; process matrix / Brier / expectancy / omission hidden below their gates.

## Portfolio → Risk (whole-book risk cockpit)

**Reach:** `#portfolio_risk`; fragment `GET /api/panel/portfolio_risk`. **Preconditions:** :7421 server. Tracker :8000 feeds beta/drawdown/factor/risk-reward-gap; style/correlation/tail-stress/collision/macro-stress compute from local disk/DB and render tracker-down. Scenario run needs `execution/run_scenario.py` + LLM budget.

**Renders (top→bottom, tracker up):** `#pfr-root` wrapper: **Risk & efficiency** (KPI strip: Beta vs SPY, Alpha (ann.), Sharpe, Sortino, Info ratio, Tracking error, Portfolio σ, R²); **Drawdown** (Max drawdown w/ peak→trough sub, Current drawdown ("at a high"/"below peak"), Recovery card ("none needed"/"Xd"/"underwater"), red underwater SVG area chart); **Factor & style exposure** (Market β, Growth β, Growth tilt, Crowding, Rate β (10Y) cards + "N of M names priced" note + "Most market-sensitive / Most growth-leaning / Most crowded" ticker lines); **Style factor loadings** (Value/Size/Momentum β cards, proxies-through note, missing-proxy warning, largest-tilt lines; empty state names `python execution/fetch_factor_proxies.py`); **Holdings correlation & crowding** (Avg pairwise corr + Largest cluster cards, cluster prose lines "X + Y — N% of book moves as one bet", heat-toned pairwise matrix in `overflow-x` scroller, "Not modeled: …" dropped note; empty: "Not enough daily price history…"); **Scenario-tail stress** (All-bears book drawdown, Coverage, Stale weight cards + per-name table Weight/Live price/Bear FV/Bear return/Contribution with ⚠ low-confidence hovers; empty: "No weighted holdings to stress yet…"); **Thesis collisions** (cached LLM findings: cluster + contradiction cards, stale-portfolio note dropping sold names; empty names `python execution/run_thesis_collision.py`); **Risk vs reward vs conviction** (provenance sub-line, table Ticker/Weight/Risk share/Reward share/Gap/Exp. return/Conviction/Mismatch with score pill + chips) — hidden tracker-down; **Whole-book macro stress** (Scenario `<select>` + "Run scenario" primary button + status span + hidden `<pre>` log + cached "Stress digest" markdown or "No stress digest cached yet — pick a scenario and run it…"). Tracker down: last-known **Risk & drawdown** cached-snapshot card ("showing the last-known snapshot (as of …). These are cached values, not live…") or offline note, then the four local sections + macro stress.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| "Run scenario" | pick option, click `#pfr-run-scenario` | POST `/actions/run-scenario` `{scenario}` → 201 job; button disables, msg "running… (LLM digest, ~10-40s)", log `<pre>` shows; SSE `/actions/stream/<id>` lines append; on done → "done — refreshing digest…" → refetch `/api/panel/portfolio_risk` and reinject at `#pfr-root`'s panel body | No selection → msg "pick a scenario"; unknown id → 400 "unknown scenario"; concurrent job → 409 error shown; SSE error → close + re-enable button; refetch fail → button re-enabled | P0 |
| Scenario select | change | Sets payload only (registry-ordered options) | — | P1 |
| Ticker links (factor tops, clusters, tail rows, RRG rows) | click | `../research/<T>/` static report | — | P1 |
| Corr-matrix cell hover | hover | title "A x B · +0.83" | — | P2 |
| ⚠ hovers (tail/RRG) | hover | confidence_reason tooltip | — | P2 |

### States to verify
- Tracker down: cached snapshot (stamped) or plain offline note; style/correlation/tail/collision/macro-stress still render; RRG absent.
- Thin DB (post-#791 hotfix): every local section shows its empty state — no 500.
- Digest refresh after run actually shows new "Latest: <scenario title> · <date>" stamp.
- Double-click Run guarded by `dataset.wired` + disabled button.

## Portfolio → Performance

**Reach:** `#portfolio` (section landing); fragment `GET /api/panel/portfolio?start_date&end_date&include_backfill=1`. **Preconditions:** :7421; tracker :8000 (page auto-starts it when down).

**Renders:** tracker-down → leading **Portfolio tracker** banner (warn-edged, "Start tracker" button, "starting automatically…" msg, hidden log, `<details>` "Start it manually · technical detail" with uvicorn command). Tracker up → **Performance vs benchmarks** (header with hover "i" methodology popover + window bar: 1M/3M/6M/YTD/1Y/2Y/Default preset buttons, two date inputs, "Apply", "modeled backfill" checkbox w/ tooltip; KPI cards Portfolio TWR + vs SPY; color-keyed legend chips Portfolio/SPY/QQQ/Policy; multi-series SVG chart; policy-mix line, unbalanced warning; backfill-unreliable ⚠ note); **Risk & efficiency**; **Positioning & concentration** (Positions/Top1/Top5/Top10/HHI/Effective holdings/Avg corr cards + By asset type/sector/region/account bar blocks); **Per-position alpha** (living-grid filter + sortable table Ticker/Name/Value/P&L/SPY P&L/α vs SPY/α vs QQQ[/α vs policy] + totals row, ⚠ incomplete-row flags); **Live portfolio** (total + tax-bucket KPI strip, sortable positions table, **Latest transactions** table). Failed analytics endpoints listed once: "Unavailable from the tracker right now: …".

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Window presets / Apply / backfill checkbox | click | Refetch fragment with computed `start_date/end_date/include_backfill`, "Loading…" placeholder, scripts re-run | Fetch fail → "Failed to load (…)"; Default clears dates | P0 |
| "Start tracker" (also auto-fires once per page load) | click / auto | POST `/actions/start-tracker` → job + SSE log stream; polls `/api/panel/portfolio` every 3s ×30 until `pf-live-offline` gone, then reinjects (or `location.reload()`) | 409 (already starting) treated as success→poll; 404 no sibling checkout → error msg; timeout → "tracker still not reachable — check the log below" + button re-enabled; `window.__pfTrackerAutostart` guards start loop | P0 |
| "i" hover/focus on title | hover | Methodology popover (Modified Dietz, synthetic benchmarks, net inflow) | keyboard-focusable (tabindex=0) | P2 |
| Table filter/sort; ticker links (`../research/<T>/`) | type/click | client re-order/filter | — | P2 |

### States to verify
- Tracker down: banner leads, auto-start fires once, manual `<details>` present; no dead bottom card.
- Tracker up, analytics endpoints failing: "The tracker is reachable but its analytics endpoints aren't — …" single note (+ cached risk section when snapshot exists).
- Empty positions: "Tracker reachable, but it reports no current holdings."

## Portfolio → Synthesis

**Reach:** `#portfolio_synthesis`; fragment `GET /api/panel/portfolio_synthesis`. **Preconditions:** :7421; tracker optional (equal-weight fallback, no offline card here).

**Renders:** insights grid — **Thesis health** ("N OK · M flagged" + tone chips `T · status` deep-linking `#holding=<T>` with `data-peek-ticker` hover cards; hidden when no evaluations) and **Exposure** (FMP-sector bars, sub "weighted by live position" or "equal-weighted (tracker offline)"); **Where the next dollar goes** (softmax distribution rows: ticker link, bar, alloc %, "now X%", hover/focus reveals factor-waterfall chips Return/Diversification/Macro with z/weight/raw tooltips; hidden-factor + not-scored warning lines; hint "Hover or focus a row for the factor waterfall…"; below it **Advisor memo** excerpt with "full memo →" peek `/api/peek/memo/next_dollar`, hash fallback `#advisor_memos`); cached **cross-portfolio lens memo** fragment. Hide-don't-stub: each panel disappears when its substrate is absent.

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Thesis chip | click / hover | Navigate `#holding=<T>` / peek mini-card | — | P1 |
| Next-dollar row | hover/focus (tabindex=0) | Factor chip waterfall reveals; chip tooltips | keyboard focus works | P1 |
| "full memo →" | click | Peek overlay of rendered next-dollar memo | no memo → whole excerpt absent | P1 |

### States to verify
- No DB / no portfolio rows: panels hide; page may be near-empty but no 500.
- Tracker offline: exposure/next-dollar fall to equal-weight with explicit sub-line.

## Portfolio → Memos (advisor memos)

**Reach:** `#advisor_memos`; fragment `GET /api/panel/advisor_memos[?user_id=]`. Standalone think-through page: `GET /socratic/<TICKER>` (auto-starts flow). **Preconditions:** :7421; LLM budget for runs; `advisor_memos` (0077+)/`stance_scores` (0078+) degrade to run-bar+screen only.

**Renders:** **Run bar** ("Advisor" label; primary "Generate next-dollar memo"; quiet "Run swap checks"; holdings select + "Think through…" when holdings exist; note "Evidence + framing, never directives…"; hidden log `<pre>`); hidden **Socratic think-through** panel shell; **Swap-discipline screen** (deterministic table Holding/Upside/Best alternative/Upside/Margin/Bar with pills "clears the bar" (warn) / "discipline holds" (ok); implausible-upside exclusion note; empty: "No screen rows — needs holdings and external names with usable DCF runs."); **Memo record** (track-record strip "Track record — Stances: x/y correct · avg excess ±pp · Swap screens: v/w validated" once graded; newest-first `<details>` cards: kind chip, scope, stance pill + graded verdict pill (e.g. "correct +3.2pp vs SPY") or "scoring pending", title, date, note#/ledger# backlinks; empty: "No memos yet — generate the first next-dollar memo above.").

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Generate next-dollar memo / Run swap checks | click (data-kind) | All buttons disable; POST `/actions/advisor-memo` `{kind}` → 201 job; SSE log lines into `#am-log`; on exit 0 → refetch fragment (new memo appears) | 409 concurrent → "failed: …"; nonzero exit → no refetch, buttons re-enable; invalid kind 400 | P0 |
| "Think through…" | click with select value | Unhides Socratic panel, POST `/api/socratic/questions` `{ticker}` — status "Generating pointed questions for T… (15-45s)" → renders 3–5 question textareas + horizon select (30/90 default/180/365) + "Write the decision memo" | fail → "Failed to generate questions: … — pick the holding and retry." | P0 |
| "Write the decision memo" | click | Validates ≥1 non-empty answer else "Answer at least one question — the memo is written from YOUR read."; POST `/api/socratic/memo` → "Saved as memo #N — stance: … scoring pending" + rendered body | fail → button re-enables, "Memo failed: … — your answers are still in the form; retry." | P0 |
| Memo card summary | click | `<details>` expand/collapse rendered markdown body (12k cap) | ▸/▾ marker | P1 |
| Screen filter/sort; ticker links `/ticker/<T>` | type/click | living-grid behavior | — | P2 |
| `/socratic/<T>` page | URL | Full dark page, flow auto-starts for T; links back to `/#advisor_memos` | — | P1 |

### States to verify
- Pre-0077 DB: screen + run bar still render, memo record empty — no 500.
- Score pills: verdict tones (correct/screen_validated ok, wrong/refuted bad, mixed warn, unscoreable muted); tooltip "graded d1 → d2 · basis …".

## Portfolio → Triggers (holdings panel)

**Reach:** `#holdings` (alias `#triggers`); fragment `GET /api/panel/holdings` (falls through PANEL_TO_SECTION → `trigger_ladder`). **Preconditions:** :7421; `dcf_runs` populated.

**Renders:** single **Trigger ladder** panel — sub "Every holding positioned by DCF over/under vs MoS bar. Sorted by absolute deviation."; table Ticker/List/Verdict/Live/Fair value/Over-under/MoS bar/Trigger, row tone by trigger_status; watchlist names render dashes (not blank rows). Empty state: "No DCF runs yet. Run `python execution/refresh_dcf.py --all-named`."

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Ticker link | click | `../research/<T>/` report | — | P1 |
| `#triggers` alias | URL hash | Redirects onto `holdings` panel | — | P2 |

### States to verify
- Empty dcf_runs → CLI-hint empty state, no 500; NULL live/fair/OU/MoS render "—" cells (regression: previously dropped row opener).

## Position lifecycle (Holding tab · Ops drawer section)

**Reach:** inside Companies → Holding's Ops drawer; fragment `GET /api/position-lifecycle/<TICKER>[?user_id=]`. **Preconditions:** :7421; `position_entries` (0088+); rows created by the morning reconciler.

**Renders:** **Position lifecycle** panel — vertical timeline: open stint first (green dot, "date → open", `OPEN` pill, entry price, conviction pill, "source: …" meta, entry-thesis excerpt, falsifiable-conditions list w/ tooltip "Falsifiable conditions snapshotted at entry"), then closed stints (dates, outcome pill "thesis played out"/"thesis broke"/"mixed"/"exit unrelated to thesis", price → price (±%), "Why exited:"/"Lessons:" when graded). Closed-but-incomplete stints get an inline grade form. Empty: "No lifecycle rows yet. The morning reconciler opens one when this name enters the portfolio (and closes it on exit) — entry price/date come from the tracker when it's online."

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| "Save grading" | form submit (`data-plc-grade`) | POST `/api/position-entries/<id>` `{exit_reason, lessons, outcome_vs_thesis}` → refetch `/api/position-lifecycle/<T>` and swap section outerHTML (fresh script self-wires) | Unknown outcome → 400; missing row → 404; pre-0088 DB → 500 `{"error":"position_entries unavailable (pre-0088 DB?)"}`; fetch error → message into `.plc-note` span; empty strings clear fields | P0 |
| Outcome select | change | picks one of OUTCOME_VOCAB labels ("outcome vs thesis…" placeholder) | pre-fills existing value | P1 |

### States to verify
- Re-inject double-wiring guarded (`data-wired`); form disappears once all three fields set.
- Corrupt entry_conditions JSON renders nothing (no crash).

---

# Part 6 — System / Provenance console, Settings, Actions

## System entry — ▦ button + status dot (shell chrome)

**Reach:** `http://127.0.0.1:7421/` top bar, right utility cluster: `▦` icon button (`.cc-system-btn`, `data-theme-target="system"`); hash `#provenance` or any legacy alias (`#system`, `#health`, `#section_coverage`, `#ir_coverage`, `#source_calls`, `#cron_health`, `#dcf_coverage`, `#evals`, `#validation`, `#restatements`, `#model_eval`) lands on the same panel; also reachable via ⌘K palette (Ctrl+K / Ctrl+Space). **Preconditions:** comments_server running (`python execution/comments_server.py --port 7421`). Status dot needs `.tmp/daily_chain_status.json` (written by `execution/verify_daily_chain.py` at the end of the morning pipeline); file absent/unparseable or `repo_root=None` → no dot at all (pre-PR9 look).
**Renders (top→bottom):** a quiet icon button showing `▦` plus, when the status artifact reads, a small dot: green `cc-system-dot-ok` (verdict `ok`, title suffix "Morning pipeline OK"), red `bad` (verdict `missing` → "Morning pipeline has not run today", or `failed`/foreign verdict → "Morning pipeline failed — <error_summary>"), amber `warn` (verdict `db_error` → "Cron status unreadable"). Title = "System · Provenance · <summary>". `system_status_summary` is a single JSON file read — never a DB scan.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| ▦ button | click | Activates the System section; lazy-loads `GET /api/panel/provenance` into the panel container | First load shows loader; fragment cached (SWR + ETag on `/api/panel/*`) | P0 |
| Status dot hover | hover | Tooltip shows section names + pipeline summary | No dot when artifact missing (fresh checkout) — must not error | P1 |
| Legacy hash | navigate `#validation` etc. | JS redirect map (mirror of `_LEGACY_PANEL_REDIRECTS`) lands on Provenance | `#budget` / `#actions` land on System AND auto-open the Settings drawer (`DRAWER_OPENERS = {budget:1, actions:1}`) | P1 |

### States to verify
- No `.tmp/daily_chain_status.json` → button renders with no dot, no 500.
- Artifact with `verdict:"failed"` + `error_summary` → red dot, summary in title.
- Non-dict JSON in artifact → treated as absent (no dot).

## Provenance console (System → Provenance)

**Reach:** `GET /api/panel/provenance` (fragment), rendered by `src/pipeline/provenance_panel.py::render_provenance_panel`; in-app hash `#provenance`. **Preconditions:** server + `data/research.db`; every section degrades independently.
**Renders (top→bottom):** a `panel_toolbar` (title suppressed — the nav owns "Provenance") holding 11 jump chips in display order: **Coverage · Validation · Evals · Optimizer · IR Docs · Data Cache · Cron Health · DCF Coverage · Restatements · Overrides · Credibility** — then the 11 sections stacked, each wrapped in `<div class="prov-sec" id="prov-<anchor>">`. A raised builder is caught by `_safe()` and renders an error card: "This diagnostic failed to render — the rest of the console is unaffected." with a `<details>` holding `Type: msg` + last 1500 chars of traceback.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Jump chip (×11) | click `[data-prov-jump]` | Smooth `scrollIntoView` to `#prov-<anchor>`; **never** changes `location.hash` (hash change would trip the shell router back to Overview) | Guarded single document listener (`window.__ccProvNav`) — re-injecting the fragment must not double-scroll | P0 |
| Error-card `<details>` | click summary | Expands traceback | — | P2 |

### States to verify
- One builder raising → error card only for that section; other 10 intact.
- Empty DB → all 11 sections show their own empty states (below), page still assembles, no 500.
- Clicking a chip must not navigate away from the System tab.

## Section: Coverage (leads the console)

**Renders:** `<h2>Section coverage</h2>`; KPI strip (Tracked names / Built report / Fully covered / Section gaps); living-grid filter bar ("Filter by ticker…", "N names"); horizontally scrollable matrix — Ticker · List · Built date · one glyph column per report section · Gaps count. Glyphs: `●` green populated, `○` muted "no data", `—` "not applicable to this business model", `·` amber "no report built yet"; each cell titled `"TICKER · Label: hover"`. Legend note ends "…this matrix is where those gaps stay visible."
**Actions:** sortable `lg.th` headers (click to sort), filter input live-filters rows, cell hover tooltips. Empty state: "No tracked names found — the coverage matrix fills in once companies are onboarded and reports are built." (P0: matrix renders; P1: sort/filter; P2: tooltips).

## Section: Validation

**Renders:** `<h2>Validation</h2>` + sub copy; KPI strip: Open · halt ("fix before trusting") / Open · warn / Open · source disagreement / Tickers affected (sub "last raised <date>") / Resolved ("all-time"). Then "Open issues by rule" sortable table (Rule/Severity/Open) and "Latest open issues" — `prov_row`s: severity tick, `rule · ticker`, `raw → expected` note, "raised …" stamp, inline **Resolve** button; capped note "Showing the latest N of X open issues (halt first)."

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| **Resolve** (per row) | click `[data-resolve-issue]` | Synchronous `POST /actions/resolve-issue {"issue_id":N}` → row fades + removed; KPI counters (`data-vi-count` halt/warn −1, resolved +1) bump client-side | 404 (already resolved) / network fail → button text becomes **"Retry resolve"**; deliberately NOT `data-prov-post` (that global listener streams) | P0 |
| Rule table sort/filter | click th / type | living-grid sort/filter | — | P2 |

**States:** no table → "No `validation_issues` table in this DB — run `alembic upgrade head`."; zero open → k-well "No open issues. N previously raised issue(s) are resolved. Sweep the book with `python execution/run_validation_engine.py`…". Double-click Resolve → second POST returns 404, button shows Retry, count must not double-decrement.

## Section: Evals

**Renders:** run bar — label "Run eval" + one button per `RUNNABLE_PURPOSES` (23 purposes; `viewspec_compile` alone is primary-styled), note "viewspec_compile = live golden set (16 questions); the rest audit existing artifacts… spot-check its agreement with `execution/spot_check_eval_judge.py`", hidden `<pre id="ev-log">`; then **Latest eval runs** table (Purpose / Avg-score pill green≥0.8, amber≥0.6, red / Pass n/n / Mode pill (`live` accent, `audit` neutral) / Prompt version / Run cost + call count / When + git sha) with a failed-cases `prov_drawer` row per purpose ("N failed case(s) — expected vs actual + judge rationale", each case capped at 1200 evidence chars); **Score by prompt version** chip strip per purpose (chip title carries n/p25/p50/p75); **Call health (30d)** sortable table (Purpose/Calls/Error rate — red >10%/Fallback rate — amber >10%/Cost/Avg ms).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Run-eval button (×23) | click `[data-purpose]` | `POST /actions/run-eval {"purpose":p}` → 201 + `stream_url`; all bar buttons disable; `#ev-log` unhides and streams SSE lines; on `done` exit 0 the panel refetches `/api/panel/evals` and re-renders | 400 unknown purpose; **409 single-flight conflict** if a job holds the slot; SSE `onerror` re-enables buttons; exit≠0 → no refetch, log kept | P0 |
| Failed-case drawer | click summary | Expands prov_cases (score pill, question+stage, rationale, expected/actual JSON) | Absent when latest run has no failures | P1 |

**States:** pre-0083 DB → explainer + run bar only; "No eval runs recorded yet — run one from the bar above (or `python execution/run_llm_evals.py --purpose viewspec_compile`)."; "No calibration scores yet."; "No LLM calls in the window."

## Section: Optimizer (model_eval)

**Renders (all read-only):** **Anonymous-purpose alarm** — green k-well "clear / No anonymous or unregistered LLM spend over $X in the last 30 days." or red "alarm" well + red chips per offending purpose (`purpose=NULL` or unregistered) with 30d cost; **Optimizer steering** — chips (`fresh` pill, or red "no nomination run in Nd" / "no sweep verdict in Nd", per-purpose "…: thin frame" chips), facts line (meta-cost/30d, calls, candidate models, newest nomination/verdict), pending-nominations table (# / Purpose / Kind·tier / Candidates / Why / Source) or "No pending nominations — the sweep runs on discovery…"; **Prompt experiments (A/B)**; **Active model-pin overrides** (incumbent → override, prod tokens, realized savings, set_at) or "No active overrides — every purpose is on its code pin."; **Downgrade verdicts** history (CANDIDATE_ERRORED = infra flag); **Per-purpose cost (30d)**. No POST affordances — verify absence of buttons. States: no DB → "No DB — steering state unavailable."; empty tables → per-section muted lines; no 500 on thin DB. (P1)

## Section: IR Docs

**Renders:** `<h2>IR document coverage</h2>`; KPI strip Auto-fetched x/y ("names with IR docs") / Manual pull needed (red when >0) / IR documents total; filterable/sortable table: Ticker / Name / List / IR docs / Latest period / Last fetched / Last crawl (`never` or `status · date`) / Status — green pill "✓ N docs" or red pill "manual pull" + gap-reason line; manual-pull how-to note. Empty: "No portfolio or evaluation names are tracked yet." Actions: sort/filter only (P2). The actual fetch action lives in the Settings drawer (Refresh IR KPIs).

## Section: Data Cache (+ Panel latency)

**Renders:** `<h2>Data fetch cache</h2>`; KPI strip Cache skip rate (green ≥50%) / Calls avoided "of N attempts" / Error rate (red >5%) / Cost avoided (only when >0); per-(source, kind) sortable table (Source/Kind/Calls/Skip%/Err%/Saved/p50 ms/Records); note. Then **Panel latency** section (`#sc-panel-latency`): client script fetches `GET /api/metrics/panel` and renders perceived p50/p95 + per-panel table (Panel/Path/Loads/p50/p95); in-memory ring, resets with the server.
**States:** zero calls → "No source-call rows yet. Adapters in `src/sources/` log to `source_calls`…" (latency section still renders); no samples → "No samples yet this server run — switch a few tabs, then reopen this panel."; fetch failure → "Metrics endpoint unavailable." (P1)

## Section: Cron Health

**Renders:** `<h2>Cron health</h2>`, sub "Last 7 days… Green = OK · Red = failed · Grey = no run recorded. Auto-refreshes every 60s."; live div `#cc-cron-live` with `hx-get="/api/cron-health" hx-trigger="every 60s"` swapping innerHTML — KPI strip (Today's pipeline OK/FAILED/"Not run yet"; Clean streak Nd, green ≥3) + 7-day dot timeline (DB backup, Morning pipeline first, then other directives alphabetical; dot title = status; amber = in_progress) + Last-status column; CLI note (`verify_cron_registration.py` / `verify_daily_chain.py`).
**Actions:** HTMX 60s poll (P1 — verify verdict flips in place without reload; JS-off still shows server-rendered body). **States:** no `ingestion_runs` rows/table → "No pipeline run rows yet…" (also served by the poll); no 500 on missing table.

## Section: DCF Coverage

**Renders:** `<h2>DCF coverage</h2>` + sub ("Refresh a name with `python execution/refresh_dcf.py --ticker T`"); KPI strip: Maintained / Fresh (valued within FRESH_DAYS) / Stale-never (amber) / plus skipped/failed-sync/no-JSON/orphan cards; filterable table Ticker/List/Model/Workbook/Last valued (`never` possible)/Priced (`pre-0091 run` or `—`)/Assumptions/JSON sync/Note; optional callout "**Stale copies in `dcf/redesign/` (N)** — …superseded and safe to delete: <list>". Empty: "No DCF artifacts found — no briefed names, `dcf/*.xlsx` workbooks, `dcf_runs` rows or assumptions JSONs under this repo root." Read-only; sort/filter only (P1).

## Section: Restatements

**Renders:** `<h2>Restatements</h2>` + sub («"was X, now Y"… Same-value re-reports… are chained but not listed.»); KPI strip Value changed / Chains total / Tickers / KPI chains; filterable table Ticker / Line item / Period / Was / Now / Δ% (green up, red down, `n/m` when old=0) / New filing (doc type · accession · filed date) / **Open** column with `was ↗` and `now ↗` links to `/source/<doc_id>`; cap note "Showing the latest N of X value-changed restatements."
**Actions:** the two `/source/` links open the source-document viewer (P1 — verify both doc ids resolve, no 404). **States:** no `supersedes_id` column → "No restatement chains in this DB — run `alembic upgrade head` (0054 adds supersedes_id)."; zero chains → "No supersede chains yet…"

## Section: Overrides

**Renders:** `<h2>Overrides</h2>` + sub ("Durable company-document overrides that supersede FMP at read/resolve time… See `directives/provenance_override_2026_06.md`."); KPI strip Active overrides / Segment / KPI / Line item; filterable table Ticker / Period+type / Kind / Key / Action (green `replace`, red `drop`, amber `qualify`) / Value ("N segments" for JSON values) / Source (doc type + accession/exhibit) / By.
**Actions:** read-only — record/drop happen via CLI, and the empty state names them: "No active company-doc overrides. Record one with `python execution/record_fact_override.py` or auto-extract from an 8-K with `python execution/extract_8k_overrides.py --apply`." (P1: table + empty copy; sort/filter P2.) Pre-0111 DB (table absent) → same empty state, no 500.

## Section: Credibility

**Renders:** `<h2>Credibility</h2>`; KPI strip Brier score ("lower = better (0.25 = no skill)") / Observed hold-rate (sub "predicted X%") / Observations (confidence note vs min_n) / Sources ("restatements · disagreements"); "Reliability by confidence band" table (Predicted band vs observed) and "Reliability by source tier" table; k-well note on the conditional denominator. **States:** ledger absent → "No confidence-observation ledger in this DB — run …"; zero graded → "No graded observations yet. The ledger fills as later …". Read-only (P2).

## Settings drawer (⚙ Settings)

**Reach:** top-bar button "⚙ Settings" (title "Budgets · ticker settings · maintenance"); hash `#budget` or `#actions` auto-opens it; palette row "Settings & maintenance". **Preconditions:** none beyond the server.
**Renders:** right-slide `aside#cc-drawer` (role=dialog, aria-modal), head "Settings & maintenance" + `×` close; four collapsed `<details class="cc-drawer-sec">` sections that lazy-load on first open via `data-endpoint`: **LLM budgets** (`/api/panel/budget`), **Ticker settings** (`/api/panel/ticker_settings`), **Global DCF assumptions** (`/api/panel/dcf_globals`), **Maintenance actions & job streams** (`/api/panel/actions`); each body starts "Loading…". Open/closed state persists per section in the client store (`drawer:<endpoint>`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| ⚙ toggle | click | Drawer opens via CCOverlay (slide-right, shared `.k-scrim`); click again closes | Second open restores last-open sections and re-runs their loads if needed | P0 |
| Dismiss | Escape / scrim click / `×` (`#cc-drawer-close`) | Drawer closes (CCOverlay triad) | — | P0 |
| `<details>` summary | click | First open fetches endpoint, injects fragment, re-executes its `<script>`; `data-loaded=1` | Fetch failure leaves "Loading…"/error text; state persisted | P0 |

**LLM budgets** (renders inside drawer): "LLM spend & budget" — per-purpose table Purpose/Spend/Cap(editable `.budget-cap` number)/Burn bar/Headroom/Mode(`.budget-mode` select: skip · block · warn)/**Save** per row → `POST /api/llm-budgets/<purpose>` `{cap_usd, on_exceed}`, inline `.budget-msg` result; MTD + projected month-end footer; "By ticker" filterable table. Empty: "No budget data. Run `python -m alembic upgrade head` to install migration 0052, then revisit." (P0)

**Ticker settings**: table Ticker/Bypass-budget pill (`ON` green / `OFF` neutral)/Toggle checkbox/Updated. Checkbox change → `POST /api/ticker-settings/<T>` `{bypass_budget:bool}` → pill swaps in place (`ERR` red pill on failure). Add-form: `TICKER` input + "bypass budget" checkbox + **Save** (primary) → same POST; note "saved T — reopen the drawer to refresh the list" (new row does NOT appear live). Checkbox title warns "uncapped spend for this name". Empty table → "No per-ticker overrides set."; no table → "No `ticker_settings` table — run `alembic upgrade head`." (P0)

**Global DCF assumptions**: three rows — Risk-free rate, Equity / market risk premium, Default tax rate — each a number input (0–1, step .001, "Decimal ratio, e.g. 0.043 = 4.3%"), live "= X%" echo, quiet **Save** → `POST /api/dcf-globals` `{field, value}` with inline "saving… / saved ✓ / error: … / network error"; under each field either "Pinned by N name(s) (the global will not move these): T (FCFF), …" (first 12 + "+N more") or "No per-ticker overrides — all names track this global."; reach note; primary **Rebuild affected models** button → `window.confirm("Rebuild every DCF-maintained name? This re-runs ~all DCF models and can take several minutes.")` then `POST /actions/rebuild-dcfs` (runs `refresh_dcf.py --all-named` as single-flight job `_REPO/rebuild-dcfs`) → button disables, msg "running (<job_id>)…", `#dcfg-rebuild-log` streams SSE lines, "=== done (exit N) ===" on completion; 409 on conflict → "error: …" and re-enable; SSE error → "stream ended". Precedence copy: per-ticker override > global > model fallback. (P0)

## Maintenance actions & job streams (drawer section / `GET /api/panel/actions`)

**Renders:** two blocks from `src/pipeline/dashboard_html.py`. **Refresh IR KPIs**: help copy ("…The ticker needs a parser config in `micro_thesis/ir_config/` (e.g. NU)."), form — ticker input (uppercased), "quarters" number (default 8, 1–40), submit **Refresh IR KPIs**, status span, hidden `<pre id="ir-output">`. **Maintenance**: help "Repo-wide chores, streamed live — the same CLIs the crons run." + buttons **Seed KPI defs** / **Process dropped docs** / **Sweep output history** / **Onboard pending** | ticker input + **Onboard ticker** (title warns "Takes minutes and spends FMP quota."), status span, hidden log `<pre>`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Refresh IR KPIs submit | submit | `POST /actions/refresh-ir {ticker, quarters}` → status "Running T (kind)…", EventSource on `stream_url` appends `[m:ss]`-stamped lines; `done` → "Done in Ns. KPIs ingested at IR-doc tier." (exit 0) or "Failed (exit N) after Ns. See log above." | Empty ticker → "Enter a ticker."; HTTP error → "Error: <msg>"; stream drop → "Stream interrupted - is the server still running?"; submit disabled while running | P0 |
| Seed KPI defs / Process dropped docs / Sweep output history / Onboard pending | click | `POST /actions/maintenance {"action": seed_kpis\|process_inbox\|sweep_history\|onboard_pending}` → 201 job (`seed_kpi_definitions.py --all`, `register_dropped_documents.py --all`, `sweep_output_history.py`, `onboard_pending_tickers.py`), all buttons disable, SSE log streams, "Done in Ns." / "Failed (exit N)…" | Unknown action → 400 listing valid; **409 RegistryConflict** while another job holds the slot → "Error: …" | P0 |
| Onboard ticker | click with ticker | Same POST with `{"action":"onboard","ticker":T}` → job `maint-onboard` per-ticker slot | Empty → "Enter a ticker to onboard."; invalid symbol → 400 "invalid ticker" | P1 |
| SSE channel | `GET /actions/stream/<job_id>` | `text/event-stream` of `{event: start\|log\|done}` frames | Unknown job → 404 JSON; `GET /actions/jobs` lists jobs | P1 |

### States to verify
- Two maintenance jobs launched back-to-back → second gets 409, first's stream unaffected.
- Server restart mid-job → EventSource `onerror` path sets "Stream interrupted." and re-enables buttons.
- Fragment re-injected into drawer twice → scripts re-execute without duplicate listeners breaking submit.

---
Key files: `src/pipeline/provenance_panel.py`, `command_center_shell.py` (▦/dot/drawer/redirects), `dashboard_html.py`, per-section panels under `src/pipeline/`, routes in `execution/comments_server.py` (`/api/panel/<name>`, `/actions/{run-eval,resolve-issue,maintenance,refresh-ir,rebuild-dcfs,stream/<job_id>,jobs}`, `/api/{llm-budgets,ticker-settings/<T>,dcf-globals,cron-health,metrics/panel}`).

---

# Part 7 — Per-ticker workspace report (build artifact)

## Document shell & page chrome (identity, strips, tabs)

**Reach:** built artifact opened via `file://` (e.g. `data/holdings/<T>/…html`) or served at `http://localhost:7421/reports/<TICKER>`; deep link `#tab=<section-or-group-id>`. **Preconditions:** build artifact exists (renderer `workspace_html.render`); no server needed to render.
**Renders (top→bottom):** two inline JSON boot blocks (`#workspace-boot`, `#workspace-comments`); `.l1-identity` — large ticker, company name, verdict badge (`Thesis Intact`/`Watch`/`Broken`/`Pending` dot + `as of MM-DD`; dot renders **grey** when `verdict_as_of` predates the newest quarter's calendar end, title `Evaluated YYYY-MM-DD — predates the {Q} print`), meta `USD · Report dated …` (+ `DCF dated …`); right valuation strip: Last price · DCF/share · Implied % (pos/neg tone) · MoS bar · Trigger. Then forgone strip (only when budget skips): `⏭ N analyses forgone to stay under budget: <names>. Raise the cap or override, then rebuild.` Thesis strip (label `Thesis`, one-liner, commentable anchor `thesis_lede`). Reread strip (label `5-min reread`, first substantive line of the cached `five_min_reread` lens, link `Full reread →`). `Open watch-items` `<details open data-persist="open-items">` with sub `{n} open · from the analyst journal`, rows kind·body·date. KPI strip: up to 4 tier-1 tiles (name, latest, delta, sparkline, `Nq trailing` axis) padded to a 4-up grid. Tab bar with per-group count badges; portfolio order Overview / Quarter / Financials / Research / Position / Sources (eval flavor: Research first, Company first within it; Position group only when held; standalone Decisions tab when exited-with-history). Footer: `Research package · T · date` / `renderer · workspace · v0.1`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Top tab button | click `.tab[data-tab]` | swaps `.tab-group-pane`; pins re-rendered (bootAll on tab click) | first group pre-active; no persistence across reload | P0 |
| Subtab pill | click `.subtab[data-subtab]` | swaps `.subtab-pane` inside the group | only rendered when group has >1 section | P0 |
| Deep link | `#tab=earnings` (load or hashchange) | activates owning group + pill via `activateSection` | unknown id = silent no-op | P1 |
| Verdict badge hover | `title` | evaluated date (+ stale note) | no `verdict_as_of` → no as-of span/title | P1 |
| KPI tile | click (only when `kpi_definition_id` resolves → `<button class="fact-doorway" data-fact-ref="kpi:T:id">`) | opens chat sidebar, auto-submits `label — kpi:T:id` | no def id → inert `<div>`, never a dead button; server down → chat error turn | P0 |
| Open-items toggle | click summary | open/closed persisted to `localStorage ws:det:open-items` | storage blocked (some file:// sandboxes) → silent no-op | P2 |
| Reread doorway | click `Full reread →` (`data-xtab="synthesis"`) | switches to Research▸Synthesis, scrolls top | hidden when no cached lens/line | P1 |

### States to verify
- No thesis text → thesis strip absent; no forgone → strip absent; <4 KPI tiles → empty `aria-hidden` pads; no synthesis lens → reread strip absent (hide-don't-stub).
- Stale verdict never renders green; `pending`/unknown verdict → muted `Pending`.

## Overview ▸ Thesis subtab (incl. in-app DCF editor)

**Reach:** Overview group (default active on portfolio flavor), first pill; `#tab=thesis`. **Preconditions:** none to render; DCF editor needs :7421 + redesigned workbook.
**Renders:** eyebrow `Thesis · Valuation · Break conditions [· updated …]`; optional `Stub:` warning; thesis lede; 2-col grid: **Valuation summary** panel (model label sub, as-of; bear·base·bull `scenario-range` cells with upside % + price track when scenario values exist, else `Consolidated NPV / share` row; Current price; MoS; Trigger; `Priced in vs your case` levers; scenario prior `bull/base/bear` weights + `E[V]`; assumptions-sync row — `sync FAILED … workbook edits not mirrored` in red on failure; `Open in Google Sheets ↗` OR `Open .xlsx` → `/dcf/<T>`) + **Universal break rules** panel (rule/latest/threshold/status OK|WARN|BREACH|UNRESOLVED + shaded detail rows; empty state `No break-rule evaluations on file for this name yet.`), then **Soft signals** (YELLOW-only). DCF editor `<section id="dcf-edit">`. `Recent decisions` badges (last 3, tooltip rationale). `Macro factor sensitivity` (β/R²/lookback). Hygiene cards (Break conditions / Qualitative breakers / Competitive watchlist). **KPI ledger detail** `<details open data-persist="kpi-ledger">` with `valuation →` xlink, living-grid filter, rows = doorway name button + definition gloss, latest (+`stale` flag >~2q old), sparkline+delta, status, break condition, source; `Tracked, no data yet (N)` footnote.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `Edit assumptions & re-run ↻` | click toggle | expands body, `GET /api/dcf/inputs/<T>`, builds controls, first recompute | 404 → `No editable DCF model for this ticker.`; offline → `Research server offline — start comments_server to edit.` | P0 |
| Any assumption input / terminal-method select / segment growth cell | input (debounce 280ms) | `POST /api/dcf/recompute` → scenario cells + WACC×multiple heatmap + status `Base $X · over/under by N% vs price · WACC …` | offline → `Research server offline — start comments_server to recompute.`; CAPM driver edit re-derives WACC field | P0 |
| `Reset` | click | restores last-loaded inputs, recomputes | no-op before load | P1 |
| `Save to model` | click | `POST /api/dcf/save`; status `Saved to model ✓ · override ledger updated (Opus baseline untouched).`; adopts canonical inputs | button disabled during save; failure → `save failed (status)` | P0 |
| `→ DCF` inject button (KPI ledger latest cell) | click `[data-dcf-inject]` | opens editor, scrolls to it, sets input (flash outline), recomputes; status `Injected <label> — recomputing…` | model not loaded → queued `pendingInject`, applied post-load; unmapped/odd-unit KPI renders no button | P1 |
| KPI ledger row name | click `.fact-doorway` | chat opens on exact series (fact_ref) | no def id → plain bold text | P0 |
| KPI ledger pin / row comment | hover row → `+` pin | comments sidebar on `kpi_ledger_row` anchor | — | P1 |
| Ledger filter | `/` or click `.lg-filter` | Alpine live filter (works file://) | — | P2 |

### States to verify
- Cold ticker: valuation panel with dashes, break-rules empty panel, no macro panel, no decisions panel — no 500, no stub stacking.
- Scenario range with null bull/bear → em-dash cell; track only when both ends + price.

## Overview ▸ Valuation subtab

**Reach:** Overview group ▸ `Valuation` pill; `#tab=valuation`. **Preconditions:** `valuation_basis` computed.
**Renders:** eyebrow `Valuation · Opus-picked multiple · 12Q context`; headline panel titled with multiple name (as-of period end): big current value, Range min–max / Median band, optional PEG block (value + `PEG (NTM)` + `X ÷ Y% fwd EPS growth`), rich/cheap verdict text, 12Q sparkline + axis dates; `Why this multiple` prose panel (commentable, xlink `thesis KPI drivers →` → `#panel-kpi-ledger`); `Target read` notes panel.

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| `thesis KPI drivers →` | click xlink | Thesis pane, scrolls+flashes KPI ledger panel (`.xlink-flash` 1.6s) | — | P1 |
| Rationale pin | hover panel | comment on `valuation_rationale` | — | P2 |

### States to verify
- `vb is None` → collapsed empty panel `No valuation multiple has been worked up for this name yet.`; non-OK status → `_missing_panel` analyst copy (never CLI fix commands).

## Quarter ▸ Earnings subtab

**Reach:** Quarter group first pill; `#tab=earnings`. **Renders:** eyebrow `Earnings calls`; quarter selector (`Quarter` label + `qbtn` short-label buttons, group `earnings`); `Analyst beat-rate scorecard` (EPS/Revenue rows, sides with zero data skipped); `Cross-quarter themes` panel (xlink `bear case ↔` → `#panel-failure-modes`; prepared vs Q&A theme lists with per-quarter mention chips + evidence quotes); then one per-quarter block visible at a time: `{Q} — financial highlights` (metric/quarter/QoQ/YoY, per-cell source chips + panel-head chip), `{Q} — prepared remarks & key takeaways` (sub `full|digest` + `transcript ↗` file:// link), `Analyst Q&A` (`<details class="qa-row">`, first open; tag/topic/analysts summary; Q/A/Follow-up; `No response captured.`).

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Quarter button | click `[data-quarter]` | swaps this group's card AND broadcasts the same quarter to `saydo` + `ir` groups when they have that button | group lacking the quarter = silent no-op | P0 |
| Source chip | click chip `<details>` | popover: tier, fetched-at, confidence, open-source link; Escape-only dismissal (SOURCE_CHIP_JS) | — | P1 |
| Q&A row | click summary | native details expand, CSS chevron | — | P2 |
| `transcript ↗` | click | opens raw transcript file:// in new tab | path missing → no link | P2 |

### States to verify
- No cards → `_missing_panel` "Earnings calls"; quarter without parsed Q&A → empty panel reason `{Q} {Y} call · not parsed`, copy `No parsed transcript on file for this quarter…`.

## Quarter ▸ Say · Do subtab

**Reach:** Quarter group ▸ `Say · Do`; `#tab=saydo`. **Renders:** eyebrow `Say · Do · Track`; verdict trajectory bar + `N quarters tracked / most recent →`; quarter selector (group `saydo`, synced with earnings); `SayDo summary — all tracked pairs` (≥2 cards); `Persistent guidance outcomes ledger`; per-quarter block: `{prior} → {current}` title, rating chip (EXCEEDED ok / MET neutral / MIXED·REVISED UP warn / MISSED bad), `Print vs guide` table (sub `X of Y commitments · LLM-judged for thesis relevance` when filtered), `Thesis view` + `Attribution` 2-col, `Full Say·Do narrative`; global `Say·Do verdict ledger` living-grid (`N commitments · M graded`, BEAT/HIT/MISS/NO DATA pills).

### Actions
| Quarter button | click | swaps saydo card + broadcasts to earnings/ir | — | P0 |
|---|---|---|---|---|
| Ledger sort/filter headers | click `lg-sortable` th / type in filter | Alpine sort with aria-sort + arrow | — | P2 |

### States to verify
- No cards → `_missing_panel` but verdict ledger still renders when rows exist; no verdicts → panel hidden.

## Quarter ▸ News subtab

**Reach:** Quarter group ▸ `News`; `#tab=news`. **Renders:** eyebrow `News & market context [· cached Xh ago]`; title `N items · last D days`; per source-section panels (`Material events`, etc.) of tone-coded tiles: tag, mono date, source, headline (external `<a target=_blank>` when URL), gloss; each tile commentable (`news_item` anchor keyed on first 80 chars of headline).

### Actions
| Headline link | click | opens source in new tab | no URL → plain text | P1 |
|---|---|---|---|---|
| Tile pin | hover → `+` | comments sidebar on the tile | — | P2 |

### States to verify
- Parser miss → `_missing_panel` + `News brief / full text` raw markdown fallback; empty section → missing panel only.

## Financials tab

**Reach:** top tab `Financials`; `#tab=financials`. **Renders:** eyebrow `Financials · Nq · CUR millions`; `§3.5 Signals` (red/yellow fire cards first, `All signals (N)` collapsible living-grid, xlink `related news →`); `Validation: segments ↔ consolidated revenue` (chip `ties to FMP (max drift x%)` / `minor drift` / `DRIFT — segments off by up to x%` + per-quarter drift table; misaligned labels → empty panel `cannot tie · labels misaligned`); YoY% heatmaps: line items, revenue by segment (definitions hover 📖), geography / segment OI / capex / headcount buckets; `Tracked KPIs` + `Tracked KPIs — annual` (fiscal-year axis) with per-cell source hovers + row-label chips; junction cross-tab panels; `Line items · last 12 quarters` levels table (`…· click ▶ to drill`).

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Revenue drill row | click `.fin-row.drillable` | toggles hidden `.fin-drill` segment table (Σ row), chevron ▶↔▼ | only revenue + aligned labels drillable | P1 |
| `All signals` toggle | click summary | table expands (not persisted — no data-persist) | — | P2 |
| Heatmap cell hover | `title` | tier + fetched-at + confidence | unsourced cell → none | P2 |

### States to verify
- status≠OK & no line items → `_missing_panel` and tab stops; empty buckets → panels absent; no signals → panel absent.

## Research group (Bear / Company / Exec Comp / Synthesis)

**Reach:** `Research` tab; pills `Bear case`, `Company`, `Exec Comp`, `Synthesis` (eval flavor: Company first, and the whole group leads the tab bar); `#tab=bear|company|exec_comp|synthesis`. **Renders — Bear:** `Most underweighted by consensus` prose + `Out-of-scope flags` list; `Failure modes` panel (`#panel-failure-modes`, xlink `earnings themes ↔`), numbered cards (hypothesis/Evidence/Leading/Quant impact/Refutation), each commentable; both-empty → `No bear case has been written on this build…` (`not generated`). **Company:** eyebrow `What this company does [· FYxxxx 10-K · cached …]`; sector title, industry lede, elevator pitch block; eval flavor: `Numbers at a glance` (FY-2→TTM + 3y CAGR) + peer-comp panel (all flavors, hides when nothing scores); `Business overview` (commentable) + `Revenue mechanics` 2-col; segment/geographic breakdown; strategic targets / customer concentration / lease ladder (industry-suppressed as applicable); `IR documents` panel with its own quarter selector (group `ir`, broadcast-synced) — cards with doc type, `source ↗`, summary; `10-K Narrative Intelligence` filing review. **Exec Comp:** `Alignment read` prose, anomaly flags, NEO comp table, insider transactions. **Synthesis:** `Synthesis · N lens artifacts`; first lens open, rest `<details>`; headers carry `DIRTY`/`STALE` pills + age + model; inline `[n]` citation chips when lens carries citations.

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| `earnings themes ↔` / `bear case ↔` xlinks | click | cross-tab jump + flash anchor panel | — | P1 |
| IR quarter buttons | click | swap IR card(s); syncs earnings/saydo quarter | quarter with 2 cards shows both | P1 |
| Lens toggles | click summary | expand (no persist key) | — | P2 |

### States to verify
- Synthesis empty → `No analytical lenses are cached for this name yet…` (`none cached`); exec comp missing → `_missing_panel`; suppressed sections (e.g. bank lease ladder) absent entirely.

## Position group (Position / Decisions)

**Reach:** `Position` tab (held names only); pills `Position`, `Decisions`; `#tab=position|decisions`. Exited names: standalone `Decisions` tab. **Renders — Position:** title `N shares across M account(s).`; **coaching block**: `Guard: never run on this name · 0 position reviews` (0-review honest-zero) or `Guard: consulted during N position reviews`, optional graded-sells base-rate line, button `Review this position →` → `http://localhost:7421/socratic/<T>`; 4 stat tiles (Cost basis / Market value / Unrealized P&L / Return, pos/neg tone); `Accounts` living-grid (as-of in header, cost-source column); `Recent transactions` grid; `Open decisions` / `Closed decisions` cards (date, action, confidence, outcome chip, `brief ↗` file:// link, thesis, `Closed <date>.` + notes). **Decisions:** `N decisions tracked`; `Summary` chips (kind + conviction counts, sub `X% win rate on graded decisions` or `no graded outcomes yet`); `Conviction × outcome` cross-tab (TRIM/SELL inverted win logic); `Decision ledger` grid, newest first.

### Actions
| `Review this position →` | click | opens Socratic page in new tab | server down → dead link (absolute :7421) | P0 |
|---|---|---|---|---|
| `brief ↗` | click | opens linked brief file:// | — | P2 |
| Grid filters/sorts | type/click | client-side, offline-safe | — | P2 |

### States to verify
- Not held → collapsed `Position` empty panel `The portfolio tracker shows no position in this name.` (`not held`); 0 decisions → header line only, no panel.

## Sources tab

**Reach:** `Sources` top tab; `#tab=sources`. **Renders:** title `N quarters of coverage · M source documents · K transcripts inline.`; button `Open the live data-quality console →` → `:7421/#provenance`; `Earnings-call transcripts` (`click to expand` details, `{Q} {Y} — n chars`); `Open validation issues` (chip `N open`, severity ticks halt/warn); `Coverage matrix` (Alpine-sortable Audio/Transcript/Release/Slides/SayDo/Summary ●/○ cells); `Source documents` grid — path cell links `:7421/source/<doc_id>` (viewer 302s to origin URL when not in-app-viewable; old builds without doc_id → plain text); `Prompt quality` panel (last 30 days, per prompt-version stats + 30d sparkline; hidden when no graded rows).

### Actions
| Console / source links | click | new tab into research server | server down → dead absolute links | P1 |
|---|---|---|---|---|
| Transcript details | click summary | inline full text | — | P2 |
| Matrix column sort | click th / Enter | Alpine numeric sort, aria-sort updates | — | P2 |

### States to verify
- Empty DB → header shows zeros, panels absent, no 500; missing prompt-calibration table degrades silently.

## Chat drawer (push-sidebar)

**Reach:** floating pill `⌘ Chat` bottom-right; any `.fact-doorway` click; × / Escape to close. **Preconditions:** comments_server on :7421 for thread + streaming; boot data present.
**Renders:** `.chat-sidebar` (460px, slides document via `--sidebar-open-width`): header `Ask Claude about {T}` + sub `{date} · streams from comments_server · think it through →` (→ `:7421/socratic/<T>`), × close; thread; textarea placeholder `Ask about a KPI, propose an edit, look up a quote in the transcript…`; hint `Cmd+Enter to send`; `Send`.

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Open | click toggle / doorway | CCOverlay `report-sidebar` group opens (closes comments sidebar); focus textarea; `GET /chat/<T>?report_date=` replays thread | fetch fail → system turn `The research server is not reachable, so chat is offline.`; iframed in shell → launcher hidden, sidebar still doorway-openable | P0 |
| Send | submit / Cmd(Ctrl)+Enter | POST SSE: hint cycles `Working…` → `Compiling the view…`/`Running the view…`/`Researching — can take ~30s…`; deltas stream; `fragment` events render data-view HTML + set follow-up `context_spec`; final markdown render + citation chips `[n] label` (hrefs absolutized to :7421) + `⚠ N unverified` chip | error → `[ERROR] …` in turn + offline hint | P0 |
| Fact doorway | click anywhere in doc | opens chat, autofills `label — fact_ref`, auto-submits | ref missing on host → no-op | P0 |
| Diff proposal | `Preview` / `Apply` buttons | `POST /chat/<T>/apply` (dry_run for preview); note `✓ Applied: …` / `↗ Preview: …` / `✗ …`; applied → green wrap | server down → fetch rejects silently (no note) | P1 |
| Close | × / Escape / open comments sidebar | closes via CCOverlay (one-Escape stack; floater/help claim Escape first) | CCOverlay absent → direct class fallback | P0 |

### States to verify
- file:// with server up: full chat works (absolute URLs). file:// server down: offline system turn, sends error out.

## Comments sidebar + pins + selection floater

**Reach:** hover any `[data-commentable]` → pin (count or `+`); select ≥2 chars of text → floating `+ Comment`; click an existing `mark.cmt-highlight`. **Preconditions:** read-only pins render from inlined JSON with no server; POST/PATCH/notes need :7421.
**Renders:** 380px push-sidebar: header `Comments` + anchor label + health pill (`● Online`/`● Offline`/`○ …`) + `Queued: N` outbox badge; card list (status open/addressed/dismissed, intent, time, resolution, follow-up thread, `Dismiss` / `Mark addressed`); form textarea placeholder `Write a comment… (tip: prefix with /kpi /thesis /q /ask /fix /update /rewrite /peers to skip auto-classify)`; intent select (`Auto-classify`, Drop this KPI, Edit thesis, Curate peers, Ask question, Flag data issue, Rewrite this section) + `Post`; note-kind select (Watch item/Question/Observation/Assumption/Decision) + `Save to journal`; hint line; offline banner `Server offline — your comment will queue locally and sync on recovery.`

### Actions
| Affordance | Trigger | Expected result | Edge | Pri |
|---|---|---|---|---|
| Pin | click | sidebar opens on anchor (captures `data-fact-ref` for rename-proof re-binding); list or `No comments yet on this element.`; draft rehydrated → hint `Draft restored.` | — | P0 |
| `Post` | submit | `POST /comments`; pin count updates; hint `Posted.`; draft cleared | server down → outbox enqueue, hint `Queued — will retry when server is back. (N total)`; retries every 15s + focus/online + health-recovery edge; entries >7d dropped | P0 |
| `Save to journal` | click | `POST /api/notes` with kind/anchor/fact_ref; hint `Saved to journal ✓` | empty text → `Write the note text above first.`; offline → `Server unreachable — journal capture needs the research server.` (no queue) | P0 |
| `Dismiss` / `Mark addressed` | click | `PATCH /comments/<id>`, card + pin tone update (warn→ok) | offline → `Server unreachable — cannot update.` | P1 |
| Selection floater `+ Comment` | mouseup selection (outside sidebars) | mousedown-open on `free_text` anchor (key = first 200 chars, landmark `panel:`/`tab:` + occurrence index) | hidden on scroll/collapse; selection inside chat/comments UI ignored | P1 |
| Highlight mark | click underlined text | reopens that comment's anchor | orphaned text → highlight silently absent | P1 |
| Typing | input | draft autosaved per `cmt-draft:T:date:type:key` | storage disabled → silent | P1 |
| Close | × / Escape (after floater) / open chat | CCOverlay close; anchor cleared; NO outside-click dismiss (deliberate) | — | P0 |

### States to verify
- file:// server down: pins + highlights render from boot JSON; health pill goes `● Offline` within ~10s; banner shows before submit.
- Double-submit / concurrent flush guarded by `outboxFlushing`.

## Keyboard map & help overlay

**Reach:** global keydown (suppressed inside inputs/selects/contenteditable and with modifiers). **Renders:** `?` builds a dialog card `Keyboard shortcuts`: `j / k — next / previous panel`, `/ — focus the table filter`, `? — toggle this help`, hint `click or ? to close`.

### Actions
| Key | Expected | Edge | Pri |
|---|---|---|---|
| `j` / `k` | smooth-scroll next/previous visible `.panel` | none visible → no-op | P1 |
| `/` | focus+select first visible living-grid filter | no filter on pane → key falls through | P1 |
| `?` | toggle help overlay; click also closes | Escape does NOT close help (by design — CCOverlay owns Escape) | P2 |
| `Escape` | one CCOverlay stack: floater → open sidebar; source-chip popovers Escape-only | — | P0 |
| `Cmd/Ctrl+Enter` | submit chat message (chat textarea only) | — | P1 |

## file:// vs served :7421 degradation & localStorage matrix

**Reach:** same artifact both ways. **Preconditions:** server = `python execution/comments_server.py` (hardcoded `http://localhost:7421` boot URL).
**Works fully offline/file://:** all rendering, tabs/subtabs/deep links, quarter broadcast, drill-downs, living-grid sort/filter (Alpine inlined), keyboard map, comment pins + free-text highlights (read-only), details persistence, Google Sheets link, transcript/brief file:// links. **Dies without the server:** chat thread + streaming (offline system turn), comment POST (queues), PATCH + Save-to-journal (error hints), DCF editor load/recompute/save, `/dcf/<T>` xlsx link, `/source/<doc_id>` viewers, data-quality console, Socratic links, cite-chip hrefs. Google Fonts is the sole other external fetch (degrades to system fonts offline).

### States to verify
- localStorage keys: `ws:det:<key>` (open-items, kpi-ledger), `cmt-draft:<T>:<date>:<type>:<key>`, `cmt-outbox` (7-day expiry, badge count) — survive reload; quarter selection and active tab intentionally do NOT persist.
- Health pill polls `/healthz` every 10s (5s abort); offline→online edge drains outbox immediately.
- No JS console errors on file:// load with server down (all fetches caught).

---

# Part 8 — Telegram bot (capture, coaching, callbacks)

## Telegram inbound capture (voice / text / URL / document)

**Reach:** Telegram DM to the bot; consumed by the long-poll poller (`execution/capture_poller.py` → `src/capture/poller.py:poll_once`), a Windows scheduled task (`cron/capture_poller.task.xml`) — never a Flask thread; no server ports. **Preconditions:** bot token at `data/secrets/telegram_bot_token` (absent → poller exits cleanly "not configured"); DB `data/portfolio.db`; offset cursor `data/capture/telegram_offset.json`; voice needs faster-whisper installed + ffmpeg at `C:/ffmpeg/bin`. **Poller restart required for ANY `src/` code change** — the task loads modules at start (PR #815 lesson); every inbound behavior below lives in the poller process.

**Renders (top→bottom):** for each message the bot replies with exactly one confirm string from `_CONFIRM` (plain ASCII, no emoji): landed text/voice → `Captured.` or `Captured. ({TICKER})` when the roster matcher resolved one; ambiguous (multi-candidate, `context.needs_ticker=true`) → **fixed by #825 (2026-07-04)**: `Captured. Which ticker?` with a one-tap candidate keyboard (`st:<ticker>:<note_id>`, the same write-once `set_ticker` action the Ledger chips call); keyboard-build failure falls back to `Captured. (Which ticker? Tap a candidate in the Ledger.)` — QA note: the pre-#825 behavior was a silent bare `Captured.`, so verify the keyboard actually arrives on a live ambiguous capture; URL/document → `Saved to On My Mind.`; voice failure → `Couldn't transcribe that one - I kept the audio, try again?` / `That voice note came through empty - try again?`. Duplicate (`tg:<update_id>` dedup) or empty gets **silence**. When `LEDGER_ONMYMIND` feed flag is on, a landed capture's confirm carries the 4-button On My Mind ladder keyboard.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Plain text musing | send any non-slash, non-bare-URL text | staging session → PII scrub → deterministic roster ticker match → `analyst_notes` row kind=musing source=capture; reply `Captured.` / `Captured. ({T})`; then fire-and-forget taps: wondering detect, pledge/annotation, artifact brief | ambiguous ticker → `needs_ticker` context + candidates list, copy above; re-sent update_id → duplicate, silent; DB write failure logged, loop survives | P0 |
| $TICKER mention | text containing symbol/name/alias in roster | same as text; `Captured. ({T})` confirm names the resolved ticker | misheard/unknown name → NULL ticker or needs_ticker — never a wrong attribution | P0 |
| Voice memo | send Telegram voice note | `.oga` downloaded to `data/capture/audio/tg_<id>.oga` → faster-whisper (`base.en`, CPU int8; `CAPTURE_WHISPER_MODEL` overrides) → same land path as text; audio purged on land | getFile/download failure → counted `download_failed`, no reply, retried never (offset advances); TranscriptionError (missing lib, decode fail, empty text) → audio RETAINED, session mark_failed, transcription-failed copy; voice with no file → `no_audio` copy | P0 |
| Bare URL | text = single `http(s)://…` token, no whitespace (`ingest._extract_url`) | lands as On My Mind reading (kind=observation, ticker=NULL, `ledger:"onmymind"`, `item_type:"link"`); reply `Saved to On My Mind.` (+ladder if flag on) | URL + trailing words = NOT a reading — falls to musing path; duplicate silent | P0 |
| Document (PDF/deck) | send file attachment (optional caption) | file saved to `data/capture/docs/tg_<id>_<name>`; reading row body `name` or `name: caption` (caption PII-scrubbed); `Saved to On My Mind.` | download failure → `download_failed`, silent skip; caption-only scrubbing, filename kept verbatim | P1 |
| Confirm send itself | any land | best-effort; `TelegramError` suppressed | failed confirm never blocks capture | P1 |

### States to verify
- Duplicate redelivery (same update_id) is silent and writes nothing.
- Transcription failure retains `.oga` on disk; retry never automatic (offset advanced).
- Poller survives a single-batch Telegram/network error (5s backoff, `capture_poll_telegram_error` log) — no crash loop.
- Empty text message → status `empty`, no reply, session mark_failed.

## Slash commands (/start, /review, unknown)

**Reach:** same DM; intercepted in `poll_once` before capture — commands are chrome, NEVER captured as musings. **Preconditions:** poller running; `/review` needs `data/portfolio.db` reachable via repo_root derived as `db_path.parent.parent`.

**Renders:** one plain-text reply per command (no keyboard, no Markdown — `plain=True`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `/start` | send `/start` | reply exactly: `Ledger capture is live - send a voice memo or a thought and it lands in your Ledger.` | none | P1 |
| `/review <NAME\|TICKER> [at $X]` | e.g. `/review Rubrik at $70`, `/review NU`, `/review Novo Nordisk` | instant LLM-free pre-analysis via `advisor.position_review.review_reply_text(plain=True)` — same parser as Ask tab; name resolves through roster+alias seed (`Rubrik`→RBRK, `Nubank at $12`→(NU,12.0)); multi-word names survive, `at $X` clause stripped by `_AT_PRICE_RX` | bare `/review` → `Usage: /review <NAME|TICKER> [at $PRICE] - e.g. /review Rubrik at $70.`; build exception → `Couldn't build a review for {T}: {Type}: {msg}`; no db_path → `Couldn't build that review (no database configured).`; outer failure → `Couldn't build a review for {TICKER}.` | P0 |
| Unknown slash (`/foo`) | any other `/`-prefixed text | reply exactly: `Not a command here. /review <TICKER> works; everything else lives in the web chat (Ask tab).` | never captured; counted `command` | P1 |

### States to verify
- `/review rubrik` (lowercase name) resolves; `/review UNKNOWNTICKER` degrades to a review-for-symbol or error copy, not a 500/crash.
- Case: `/REVIEW nu` matches (`low.startswith("/review")`).

## Pledge challenge + annotation follow-up (entry coaching, W2)

**Reach:** fires automatically inside the text/voice land path (`poller._pledge_and_annotate` → `src/research/pledge.py`). **Preconditions:** landed musing; LLM available for `musing_decision_extract` (governed #718); entire tap fire-and-forget — any exception silently returns.

**Renders:** on a pledge match, a second bot message after `Captured.`: header `Pledge recorded (#{id}): {DIRECTION} {TICKER}.` + blank line + the 3-line catalyst test (`The catalyst test (your rule — it must pass BEFORE you order):` / `1. What is the near-term catalyst, in one sentence?` / `2. Is it already priced in — what does the market believe?` / `3. Washout or value trap: what distinguishes this one?`) + optional `Current read:` block (LLM-free position-review pre-analysis: weight, valuation gap, tripped rules; degrades to nothing) + optional ask: `Reply with your {conviction and falsifier} to complete the record — e.g. "high conviction, falsifier: 15-90d NPL >5% for 2Q".` when fields are missing.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Pledge-shaped musing | regex `_PLEDGE_RE` matches: `buying`, `selling`, `trimming`, `adding to`, `about to buy/sell/add/trim`, `i'm buying / i am selling…`, `pledge:` / `pledge ` | LLM extract {ticker,direction,size_pct,conviction,falsifier}; `~$30k` idiom parses via `_SIZE_RE` (`[~≈]?$N[k]`) to size_usd; owner decision persisted with `user_notes` marker `pledge:note:<id>`; challenge reply sent | idioms that do NOT match: `should I buy NU?` (wondering), `bought last year` (past tense `bought` not in regex), `buy NU` bare imperative, `sold`, `I might add` — retro net covers misses; LLM returns direction∉{buy,sell,add,trim} or no ticker → audit `pledge:unparsed`, no reply; below capture threshold → `pledge:below_threshold`; any exception → `pledge:errored`, silent | P0 |
| Same note re-processed | duplicate | `WHERE user_notes LIKE '%pledge:note:<id>%'` guard → None, no duplicate decision | — | P1 |
| Annotation follow-up | later message matching `_ANNOTATION_RE` (`conviction`, `falsifier`, `high`, `medium`, `low`) and a pending owner-decision stub (NULL conviction OR falsifier, pledge- or retro-net-marked, ≤48h old) | fills ONLY the NULL fields (write-once), appends `· annotated <iso>`; reply exactly `Noted — recorded on decision #{N}.` | no pending stub in window / nothing extractable / both fields already set → silent (no reply); message also landed as a normal musing first (`Captured.` precedes it) | P0 |

### States to verify
- Word `high` alone in an unrelated musing triggers the annotation extract path — verify silence (no false `Noted —` when nothing fills).
- Pledge reply never blocks landing: kill LLM budget → musing still lands with `Captured.`.

## Wondering card (research tap) + rt:/rp: callbacks

**Reach:** land path tap `poller._tap` → `research.proposals.detect_and_create_task`; card via `research_notify.notify_new_task`. **Preconditions:** `LEDGER_RESEARCH_TAP` ON by default (`=0` disables); run button additionally needs `LEDGER_RESEARCH_RUN=1` (default OFF — current prod state per memory).

**Renders:** message `Caught a wondering: {claim}\nResearch it?` with one button `Research it` (`rt:run:<task_id>`) when run flag on; flag off → `Caught a wondering: {claim}\n(Enable research to dig in.)` with NO buttons. After a run, a proposal card: `{TICKER} - {title}` (or title alone) + blank line + 600-char body excerpt (`...` suffix), with 4 stacked buttons `Approve` / `Research further` / `Steer` / `Reject` (`rp:<verb>:<proposal_id>`).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `Research it` | press `rt:run:<id>` | toast (answerCallbackQuery) `Researching...`; two-pass engine runs; proposal card pushed; original card edited in place: keyboard stripped + line `- researched MM-DD` appended | run flag off → toast `Research is off.`, no stamp; engine exception → toast `Research failed; try again.`, task reverted, card NOT stamped (re-pressable); double-press after stamp → keyboard gone, nothing to press | P0 |
| `Approve` | `rp:approve:<id>` | `act_on_proposal` + `apply_approved_proposal` write; toast `Approve: {status}. {applied}`; card stamped `- {status} MM-DD`, keyboard stripped | apply write failure swallowed (toast without suffix); stale proposal → status string reflects no-op | P0 |
| `Research further` / `Steer` / `Reject` | `rp:further/steer/reject:<id>` | toast `{Verb}: {status}.`; stamp + keyboard strip; steer marks steered (typed direction lives in web inbox) | re-press of already-handled id → status indicates stale; malformed data → toast `Unrecognized action.` | P1 |

### States to verify
- Callback dispatch wrapped in `suppress(TelegramError)` in the poller — a failed answer/edit never kills the loop.
- Stamp is best-effort: chat_id/message_id missing → action still succeeds, card unedited.
- `_stamp_card` uses the original `callback_query.message.text` — verify body preserved verbatim + one appended line.

## On My Mind ladder + engage brief (om: callbacks)

**Reach:** ladder keyboard attaches to (a) landed-capture confirms when `onmymind_enabled()` flag on, (b) every engage-brief push. Brief tap: `poller._artifact_brief` → `research.brief.generate_brief_for_note`. **Preconditions:** brief gated by `LEDGER_ARTIFACT_BRIEF` (default ON, `=0` kills); needs the intent tap to have stamped `context['engage_intent']` on the note, a resolvable artifact (Phase-2 extract, cache `data/artifact_cache/`), and a working LLM (spend!).

**Renders (brief):** header `Brief of what you saved:` (or `Stress-test of what you saved:` for stress mode); up to 5 `- takeaway` lines; `Bull: …` (500-char excerpt) / `Bear: …`; stress adds `What would change your mind: …`, `Second-order: …`, `Your book: …` (400-char excerpts). Keyboard rows (one button per row): `Incorporate` / `Discuss` / `Save for later` / `Dismiss` → `om:<verb>:<note_id>`.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `Incorporate` | `om:incorporate:<id>` | same action core as web (`act_on_feed_item`); toast `Sent to research.`; stamp `- incorporate MM-DD`, keyboard stripped | note gone → toast `Not found.`, no stamp | P0 |
| `Discuss` | `om:discuss:<id>` | toast `Let's dig into it — continue in the web thread.`; stamp `- discuss MM-DD` | — | P1 |
| `Save for later` | `om:save:<id>` | toast `Saved for later.`; stamp | — | P1 |
| `Dismiss` | `om:dismiss:<id>` | note archived; toast `Dismissed.`; stamp | already gone → `Not found.` | P0 |
| Auto-brief fire | send URL-bearing musing intent-tapped as brief/stress | second message with brief + ladder | any fetch/LLM/parse failure → NOTHING sent (capture unaffected); no takeaways → degrade to None; `LEDGER_ARTIFACT_BRIEF=0` → tap dead | P0 |

### States to verify
- `om:worldview` is a valid `LADDER_VERBS` member but has NO Telegram button — only reachable via crafted callback; toast `Staged as a candidate Tenet.` / `Nothing to stage.`.
- Ladder absent when `LEDGER_ONMYMIND` off (plain confirm — behavior unchanged).

## Coach pings (governed initiation) + cp: callbacks

**Reach:** `execution/run_coach_pings.py` (daily scheduled run; fresh process each run so it picks up code WITHOUT poller restart — but its buttons' callbacks are handled by the poller, which DOES need restart). **Preconditions:** token file + persisted owner chat id at `data/capture/telegram_chat_id.json` (saved by the poller on any inbound message — a never-messaged bot cannot ping); `coach_pings`/`coach_mutes` tables; `--dry-run` collects+gates without sending.

**Renders:** at most `DAILY_CAP=1` send/day, `WEEKLY_CAP=3`/week; three exact bodies: **falsifier_breach** `Your falsifier on {T} tripped. You wrote: "{falsifier≤200}". Keep the verdict with the framework, not the feeling — what's the call? (/review {T})` with buttons `Answer: review {T}` (`cp:review:<ping_id>`) + `Dismiss` (`cp:dismiss:<ping_id>`) on one row; **retro_annotation** `Decision #{N} — {KIND} {T} (~$30,000) — is still missing your conviction + falsifier. One line back completes the record (it's write-once after that).` Dismiss-only; **intent_followup** `Standing intent still open: "{body≤140}" — still live? Reply here with a close reason, or resolve it from the Ledger tab.` Dismiss-only (re-asked at most once per 14-day bucket).

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| `Dismiss` | `cp:dismiss:<id>` | ping → status dismissed; toast `Dismissed.`; stamp `- dismissed MM-DD` + keyboard strip | 3rd consecutive dismissal of a class → toast `Dismissed — and {class name} pings are now muted.` (underscores → spaces) and class muted until `unmute()`; re-press/already-handled → toast `Already handled.`, no stamp (`cp_stale`) | P0 |
| `Answer: review {T}` | `cp:review:<id>` on falsifier_breach | toast `Reviewing {T}...`; same plain `/review` reply pushed as a new message; stamp `- reviewed {T} MM-DD` | ping id gone → `Unrecognized action.`; ping without ticker → `No ticker on this ping.`; review build failure → `Couldn't build a review for {T}.` | P0 |
| Digest downgrade | cap exceeded, class muted-check passed but `send_fn` fails/absent | ping row written status `digest` (never pushed; surfaced quietly in web Coach panel via `digest_pings`) | Telegram send exception → sent row downgraded to digest post-hoc; dry-run → all sends digest | P1 |
| Freshness gate | stale subject (falsifier `(inferred)`, ticker no longer portfolio, stub filled, intent closed) | row `skipped_stale`, no push, moment consumed forever (UNIQUE class+key) | can't verify → don't ping | P1 |

### States to verify
- Rerunning the script same day is idempotent (a moment considered exactly once); tally printed to stderr `run_coach_pings: {…}`.
- `auto_reconcile` housekeeping runs first — moot items never reach the queue.
- No chat id persisted → script sends nothing, all `digest`, exit 0 (no crash).
- Dismissed digest rows count toward the 3-dismissal auto-mute (`status IN ('sent','digest')` accepted by `record_dismissal`).

## Callback dispatch generics

**Reach:** any inline-button press → poller `kind == "callback"` → `research_notify.dispatch_callback`. **Preconditions:** poller running (restart to pick up handler changes).
**Renders:** no new message except where noted; feedback is the answerCallbackQuery toast + in-place editMessageText stamp.

### Actions
| Affordance | Trigger | Expected result | Edge/degraded states | Pri |
|---|---|---|---|---|
| Malformed/unknown data | data not `kind:verb:int` or unknown kind/verb | toast `Unrecognized action.`, nothing else | missing callback_query_id → total silence | P1 |
| Stale re-press | pressing a card whose keyboard strip failed | action core re-runs; verbs are idempotent or report stale status; stamp retried | keyboard strip uses empty `inline_keyboard: []` (not absent field) — verify buttons actually vanish | P1 |
| Handled-card stamp | any successful action | original text + `\n- {label} MM-DD`, keyboard removed | edit failure suppressed — action still committed server-side | P1 |

### States to verify
- Every `cp:`/`rp:`/`om:`/`rt:` verb above answered — no press ever leaves Telegram's spinner running (all paths call `answer` when cqid present).
- Stamps are MM-DD naive-UTC, ASCII (`- approved 07-03`), never Markdown.

---

# Appendix A — Known open issues (verify, don't re-file)

**Update 2026-07-04:** all four issue clusters below were FIXED and merged the same day this doc shipped — #826 (counter integrity: window must elapse, candidate-vs-owner-attested split, memo source tags excluding agent runs), #825 (four coach-path gaps: pledge sell-idioms, send-failed breach re-send, in-channel needs_ticker candidate keyboard, status-dot artifact-age check), #824 (badge greys until the print is INGESTED; graded-sells line hedges sparse n via calibration_guard), and the guard-literal fix (phrase now derives from the review count). These landed AFTER the Appendix B mechanical pass ran, so their new behaviors are NOT machine-verified — fold them into your human pass:

1. **Coach P&L counter (#826)**: verify "Decisions changed" renders candidates separately from owner-attested changes, and an agent-sourced memo never counts.
2. **Coach paths (#825)**: pledge with "taking profits on NU ~$2k" is caught; a send-failed breach ping re-attempts next governor run; ambiguous Telegram capture gets the candidate keyboard; a stale `.tmp/daily_chain_status.json` (>26h) turns the System dot warn/bad instead of stale-green.
3. **Honesty fixes (#824)**: verdict badge stays grey between quarter-end and print INGESTION; graded-sells line carries the sparse-n hedge.
4. **Guard line**: Position tab renders "Guard: never run on this name · 0 position reviews" at zero and "Guard: consulted during N position reviews" otherwise — never the old self-contradicting literal.

## Partial-by-design (owner-accepted scope — not bugs)
- The workspace "what changed" strip is **build-diffed, not visit-diffed**: it hoists the cached five_min_reread lens (manually generated via run_lens.py, hidden past a 21-day stale threshold). There is **no server-side last-seen anywhere** — unread accents are per-browser localStorage.
- The ritual's navigational spine = Home open-loops band + Ledger jump chips + alias hashes; there is deliberately no persistent app-wide ritual chrome.
- The Ledger remains the 6th Companies sub-tab (promotion to a primary section is a considered-later decision); while a holding is open the Companies sub-row is suppressed and the holding band's "Ledger" link is the doorway back.
- The workspace Position tab's "Review this position" doorway targets the served socratic page (`/socratic/<T>`) — a stand-in until a dedicated /review HTML route exists.
- Report chrome/doorways only work served from :7421; `file://` opens degrade (Appendix of Part 7 lists exactly what dies).

## Deployment reality check (do this before ANY UI QA)
1. `git -C <MAIN> pull` (MAIN checkout was confirmed stale on 2026-07-04 — it predated the whole program).
2. Restart both NSSM services (es-dashboard, es-poller).
3. Rebuild at least one portfolio-flavor and one evaluation-flavor report (`--enable-llm` optional for chrome QA).
4. Confirm `.tmp/daily_chain_status.json` is fresh (status dot) and the morning pipeline ran.

---

# Appendix B — Mechanical verification results (2026-07-04)

A 6-agent pass executed the mechanical rows against a sandboxed instance (worktree code @ current main, **:7517**, disposable prod-copy DB migrated to head, `LEDGER_RESEARCH_TAP/RUN/ARTIFACT_BRIEF=0`, `ONMYMIND/WORLDVIEW=1`, tracker :8000 down). **115 checks: 110 PASS · 1 FAIL · 2 BLOCKED (env data gaps) · 2 SKIPPED-LLM.** Rows below are machine-verified — skim, don't re-walk them; your pass is Appendix B.2.

## B.1 What is already verified (skip on the human pass)
- **GET renders (46 checks, all PASS):** `GET /` (shell, open-loops band, cockpit columns, inbox rail, topbar/overlay markup), `/feed`, every `/api/panel/<id>` fragment (incl. Ledger with jump chips, Decisions with mirror-first KPI strip + Coach P&L + pings/mutes/digest sections, Risk offline sections, Provenance's section anchors), ETag→304 behavior, holding band links (`Report/DCF/Review/Ledger`), `/api/peek/review/NU` (base-rate line renders), `/alerts`, `/dcf/NU`, `/source/<id>`. Zero tracebacks, zero 500s anywhere, incl. unknown-panel and tracker-down degrades. Note: `/api/panel/overview` 404s **by design** (Overview is server-inlined on `GET /`).
- **Ledger action POSTs (19 PASS):** plain + `$TICKER` capture land correctly; notes lifecycle verbs (resolve/archive/unarchive/reclassify) mutate DB as documented; falsifier ratify returns its honest receipt ("queued for arming" vs "armed" per `arming_status`); Rewrite (action=edit) persists; armed-falsifiers table matches SQL ground truth; set-ticker attribution works.
- **Inbox/alerts/coach POSTs (16 PASS):** alert dismiss ± reason, reason-only round-trip on an already-dismissed alert (no 409), HTMX chip with "why?" affordance, `/api/coach/unmute` clears a mute and the Decisions fragment reflects it, open-loops band counts move coherently after mutations.
- **Report build (17 PASS):** no-LLM `build_artifacts` build succeeds on scratch data; 6 tab groups in order; verdict badge dated; KPI tiles emit fact-doorway buttons when `kpi_definition_id` exists (inert divs otherwise, as designed); reread strip honestly absent with no cached lens; data-xtab/src-chips/comment-pins/selection-floater all present; `GET /reports/NU` serves it. (Artifact filename is `<date>_workspace.html`.)
- **Telegram handlers, function-level (12 PASS):** `/start`, unknown-slash reply, `/review NU` plain-text (no `**`/backticks), `Captured. (NU)` confirm, pledge challenge + `Noted — recorded on decision #N` (extraction seam faked — zero LLM spend), `cp:dismiss` → "Dismissed." + card stamp + keyboard strip, stale re-press → "Already handled.", 3-dismissal auto-mute + notice, `cp:review` → "Reviewing NU..." + plain reply.

## B.2 The human-only pass (what remains for you)
1. **Deployment first** (Appendix A checklist): pull MAIN, restart es-dashboard + es-poller, rebuild reports. *None of the Telegram behaviors above exist on your phone until the poller restarts.*
2. **Browser feel & keyboard (not machine-verifiable):** Ctrl+K palette, Ctrl+. tray open/prefill/focus, Escape priority ladder (palette>peek>drawer>dock), scrim click-out, focus traps, j/k + `/` + `?` in reports, theme toggle look, ≤900px responsive, drawer/peek animations, unread accents.
3. **LLM-invoking flows (deliberately skipped; each costs real calls):** Ask tab / per-ticker chat threads + citations; **/actions/position-review** full SSE review (persists a gradeable memo — do ONE deliberately, it seeds your calibration loop); a real pledge → challenge → annotation round on Telegram (~1 `musing_decision_extract` call); discovery run; artifact brief on a captured URL.
4. **Tracker-UP renders:** with :8000 running — sizing-audit live weights/alpha, Skill decomposition, Risk live vs cached-snapshot banner, tax lines in the review peek.
5. **Real-phone Telegram:** voice memo → transcription; keyboards render/tap; handled-card stamps look right in the thread; a coach ping arriving (or force one via `execution/run_coach_pings.py`) with the **Answer: review <T>** button.
6. **Blocked-in-sandbox rows:** Position-tab coaching block + per-section quarter selectors + selector broadcast — verify in a MAIN-built report for a held name (scratch lacked holdings JSONs / quarter cards).
7. **Visual design conformance:** density, token discipline, dark + paper themes — eyes only.

## B.3 Findings from the mechanical pass
- **FAIL (1) — silent unattributed capture — RESOLVED same-day:** an ambiguous two-name musing landed with bare `Captured.` (no hint, no keyboard, ticker NULL); #825 shipped the in-channel candidate keyboard hours later. Verify the NEW behavior on your pass (Appendix A item 2).
- **Walkthrough corrections applied:** pledge-detect layer is NOT LLM-free (one governed `musing_decision_extract` call per pledge-shaped capture); `/api/panel/overview` 404-by-design; report artifact is `<date>_workspace.html`.
- **Snapshot caveat:** the mechanical pass ran against main BEFORE #824/#825/#826 merged — everything in B.1 still holds (those PRs are narrow), but the four fix behaviors in Appendix A's update block are unverified by machine and belong in your pass.
