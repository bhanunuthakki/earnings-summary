# THE LEDGER — BUILD PLAN

*Implementation directive for the sole owner. Build exactly the signed-off design; every choice below is settled. Tone: concrete, single-user, governed-LLM. Cite the real seams.*

---

## 1. Scope recap

The Ledger turns the owner's stray spoken/typed musings into a durable, theme-organized record of how their investment views evolve — and, in later phases, into a self-driving research and decision-capture loop. It is a personal instrument bolted onto the existing equity-research platform, not a new app.

The settled design, in brief:

- **Capture is voice-first and off-desk.** Two free ingest adapters feed one pipeline: a **Telegram bot** (PRIMARY — voice + text in one long-polled thread, no open ports) and a **Gmail label** (SECONDARY — share-to-email, polled on the existing Google OAuth). A desktop **Ctrl+. tray** stays the at-desk text mouth. Voice is transcribed locally, lightly scrubbed, and landed as an `analyst_notes` musing with a deterministically auto-matched ticker.
- **Synthesis groups musings into THEMES**, read by theme, with a "how my view evolved" timeline — **seeded day one** from existing notes + theses + recent decisions, plus a manual seeding interview.
- **Phase 1 promotes the conversational research loop to core**: a wondering becomes one-tap validation research, budget-tiered by portfolio weight and an actively-considering signal, drafting all five artifact types as **reviewable proposals** (never live writes), with an LLM **adversarial self-assessment** calibrating assert-vs-Socratic tone, surfaced in a 4-action inbox **mirrored into Telegram inline keyboards**.
- **Phase 2** adds quiet behavioral coaching (conviction-drift) and multi-channel, size-thresholded decision capture.

**Deltas from the original directive** (what the grilling changed):

| Delta | Original | Now |
|---|---|---|
| Voice | partly cut ("babysit a sidecar") | **restored** — faster-whisper is already in-tree, no sidecar |
| Research loop | deferred | **promoted to core (Phase 1)** |
| Telegram | one-way capture | **two-way** (inline-keyboard mirror of the 4 inbox actions; free-text reply = steer) |
| MNPI denylist | planned scrub | **dropped** — owner has no MNPI; scrub is phone/email/account regex only |
| Raw audio/transcript | retained | **transient** — purged once the structured musing exists; a durable summary-level **audit log** survives |

House rules are non-negotiable throughout: single-user posture (no multi-tenant drift), the 3 Instrument-Paradigm Laws (identity-over-source; every-datum-a-doorway; dismissible-layers/one-instrument-per-panel), no-build frontend (server f-strings + vanilla JS + HTMX + Alpine), every LLM call through the governed path with the 4-registry lockstep, naive-UTC + `db_paths.resolve_db_path`, code-level RI (no FK `REFERENCES`), reuse seams over new frameworks.

Verified anchors at build time: alembic linear head is **`0114_decision_process_quality`** (the `b79cec08ce5b` saydo branch is non-linear — do not chain off it); `NOTE_KINDS`/`NOTE_SOURCES` are plain TEXT tuples in `src/user_state/notes.py:54,58` with **no DB CHECK** (so extending them is a code edit, not a migration). The capture-text and panel-read routes ride `execution/comments_server.py` (the repo root has no `comments_server.py`) — `panel_fragment` is at line 602, `render_panel_fragment` at line 909.

---

## 2. Phase 0 — capture + seeded synthesis (the buildable slice)

The day-one payoff: speak a musing into Telegram from the couch, and by morning it's a theme-grouped note in the Ledger. The build is **two-waved** so the kill-gate clock starts at *first capture*, not at full synthesis (§2.8, §6): Wave A ships a capture-only, theme-less loop (the owner can speak/type a musing and read a flat list within days); Wave B layers the seed + theme synthesis on top.

### 2.1 The three mouths

**Telegram bot (PRIMARY).** Setup is one `@BotFather` token dropped at `data/secrets/telegram_bot_token` (single line; `data/` and `**/secrets/` are already gitignored — mirrors `data/portfolio_pins.json`). The app **long-polls** Telegram from a standalone process — never a Flask daemon thread:

```
execution/capture_poller.py        # the one long-running loop
src/capture/telegram.py            # getUpdates/getFile/sendMessage + inline keyboards (urllib)
src/capture/token_store.py         # data/secrets/telegram_bot_token loader; raises CaptureSetupError
cron/run_capture_poller.bat        # PROJECT_ROOT cd + python + TS-stamped log
cron/capture_poller.task.xml       # LogonTrigger + RestartOnFailure(PT30M,2) + IgnoreNew
```

The loop:

```
loop:
  resp = GET api.telegram.org/bot<TOKEN>/getUpdates?offset=<next>&timeout=50   # server-side long-poll, no busy-wait, no open port
  for update in resp.result:
      next = update.update_id + 1                       # persist -> data/capture/telegram_offset.json (restart never re-ingests)
      if   update.message.voice:  pull .oga via getFile -> ingest_capture(channel="telegram", media_kind="voice", ...)
      elif update.message.text:   ingest_capture(channel="telegram", media_kind="text", ...)
      elif update.callback_query: dispatch research:<id>:approve|further|steer|reject   # Phase 1 surface, wired now
```

Rationale for a scheduled task over a thread: the repo's whole async philosophy is subprocess isolation (`run_morning_pipeline._run_stage`); a daemon thread in the dev Flask `create_app` dies on reload, has no supervision and no log file, and couples capture liveness to the dashboard being open. Task Scheduler gives `RestartOnFailure`, `RunOnlyIfNetworkAvailable`, single-flight (`MultipleInstancesPolicy=IgnoreNew`), and per-run logs for free — exactly as the other 30+ tasks do.

**Gmail label (SECONDARY).** Reuse the Google OAuth machinery in `src/integrations/gsheets.py` (`load_credentials`, `data/secrets/` token location, lazy optional-import idiom, the one-time `auth` CLI UX) — but mint a **second token**, because Google tokens are scope-bound and the existing one is `drive.file` only. New `src/capture/gmail.py` with `GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)`, creds at `data/secrets/gmail_credentials.json` (can share the same downloaded client-secrets), token at `data/secrets/gmail_token.json`. Owner sets a Gmail filter that labels share-to-self voice memos `Capture/Inbox`; poller query `label:Capture/Inbox -label:Capture/Done`, walks parts for `audio/*`, then relabels `Capture/Inbox→Capture/Done` (the visible "processed" signal and the dedup latch). Gmail runs in the **same poller process** on a slower cadence (every ~5th long-poll cycle ≈ 4 min) so it adds no API load. One-time consent via `execution/capture_gmail_auth.py` (mirrors `dcf_sheets.py auth`).

**Desktop Ctrl+. tray (at-desk secondary).** A loopback, CSRF-guarded `POST /api/capture/text {text}` route → `ingest_capture(channel="tray", media_kind="text", ...)`. This is a new branch inside the existing `def panel_fragment(name)` dispatcher (`execution/comments_server.py:602`) — a sibling case, not a brand-new route module. No new infra.

### 2.2 Voice transcription — local faster-whisper (decisive)

`faster_whisper.WhisperModel` is **already a `requirements.txt` dependency**, already imported in `execution/fetch_audio_transcripts.py:35`, already wired to off-PATH ffmpeg via `FFMPEG_LOCATION` (`C:/ffmpeg/bin/ffmpeg.exe`). "Local STT" here is a library call inside the poller, not a sidecar to babysit — the cost the original directive feared is already paid. Routing STT as a governed cloud purpose would be *more* new surface: `src/llm/cli.py` pipes a text prompt via stdin to `claude -p` and has **no audio channel**, so it would need a brand-new multimodal transport and a per-second budget the ledger can't model. We keep audio local (and off-box — a free privacy win) and cloud only the cheap text-structuring step.

```python
# src/capture/transcribe.py — model loaded ONCE at poller start (module global), not per-message
FFMPEG = os.environ.get("FFMPEG_LOCATION", r"C:/ffmpeg/bin") + "/ffmpeg.exe"
subprocess.run([FFMPEG, "-y", "-i", oga_path, "-ar", "16000", "-ac", "1", wav_path], check=True)
text = _MODEL.transcribe(wav_path)[0]   # WhisperModel("base", device="cpu", compute_type="int8")
```

### 2.3 The capture pipeline

Single entry point `src/capture/ingest.py::ingest_capture(...)`, called by all three adapters. The ordering **is** the never-lose-words guarantee — raw persists before any fallible step, purge runs only after a durable note exists:

```python
def ingest_capture(*, channel, external_ref, media_kind, audio_path=None, db_path=None, user_id=DEFAULT_USER_ID):
    sess = stage_raw(channel, external_ref, media_kind, audio_path, ...)   # 1. PERSIST RAW FIRST (idempotent on unique idx)
    if sess is None: return                                                #    dedup hit -> no double-land
    raw = transcribe(audio_path) if media_kind == "voice" else read_text(...)   # 2. fallible; raw already safe
    set_session(sess.id, raw_text=raw, status="transcribed")
    scrubbed = scrub_pii(raw)                                              # 3. light regex scrub (pre-LLM, pre-log)
    set_session(sess.id, scrubbed_text=scrubbed, status="scrubbed")
    ticker, method = match_roster_ticker(scrubbed, db_path=db_path, ...)   # 4a. DETERMINISTIC ticker match
    summary, cost, prompt_sha = summarize_utterance(scrubbed)             # 4b. governed LLM (musing_structure)
    ctx = {"channel": channel, "capture_session_id": sess.id, "match_method": method, "musing": True}
    if method == "multi": ctx["needs_ticker"] = True
    note = create_note(user_id=user_id, ticker=ticker, kind="musing",     # 5. LAND via the EXISTING writer
                       body=scrubbed, source="capture",
                       source_ref=f"{channel}:{external_ref}", context=ctx, db_path=db_path)
    write_audit(capture_session_id=sess.id, channel=channel, utterance_summary=summary,
                action_chosen="musing", note_id=note.id, ticker=ticker, match_method=method,
                prompt_sha256=prompt_sha, cost_micro_usd=cost, db_path=db_path)  # 6. DURABLE summary audit
    purge_session(sess.id, note_id=note.id, db_path=db_path)               # 7. PURGE raw+audio, keep audit shell
    if audio_path: os.remove(audio_path)
```

`write_audit` carries `prompt_sha256` at write time (returned by `summarize_utterance` alongside the summary + cost) so the durable audit row can actually join to `llm_calls` (§2.4) — the join is real, not aspirational.

**Scrub** (`src/capture/scrub.py`) — regex only, no MNPI list, runs before any log line:

```python
_PATTERNS = [
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[redacted-phone]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),          "[redacted-email]"),
    (re.compile(r"\b\d{8,17}\b"),                      "[redacted-acct]"),
]
```

**Deterministic ticker match** (`src/capture/matcher.py`) is NOT the LLM's job. It tokenizes the scrubbed text, queries the `tracked_companies` roster (`src/db.py:125`, active = `archived_at IS NULL`), and canonicalizes each candidate through `alias_manager.resolve_ticker` (`src/alias_manager.py:81`, GOOGL→GOOG, dot/dash folding). Returns `(ticker|None, method)` with `method ∈ {roster_exact, roster_alias, none, multi}`. One hit → that ticker; many → `ticker=None` + `needs_ticker`; none → `ticker=None` portfolio-level note. Words **always land** — no-fit is surfaced for one-tap disambiguation in the existing Triage surface, never silently flattened (Law 1, closed-under-no-fit). The LLM's `theme_hint` is advisory only.

**Land** via `user_state.notes.create_note` — the single durable write path (validates `kind`/`source`, upper-cases ticker, JSON-encodes context, `status='open'`). We extend two tuples (code, no migration, since the columns have no CHECK):

```python
NOTE_KINDS   = ("question", "decision", "watch", "assumption", "observation", "musing")
NOTE_SOURCES = ("comment", "chat", "alert", "manual", "advisor", "capture")
```

`musing` is the captured-thought kind; `capture` is the honest provenance (distinct from owner-typed `manual`, the same reason `advisor` was added in 0077). `list_triage_notes` keys on `source='comment'`, so musings are correctly excluded from that surface.

### 2.4 Migrations (DDL)

Three concerns, **two** migration files (the enum extension is code-only). Both: naive-UTC TEXT timestamps via `now_iso()` (`src/user_state/_db.py:50`), no FK `REFERENCES`, idempotent `if table in inspect(...).get_table_names(): return`. Pick `down_revision` at **rebase time** against the live head (the index churns daily; 0114 is the placeholder).

**Migration A — `0115_raw_capture_sessions`** (TRANSIENT staging; words land here before any fallible step):

```sql
CREATE TABLE raw_capture_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'bhanu',
  channel TEXT NOT NULL,            -- telegram | gmail | tray
  external_ref TEXT NOT NULL,       -- "<chat>:<msg>" | gmail msg id | tray uuid  (dedup key)
  media_kind TEXT NOT NULL,         -- voice | text
  audio_path TEXT, raw_text TEXT, scrubbed_text TEXT,
  status TEXT NOT NULL DEFAULT 'received',   -- received|transcribed|scrubbed|extracted|failed|purged
  note_id INTEGER, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, purged_at TEXT
);
CREATE UNIQUE INDEX ix_raw_capture_user_channel_ext ON raw_capture_sessions(user_id, channel, external_ref);
CREATE INDEX ix_raw_capture_status ON raw_capture_sessions(status);
```

The `UNIQUE(user_id, channel, external_ref)` index is the **idempotency latch** — a re-polled Telegram update or re-seen Gmail label hits it and `stage_raw` returns `None`, so the pipeline no-ops (mirrors `source_ref` dedup in `sync_store_comments`).

**Migration B — `0116_capture_audit_log`** (DURABLE summary-level; survives the raw purge):

```sql
CREATE TABLE capture_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'bhanu',
  capture_session_id INTEGER,       -- back-pointer to the (purged) staging row
  channel TEXT NOT NULL,
  utterance_summary TEXT NOT NULL,  -- ONE-LINE LLM summary; the durable trace, NOT verbatim words
  action_chosen TEXT NOT NULL,      -- musing | research | synthesis | decision | needs_ticker
  note_id INTEGER, ticker TEXT, match_method TEXT,
  llm_purpose TEXT, llm_model TEXT, prompt_sha256 TEXT, run_id TEXT,
  cost_micro_usd INTEGER DEFAULT 0, latency_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX ix_capture_audit_user_created ON capture_audit_log(user_id, created_at);
CREATE INDEX ix_capture_audit_action ON capture_audit_log(action_chosen);
```

This is the cost-cap accounting surface (sum `cost_micro_usd` per window) and the substrate the Phase-1 research trail extends into. It stores the summary, **never the raw words** — privacy holds even in the durable log. The `prompt_sha256`/`run_id` columns are written by `write_audit` at land time (§2.3), so the join to `llm_calls` for per-call detail is concrete, not aspirational.

### 2.5 Reuse seams (exact modules)

| Need | Reuse | File |
|---|---|---|
| Durable note write | `create_note` / `supersede_note` | `src/user_state/notes.py:138,405` |
| Cross-table linkage (later) | `journal_links.link_note` over `set_note_links` | `src/journal_links.py:251`, `notes.py:365` |
| Ticker canonicalize | `alias_manager.resolve_ticker` | `src/alias_manager.py:81` |
| Roster | `tracked_companies` (active = `archived_at IS NULL`) | `src/db.py:125` |
| Local STT + ffmpeg | faster-whisper pattern | `execution/fetch_audio_transcripts.py:35` |
| Google OAuth pattern | `load_credentials`, `data/secrets/`, lazy import | `src/integrations/gsheets.py` |
| Secret-file pin pattern | `load_pins` gitignored under `data/` | `sync_list_type_from_holdings.py` |
| naive-UTC | `now_iso()` | `src/user_state/_db.py:50` |
| Subprocess-stage cron | `.bat` + `*.task.xml` + `IgnoreNew` | `cron/`, `SETUP_WINDOWS_SCHEDULER.md` |
| Panel-fragment dispatch (tray + reader) | `panel_fragment` / `render_panel_fragment` | `execution/comments_server.py:602,909` |
| Governed LLM | `call_llm` / `call_llm_structured` | `src/llm/cli.py`, `src/llm/structured.py` |
| Atomic JSON materialize | `tempfile.mkstemp` + `os.replace` | `src/candidate_fit_cache.py:198` |

### 2.6 Seeded synthesis (the day-one payoff — Wave B)

A **theme** is a durable cluster of musings + decisions expressing one stance about one (ticker|portfolio) subject. Synthesis sits *on top of* notes — musings ARE notes, so the priors anchor, supersede-chains, and backlinks work for free.

**Migration `0117_themes`** (chains after 0116). Three tables + the repo's first **FTS5** index over `analyst_notes.body`:

```sql
CREATE TABLE themes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
  ticker TEXT,                          -- NULL = portfolio-level
  slug TEXT NOT NULL, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',  -- active|dormant|retired
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(user_id, slug)                 -- code-level RI only
);
CREATE TABLE theme_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT, theme_id INTEGER NOT NULL,
  member_kind TEXT NOT NULL,            -- note | decision | memo
  member_id INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
  added_at TEXT NOT NULL, UNIQUE(theme_id, member_kind, member_id)
);
CREATE INDEX ix_theme_members_member ON theme_members(member_kind, member_id);
CREATE TABLE theme_stances (         -- insight_notes shape: one LIVE row/theme; correction = new row + supersedes_id
  id INTEGER PRIMARY KEY AUTOINCREMENT, theme_id INTEGER NOT NULL, user_id TEXT NOT NULL,
  stance_md TEXT NOT NULL, summary TEXT NOT NULL, conviction TEXT,
  member_watermark INTEGER NOT NULL,   -- max(theme_members.id) folded into this stance
  status TEXT NOT NULL DEFAULT 'live', supersedes_id INTEGER, synthesized_at TEXT NOT NULL
);
CREATE INDEX ix_theme_stances_live ON theme_stances(theme_id, status);

CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
  body, content='analyst_notes', content_rowid='id', tokenize='porter unicode61');
-- + AFTER INSERT/DELETE/UPDATE triggers keeping the index in step; backfill in upgrade()
```

**Guard:** the migration checks `PRAGMA compile_options` for `ENABLE_FTS5`; absent → create a plain shadow table + LIKE fallback so the reader never 500s (same degrade-to-empty discipline as `list_triage_notes`). FTS5 here is **only the reader's search index** — the surface that lets the owner full-text-search across musings. It is *not* a clustering input.

**Day-one SEED** — `execution/seed_themes.py`, run once, three sources all already present:
1. **Existing `analyst_notes`** — pull live notes (`list_notes`, status=None), group by ticker, then **cluster via one `theme_seed_cluster` LLM call per ticker** (Haiku, already budgeted in §2.7). Clustering is the LLM call; FTS5 gives full-text *match*, not similarity, so it is not leaned on for grouping. Each returned cluster → a `themes` row + members.
2. **Theses** — each ticker's `thesis_one_liner` (`src/advisor/memos.py:49`) seeds a baseline theme.
3. **Recent decisions** — last 180 days of `decisions` (0046) attach as `member_kind='decision'`, anchoring conviction history.

Each seeded theme writes its initial `theme_stances` row through the **exact `persist_memo` path** (`memos.py:168`) so a seeded stance is indistinguishable from a synthesized one.

**Manual seeding interview** — the owner narrates recent moves by voice via Telegram. Each narration lands as `kind='musing'` through the **same live pipeline**, then one schema-validated pass splits it into `{musings[], decisions[], theme_assignments[]}`: decisions land in `decisions` (0046), musings land as notes, assignments create/extend themes. The interview is just live capture replayed in bulk — no special path.

**Note on `decisions` mutability — for the capture writer.** Treat the conviction-% and the falsifier as **write-once fields**, but NOT the row: `decisions` is not append-only. `src/decision_conditions.py:744` and `:847` both run `UPDATE decisions SET decision_conditions=…/qualitative_conditions=…` to backfill the conditions/qualitative columns post-hoc. So the capture path must (a) never re-write conviction-% / falsifier once set, and (b) never enforce row-level immutability or otherwise lock the row — doing so would break the existing `decision_conditions` extractor that mutates those other columns later.

**Synthesis cadence** — a new **morning-pipeline stage 0g** (`src/synthesis/theme_synth.py::run_theme_synthesis`, after `candidate_fit` 0f), incremental and **watermarked**: only themes whose `max(member_id) > live_stance.member_watermark` spend an LLM call; a quiet week costs $0. The prior stance is fed in (incremental refinement, not re-derivation). Hard stops propagate, transient failures skip-with-reason (`is_hard_stop`) — one flaky theme never aborts the run. After persisting stances, the stage materializes `data/themes.json` via `tempfile.mkstemp` + `os.replace` (last-good: a crashed run leaves the prior cache intact). The render path **never touches the LLM or the slow DB views** — the no-build HTMX/Alpine panel reads disk.

**Reading surface** — `GET /api/panel/musings`, a new branch in `panel_fragment` (`execution/comments_server.py:602`), sibling to `journal`. In **Wave A** it renders a flat, reverse-chronological list of captured musings straight from `analyst_notes` (no themes yet) — enough to dogfood capture immediately. In **Wave B** it switches to read **by theme**, each theme a vertical thread of musings + decisions ordered `created_at ASC`, with each superseded stance a milestone marker (the "how my view evolved" timeline). Drift detection reuses `src/timeseries/primitives.py` unmodified: map a theme's conviction signal to an `Observation` series → `detect_trend` (slope + Mann-Kendall p) / `detect_inflection` (PELT changepoint) → "your view turned on {date}". This is the Phase-2 coaching signal, grounded in an existing primitive — no new math.

### 2.7 New LLM purposes (Phase 0) + routing

| Purpose | Model / tier | Schema | Budget | Eval |
|---|---|---|---|---|
| `musing_structure` | **Haiku** (narrow closed-vocab JSON, off hot path) | `{ticker?, theme_hints[], claims[], stance, falsifier?, kind}` | warn | golden set (transcript→fields) |
| `theme_synthesis` | **Sonnet** (owner-facing prose, matches `earnings_themes_split`) | `{stance_md, summary, conviction}` | warn | Mode-B rubric |
| `theme_seed_cluster` | **Haiku/Gemini-flash** (closed clustering, seed-only) | `{clusters:[{title, note_ids[]}]}` | warn | golden set |
| `view_evolved` | **Sonnet** (timeline narrative, faithful to cited notes) | `{timeline:[{date, prior, now, trigger_note_id}]}` | warn | Mode-B rubric |

(STT is **not** a governed LLM purpose — local faster-whisper, §2.2.) Each purpose ships the 4-registry lockstep (§5.3) plus a budget-row migration. `musing_structure` is `warn` (a blown cap drops the capture to triage rather than hard-failing the poller).

### 2.8 Phase-0 kill/keep gate

The kill-gate clock **starts at first capture** (Wave A), not at full synthesis — the owner is dogfooding the capture loop within days, and the off-desk-friction signal accrues from day one rather than after a multi-PR dark period.

**KEEP** if, across ~2–3 weeks, the owner captures **a few times/week unprompted** and (once Wave B lands) reads by theme at least weekly. **KILL/PIVOT** if capture is forgotten (the off-desk friction is too high), themes read as noise (clustering is wrong), or the deterministic ticker match misses constantly. Only on KEEP do we consider a first-class `'musing'` CHECK migration — don't migrate for an unproven feature.

---

## 3. Phase 1 — the conversational research loop

Consumes the Phase-0 musing/theme substrate; writes **nothing live**.

### 3.1 wondering → research

Detection is a **fire-and-forget tap off `respond_turn`** (`src/ask/engine.py`) and inside the Phase-0 ingest pipeline, fronted by a **NEW deterministic regex pre-gate** (`do .* still|is .* really|why is|what if|I wonder|does .* hold|how .* compare`) that decides whether the classifier LLM fires at all, so a flat observation costs zero tokens. This gate is modeled on the ask-router's claim pre-filter (`src/ask/router.py`) — that is the real, in-repo seam to follow. The software-feedback "a thought must never be misread as a request" asymmetry is a **design analogy, not a seam to import**: `feedback_triage`/`is_wondering` do not exist in this repo (they belong to the user's separate Cowork framework), so build the gate net-new rather than reaching for a non-existent module. Default verdict `is_wondering=false` (the asymmetry, encoded explicitly). The classifier purpose `wondering_detect` (Flash-Lite, `skip`-budgeted, `call_llm_structured`) emits `{is_wondering, claim, ticker, suggested_artifacts[]}`. A positive verdict materializes a `research_task` row (status `proposed`) and an **inert chip** — never auto-runs. The chip's one tap (`POST /research/<id>/run`) is the only thing that spends a research budget (SEMI-AUTO; full-auto is the maturity vision). Identical no-pre-granted-capability posture to the `spawn_task` chip.

### 3.2 The budget TIER

Two inputs, both already on disk — no live calls on this path:
- **(a) Portfolio weight** — `portfolio_weights.read_materialized_weights(repo_root)` over `data/portfolio_weights.json` (materialized by morning-pipeline stage 0c; the exact seam `inbox_rank._materialized_weights` uses at `src/dashboard/inbox_rank.py:320,330` — reuse verbatim, never the live tracker).
- **(b) Actively-considering** — OR of a **manual hot-flag** (new `research_hot_flags(ticker, set_at, expires_at)`, time-boxed, settable from Telegram or the holding rail) and an **inferred attention-spike** (z-score of musing count over a trailing window vs. the ticker's own baseline, via `timeseries/primitives.py`). The `list_type_reconcile` evaluation/portfolio seam feeds a **floor** so unheld evaluation names aren't starved of entry-tier diligence.

| Tier | Condition | Web budget/run | Touch web | Draft artifacts |
|---|---|---|---|---|
| **deep** | weight ≥ 5% OR (hot-flag/spike AND held) | `--max-budget-usd 2` | ≤2 searches | all 5 |
| **standard** | held, no spike | $0.50 | ≤1 search | memo / view / thesis |
| **entry** | evaluation / unheld / cold | $0.15 | none | memo only |

The per-run cap routes through `call_llm_with_web(..., purpose='research_orchestrate', max_budget_usd=...)`, whose `--max-budget-usd` flag **hard-terminates** a runaway at `src/llm/cli.py:425` (it "terminates the call once API spend hits it") — the prompt's "AT MOST N searches" is advisory, not trusted alone. Because `research_orchestrate` fires on a **user-facing synchronous tap**, its *monthly* `llm_budget` row is `warn`, not `skip` (§5.1): the per-run ceiling already hard-caps each run, so a blown monthly cap should **degrade the tap to a read-only proposal** ("budget tight — proposal only"), never hard-fail the tap the owner just pressed. A separate **weekly count-cap** governs the genuinely-autonomous code leg (it bills outside `llm_calls`, on the `autonomous_build` row, which is correctly `skip`/fail-closed).

### 3.3 The five artifact proposals + the higher bar

A tapped task runs `call_llm_with_web` (purpose `research_orchestrate`, Sonnet) grounded in DB + web within tier budget, drafting up to five **inert proposal rows**:

1. **Memo + evidence** → `llm_artifacts` row (the table `inbox._synthesis_items` reads), `purpose='artifact_draft_memo'`, evidence doorways (fact_ref/news_id/note_id — Law 2).
2. **DCF scenario/assumption tweak** → a **dry-run** `DcfRunRow` (`src/dcf/persist.py` + `build_assumption_snapshot`) recomputed in Python via `dcf/reprice.py`, persisted with a `proposed` flag — **never** upserted into the live survivor row. Diff = old-vs-proposed `assumption_snapshot_json` + `npv_per_share`.
3. **Thesis/ledger edit** → a drafted `ThesisLedgerEntryRow` (`src/user_state/ledger.py:append_entry`) held as a proposal, not appended (routes to the company surface, not a net-new path).
4. **Saved view/chart** → a **ViewSpec** JSON (`src/viewspec/spec.py:from_dict` validates hard) drafted but not written to `saved_views` (0079); preview executes LLM-free via `viewspec.engine.execute_view` (deterministic).
5. **Code/feature change** → the `spawn_task` path → isolated worktree → reviewable diff, never a live mutation. The spawned coder **binds to the scrubbed fixture DB (asserted before spawn) and runs network-less** — never prod-read + web + action-write together (the trifecta defense). The worktree-resolves-to-MAIN footgun is real (MEMORY) → the bind is **asserted, not assumed**.

**Higher bar for mutating artifacts (types 2/3/5).** An apply affordance renders only when **all three** clear, OR an explicit steer authorizes it:
- **Evidence-gated** — ≥1 concrete fact_ref/URL doorway, not prose.
- **Adversarial-survived** — the self-assessment (§3.5) failed to refute.
- **Deterministic numeric/oracle** — any valuation/KPI/financial-math diff validates the affected `fact_ref` against ground truth ("tests green is insufficient for numeric correctness"). DCF dry-run recomputes in Python; the code diff runs diff-aware CI + a golden numeric check **before the merge button renders**.

A diff touching a **safety control** (redactor, retention TTLs, kill switches, provenance tagging, trifecta isolation, `src/llm` governance) renders a **distinct red merge path** requiring explicit acknowledgement — never routine one-click.

**Results fold into the THEME** — the artifact's evidence updates that theme's "how my view evolved" timeline, not a loose feed.

### 3.4 Inbox 4-action surface + Telegram mirror

The proposal renders as an `InboxItem` (`src/dashboard/inbox.py`), ranked by `inbox_rank.annotate_and_rank` (severity × recency × **position weight** × thesis tone, the same transparent `score_why` tooltip). A new `semantic_kind='research_proposal'` (Law 1) gives it a category facet and a four-action footer extending the existing `_render_card_footer` / HTMX quick-action pattern:

- **approve** → applies the artifact (mutating types require the higher bar already cleared → flips to the real write: `append_entry`, `dcf.persist.upsert`, `saved_views` insert, or worktree merge).
- **research-further** → re-runs at the next tier up, folding new evidence in.
- **steer** ("look into this direction") → free-text that **authorizes** a mutating proposal even absent full adversarial survival; re-runs grounded by the steer text.
- **reject** → supersede-chains the proposal closed (append-only) and **bumps the future threshold** for that signal type (you train the loop down).

**Telegram mirror.** Each proposal posts to the thread with an inline keyboard of four `callback_data` buttons (`research:<id>:approve|further|steer|reject`). A press arrives via the same long-poll loop and routes to the **identical handler** the inbox HTMX endpoint uses — one action core, two surfaces. **A free-text reply IS a steer** — captured, attached, re-runs the task. The off-desk owner drives the whole loop from voice + taps.

### 3.5 Adversarial self-assessment (assert-vs-Socratic)

Before any proposal surfaces, a second pass (`call_llm_structured`, purpose `research_adversarial_assess`, Sonnet — *not* Opus; owner decision: nothing to Opus except the code leg) **tries to refute the loop's own claim** against the same evidence: `{survives, strongest_counter, residual_uncertainty, recommended_tone:'assert'|'hedge', socratic_q?}`.

- **Survives** → the memo asserts conclusively; mutating proposals become higher-bar-eligible.
- **Fails** → the memo **hedges and asks a Socratic question back** ("the counter is X — does that change your conviction?"), routed into the theme, mirrored to Telegram as a question (never a verdict), downgraded to insight-lane reading, not an actionable apply.

> **Verify before relying on a non-decay lane.** The intended home for a "fails" insight is a non-decaying insight lane à la `signals/store.py`. Confirm by line-check that `src/signals/store.py` actually exposes an insight lane with a non-decay flag before wiring the downgrade to it; the other signals-table claims in MEMORY are real, but this specific non-decay lane is unverified. If it isn't there, route the hedged insight to a plain low-priority inbox insight (no decay applied) rather than inventing a store capability.

The verdict is logged in the summary-level audit row so calibration is observable: utterance(summary) → claim → adversarial outcome → assert/hedge → cost. *The research loop IS the coaching* (epistemic-primary).

### 3.6 Phase-1 kill/keep gate

**KEEP** if one-tap research produces proposals the owner actually approves/steers (not reflexively rejects), and the adversarial calibration reads as *useful* hedging rather than noise. **KILL/PIVOT** if proposals are low-signal, the budget tiers misfire (spend without value), or the owner ignores the Telegram mirror. The mutating-artifact higher bar must have had **zero** bad live writes — any single bad auto-applied mutation is a kill-criterion for the apply path (demote everything to read-only proposals).

---

## 4. Phase 2 — behavioral drift + decision capture

### 4.1 Conviction-drift (secondary, quiet)

Behavioral coaching is **secondary and quiet** — the research loop already does the epistemic work. Drift is the `timeseries/primitives.py` detection from §2.6 run over each theme's conviction series, fed by decision captures. Narration only: purpose `drift_narrate` (Haiku — narration of a *computed* signal, not judgment) → `{drift_summary, magnitude, cited_decisions[]}`, surfaced as a low-priority inbox insight, never an interrupt. Optional `coach_socratic` (Sonnet) for the question voice when drift is significant.

### 4.2 Multi-channel decision capture

Logged **only on position changes above a size threshold** (lighter). Three channels reconcile into one structured shape: via **Telegram**, the **in-app form**, or **recognized from an ambient voice musing** (the Phase-0 pipeline already classified `is_decision_capture`). Purpose `decision_extract` (Gemini-flash, same closed-schema shape as `decision_conditions_extract`) → `{ticker, direction, size, conviction_pct, falsifier, missing_field?}`, asking a **one-line follow-up only if a required field is missing**. Lands in `decisions` (0046) and attaches to the originating musing via `journal_links.link_note(note_id, decision_id=…)` — the same validated seam, no new framework.

**Write-once, not row-frozen.** `conviction_pct` and `falsifier` are **write-once fields** — never overwritten once captured. This is *not* row-level immutability: `src/decision_conditions.py:744,847` legitimately `UPDATE decisions SET` for the conditions/qualitative columns after the fact, so the capture writer must leave the row mutable and only guard those two specific fields. Enforcing append-only on the whole row would break the conditions extractor.

### 4.3 Phase-2 kill/keep gate

**KEEP** if drift narration ever changes a decision and multi-channel capture is lighter than the in-app form alone. **KILL** drift if it reads as nagging; **KILL** a capture channel if it produces more reconciliation follow-ups than it saves.

---

## 5. Cross-cutting

### 5.1 Full new-LLM-purpose table

| # | Purpose | Model / tier | Schema (via `call_llm_structured`) | Budget | Eval-ability |
|---|---|---|---|---|---|
| 1 | `musing_structure` | Haiku | `{ticker?, theme_hints[], claims[], stance, falsifier?, kind}` | warn | golden set |
| 2 | `theme_seed_cluster` | Haiku/Gemini-flash | `{clusters:[{title, note_ids[]}]}` | warn | golden set |
| 3 | `theme_synthesis` | Sonnet | `{stance_md, summary, conviction}` | warn | Mode-B rubric |
| 4 | `view_evolved` | Sonnet | `{timeline:[{date, prior, now, trigger_note_id}]}` | warn | Mode-B rubric |
| 5 | `wondering_detect` | Flash-Lite (behind regex pre-gate) | `{is_wondering, claim, ticker, suggested_artifacts[]}` | skip | golden set (real wondering vs. flat observation = highest-value) |
| 6 | `research_orchestrate` | Sonnet | `{subquestions[], packs_needed[], web_needed, artifact_targets[]}` | **warn** (user-facing synchronous tap; per-run `--max-budget-usd` is the hard cap, monthly cap degrades to read-only proposal — must NOT break mid-tap) | Mode-B rubric |
| 7 | `research_adversarial_assess` | Sonnet | `{survives, strongest_counter, residual_uncertainty, recommended_tone, socratic_q?}` | skip | **human rubric only** + spot-check harness |
| 8a | `artifact_draft_memo` | Sonnet | `{title, thesis, evidence:[{claim, cite_note_id}], confidence}` | warn | Mode-B rubric |
| 8b | `artifact_draft_dcf` | Sonnet | `{driver, old, new, rationale, sheet_cell}` | skip | **numeric/oracle** golden + prose rubric |
| 8c | `artifact_draft_thesis` | Sonnet | `{section, old_text, new_text, evidence_ids[]}` | warn | Mode-B rubric |
| 8d | `artifact_draft_view` | Haiku/Gemini-flash | `{viewspec:{metrics[], filters[], chart_kind}}` | warn | golden set (closed vocab) |
| 8e | `artifact_draft_code` | **Opus** (`claude-opus-4-8`) — the ONE code leg | `{spawn_prompt, title, files_touched[], oracle_check}` | skip, on **separate `autonomous_build` line** | **human review + oracle gate**, no golden |
| 9 | `decision_extract` | Gemini-flash | `{ticker, direction, size, conviction_pct, falsifier, missing_field?}` | warn | golden set |
| 10 | `drift_narrate` | Haiku | `{drift_summary, magnitude, cited_decisions[]}` | warn | golden set (signal→sentence) |
| 11 | `coach_socratic` | Sonnet | `{questions:[{q, targets_bias?}]}` | warn | **human rubric only** |
| 12 | `telegram_reply` | Haiku | `{text, buttons:['approve','research_further','steer','reject']}` | warn | golden set (state→reply shape) |

**Budget-mode rule (reconciled):** autonomous lanes are `skip` (fail-closed — the genuinely autonomous `artifact_draft_code`/`autonomous_build` lane, where a tier cap MUST stop spend); user-facing synchronous lanes are `warn` (degrade, never break mid-stream). `research_orchestrate` is synchronous-user-facing → `warn` at the monthly level, with the per-run `--max-budget-usd` ceiling doing the genuine hard-stop. **Tier discipline:** deterministic narration/classification/transcription → Haiku or Gemini-flash; user-facing voice + synthesis → Sonnet; **Opus exactly once** (`artifact_draft_code`), on its own `autonomous_build` budget row so its spend is attributed and hard-capped separately. STT stays off this table (local).

### 5.2 Safety / cost model

- **Transient raw.** Raw `.oga`/`.wav` live in `.tmp/capture/<uuid>/` (gitignored), staged in `raw_capture_sessions`, purged on successful land. The transcript string is dropped too — only the scrubbed, structured musing + the summary audit survive. A failed extraction keeps raw for bounded retry, then purges with a `data/capture/dead_letter.json` breadcrumb (no audio).
- **Light regex scrub** — phone/email/account only, before any LLM call and any log line; no MNPI denylist (owner has none).
- **Durable summary-level audit log** (`capture_audit_log`) — every utterance(summary) → action/research/synthesis the LLM chose → cost, kept for evals/observability. Reuses `llm_call_ledger` privacy discipline: **never raw prompt bodies** (`prompt_sha256` + `prompt_chars` only; full-text only behind opt-in `LLM_CAPTURE_DIR`), coded outcomes from the `is_hard_stop` taxonomy (`ok|budget_block|setup_error|transient_degraded|adversarial_failed|oracle_rejected`), joins to `llm_calls` on `prompt_sha256` + `run_id` (both columns written at land time — §2.3/§2.4).
- **Code-draft worktree gate** — never live-write; scrubbed-fixture + network-less bind asserted before spawn; numeric/oracle + diff-aware CI before the merge button renders; red path for safety-control diffs.
- **Per-tier research budget** — dynamic per-call ceiling (weight × consideration → dollars) resolved by the orchestrator before the call and passed to `--max-budget-usd`; the monthly `llm_budget` row is the backstop. Autonomous lanes are `skip` (fail-closed); user-facing synchronous lanes are `warn` (never break mid-stream).

### 5.3 The 4-registry-lockstep checklist (per new purpose)

1. **`LLM_MODELS`** (`src/llm/cli.py`) — add `"<purpose>": <model id>` + rationale comment. Without it `_model_for` logs `llm_model_purpose_unknown` and silently defaults to Sonnet.
2. **`run_llm_evals`** (`execution/run_llm_evals.py`) — add to `GOLDEN_PURPOSES` (if `evals/golden/<purpose>.json` exists) or `AUDIT_PURPOSES` (rubric). Human-rubric purposes (`research_adversarial_assess`, `coach_socratic`, `artifact_draft_code`) register a spot-check, not an automated mode.
3. **`evals_panel.RUNNABLE_PURPOSES`** (`src/pipeline/evals_panel.py`) — make it runnable from the in-app Evals panel.
4. **`prompt_versions` + `coverage`** (`src/llm/prompt_versions.py`, `src/evals/coverage.py`) — register a versioned prompt AND classify into `GOLDEN_PURPOSES`/`AUDIT_SPECS`/`OUTCOME_PURPOSES`/`META_PURPOSES`. Coverage's universe = `LLM_MODELS` ∪ `prompt_versions` ∪ observed `llm_calls`, so an unclassified purpose surfaces as **uncovered**.

Plus a **5th non-registry step**: an Alembic migration seeding the `llm_budget` row (mode + cap), exactly as `ask_*` did (0089–0091, 0104). `artifact_draft_code` also seeds the distinct **`autonomous_build`** row. Run the **full suite before push** — the three sync guards fail the push if any registry is left blind.

---

## 6. First concrete tickets (ordered Phase-0 PRs)

Each is a small, independently-mergeable PR; edit inside the worktree; gate on touched-file lint only; push needs `--no-verify` (the global pre-push hook runs whole-repo gates blocked by the repo's intentional baselines — real gates are diff-aware CI).

**Two waves, so the dogfood loop ships early.** Wave A (PRs 1–9) lands a *capture-only, theme-less* loop: the owner can speak/type a musing and read a flat list. The kill-gate clock (§2.8) starts the moment Wave A's poller + tray are live — **first capture, not full synthesis**. Wave B (PRs 10–13) layers Gmail, themes, and synthesis on top.

| Wave | # | PR | Touches | Test |
|---|---|---|---|---|
| A | 1 | **Note enums + capture staging migration** | `src/user_state/notes.py` (`+musing`, `+capture`); `alembic/0115_raw_capture_sessions` | unit: `create_note(kind='musing', source='capture')` round-trips; migration up/down on a temp DB; unique-index dedup |
| A | 2 | **Audit-log migration + writer** | `alembic/0116_capture_audit_log` (incl. `prompt_sha256`/`run_id`); `src/capture/audit.py` | unit: `write_audit` row has summary not raw, carries `prompt_sha256`; cost sum per window |
| A | 3 | **Deterministic ticker matcher** | `src/capture/matcher.py` (uses `alias_manager`, `tracked_companies`) | golden: roster_exact / roster_alias / none / multi cases; GOOGL→GOOG |
| A | 4 | **Scrub** | `src/capture/scrub.py` | unit: phone/email/acct redaction; no over-redaction of tickers/$ amounts |
| A | 5 | **Transcribe** | `src/capture/transcribe.py` (faster-whisper + ffmpeg) | unit: `.oga`→text on a fixture clip; model loaded once |
| A | 6 | **`musing_structure` purpose + 4-registry lockstep + budget row** | `src/llm/cli.py`, `run_llm_evals.py`, `evals_panel.py`, `prompt_versions.py`, `coverage.py`; `evals/golden/musing_structure.json`; budget migration | full eval suite green; coverage shows no uncovered row; summarizer returns `prompt_sha256` |
| A | 7 | **`ingest_capture` pipeline** | `src/capture/ingest.py` (stages 1–7), `pipeline.py` | integration: text + voice fixture → note + audit + purge; crash-after-stage-1 leaves raw; dedup no-ops |
| A | 8 | **Telegram client + token store** | `src/capture/telegram.py`, `src/capture/token_store.py` | unit: getUpdates offset cursor; CaptureSetupError on missing token; callback_query parse |
| A | 9 | **Poller process + scheduled task + flat musings list** | `execution/capture_poller.py`, `cron/run_capture_poller.bat`, `cron/capture_poller.task.xml`; `execution/comments_server.py` (`POST /api/capture/text` + flat-list `GET /api/panel/musings`, both new `panel_fragment` branches at :602) | `--once` drains one batch; offset persists across restart; CSRF-guarded tray capture; flat list renders from `analyst_notes`. **Kill-gate clock starts here.** |
| B | 10 | **Gmail adapter + one-time auth** | `src/capture/gmail.py`, `execution/capture_gmail_auth.py` | unit (mocked Gmail API): label query → attachment bytes → relabel |
| B | 11 | **Themes migration + FTS5** | `alembic/0117_themes` (+ FTS5 guard/fallback) | migration up; FTS backfill; compile-options-absent fallback path |
| B | 12 | **Seed (LLM clustering)** | `execution/seed_themes.py`, `theme_seed_cluster` purpose (4-registry) | seed from fixture notes/theses/decisions; clustering is the LLM call (FTS not a clustering input) |
| B | 13 | **Synthesis stage + by-theme panel** | `src/synthesis/theme_synth.py` (stage 0g), `theme_synthesis` purpose (4-registry); `execution/comments_server.py` (panel switches flat→by-theme) | watermark skip = zero-call; `data/themes.json` atomic write; by-theme timeline renders from disk |

Build-order rationale: data spine first (1–2), pure deterministic units next (3–5), the governed LLM purpose (6) before the pipeline that calls it (7), then the Telegram adapter + poller (8–9) — **at which point the capture-only loop is usable and the kill-gate begins**. Wave B adds Gmail (10) and the theme/synthesis layer (11–13). The by-theme "how my view evolved" payoff lands at PR 13, but the owner is already dogfooding capture after PR 9.

---

## 7. Open implementation risks

| Risk | Why it's hard | De-risk |
|---|---|---|
| **Telegram poller process model on Windows** | A long-running loop under Task Scheduler can wedge (network stall, Telegram 409 on duplicate `getUpdates`, silent death) with no one watching. | `RestartOnFailure(PT30M, count 2)` + `RunOnlyIfNetworkAvailable` + `IgnoreNew` single-flight; `--once` mode for manual drain; persisted offset so restart never re-ingests; `getUpdates` 409 means a second poller is running — assert single-flight and back off. Heartbeat to the TS-stamped log; a stale log surfaces a dead poller. |
| **Voice transcription cost/accuracy** | Whisper `base` may mis-hear tickers ("Nu" vs "new", "MELI" vs "Molly"); CPU latency on long clips. | Cost is **$0** (local). Accuracy is contained because **ticker match is deterministic over the roster**, not Whisper's job — a misheard ticker just yields `needs_ticker` (surfaced, never wrong-attributed). Upgrade `base`→`small` if WER on a golden audio set is poor; clips are short (seconds–2 min). |
| **FTS5 not compiled in SQLite** | First FTS5 use in the repo; a stock Python SQLite may lack `ENABLE_FTS5`. | Migration guards on `PRAGMA compile_options`; absent → plain shadow table + LIKE fallback so the reader never 500s. FTS5 is the reader's search index only, never a clustering input. Verify on the prod DB build before PR 11 merges. |
| **Gmail second-token scope friction** | Tokens are scope-bound; the Drive token can't be widened; consent UX is one-time but easy to misconfigure. | New `gmail.modify` token at `data/secrets/gmail_token.json`, separate from Drive; `execution/capture_gmail_auth.py` mirrors the proven `dcf_sheets.py auth` flow; relabel-based dedup gives a visible "processed" signal. Gmail is SECONDARY — Telegram works without it. |
| **Worktree-resolves-to-MAIN footgun (Phase 1 code leg)** | A spawned coder can resolve its DB/worktree to MAIN and live-write (MEMORY documents this). | Bind to the scrubbed fixture DB is **asserted before spawn** (not assumed), network-less, never prod-read + web + write together. No live mutation without the higher bar + explicit merge; safety-control diffs get the red path. |
| **`signals/store.py` non-decay lane may not exist** | §3.5 routes hedged "fails" insights to a non-decaying insight lane; that specific capability is unverified (other signals-table claims in MEMORY are real). | Line-check `src/signals/store.py` for an insight lane + non-decay flag before wiring; if absent, route hedged insights to a plain low-priority inbox insight (no decay) rather than inventing a store capability. |
| **Budget-tier misfire (Phase 1)** | The weight × consideration ceiling could under-fund a hot name or over-spend on a cold one. | Tier is a transparent lookup (no ML), inputs already on disk (no live calls); `--max-budget-usd` hard-terminates regardless of prompt; weekly count-cap on the code leg; `skip` budget fails closed on the autonomous lane, `warn` degrades the synchronous tap. Tune thresholds against the audit log after Phase 1 ships. |
| **`down_revision` collisions (parallel sessions)** | The alembic head churns daily; 0115–0117 are placeholders. | Pick `number` + `down_revision` at **rebase time** against the live linear head (confirmed `0114_decision_process_quality`; ignore the `b79cec08ce5b` saydo branch). |
| **Musing as `observation`+flag vs. first-class kind** | Shipping `kind='musing'` touches a tuple readers may not expect; reverting after a KILL is cleaner if we avoided a CHECK migration. | The columns have **no CHECK** — extending the tuple is reversible code, no migration. Defer any first-class `'musing'` CHECK constraint until the Phase-0 keep gate passes. |