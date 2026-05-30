# Plan: `news` table + ingestion for the material-news trigger

> Status: PLAN FOR REVIEW — not implemented. This document is the only artifact in this branch.
> Author: planning session, 2026-05-30.
> Scope: design the structured `news` SQLite table and the ingestion path that fills it, so the
> already-built `material_news` trigger ([src/triggers/material_news.py](src/triggers/material_news.py)) produces real alerts.

---

## 1. Summary

The material-news sensor is **fully built, wired into the driver, typed, and tested** — but **dormant**.
Its `scan()` reads a `news` table that no migration creates, so the `_has_table` guard short-circuits and
it returns `[]` forever ([src/triggers/material_news.py:225-233](src/triggers/material_news.py), docstring
:33-44). It is registered in `ENABLED_TRIGGERS` (registry.py:24-29, per investigation) and its artifact
purpose is already a member of `FACT_DEPENDENT_PURPOSES` ([src/llm_artifact_store.py:330](src/llm_artifact_store.py)),
so nothing else has to change in the trigger framework.

**The fix, in two sentences:** Add an Alembic migration that creates a `news` table whose six trigger-read
columns match the sensor's `_NEWS_*` constants exactly, plus a per-ticker ingestion fetcher
(`execution/fetch_fmp_news.py`) that pulls structured rows from FMP's stock-news endpoint, normalizes
timestamps to UTC, validates with a Pydantic pre-insert gate, and idempotently upserts. Then slot the
fetcher into `run_morning_pipeline.py` as a new stage *before* the trigger stage so each morning's fresh
news is classified the same run. **No edits to `material_news.py` are required** — the schema is designed
to satisfy its existing contract verbatim.

---

## 2. Schema

### 2.1 The exact contract the table must satisfy

The sensor centralizes its column contract in module constants
([src/triggers/material_news.py:100-117](src/triggers/material_news.py)):

```python
_NEWS_TABLE          = "news"
_NEWS_COL_ID         = "id"
_NEWS_COL_TICKER     = "ticker"
_NEWS_COL_HEADLINE   = "headline"
_NEWS_COL_URL        = "url"
_NEWS_COL_PUBLISHED_AT = "published_at"
_NEWS_COL_SNIPPET    = "snippet"

_NEWS_SELECT_SQL = (
    "SELECT id, headline, url, published_at, snippet "
    "FROM news "
    "WHERE ticker = ? AND published_at >= ? "
    "ORDER BY published_at DESC LIMIT ?"
)
```

Row-unpacking + type validation in `_load_recent_news` ([:248-274](src/triggers/material_news.py)) requires:

| column         | runtime type required by `_load_recent_news` | notes |
| :------------- | :------------------------------------------- | :---- |
| `id`           | `int` (else row skipped, :257-263)           | the candidate key — `signature_key_evidence` hashes `news_id` alone (:561-569) |
| `ticker`       | `str` (filter param)                          | per-ticker association column |
| `headline`     | `str` (else row skipped)                      | |
| `url`          | `str` (else row skipped)                      | |
| `published_at` | `str` (else row skipped)                      | **must be `'YYYY-MM-DD HH:MM:SS'` UTC** — see 2.3 |
| `snippet`      | `str \| None` (kept only if non-empty str, :264) | nullable |

**The authoritative shape is the test fixture** ([tests/test_trigger_material_news.py:50-59](tests/test_trigger_material_news.py)):

```sql
CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    snippet TEXT
)
```

The migration's table must be a **superset** of this: the six read columns identical in name and SQLite
affinity, plus ingestion bookkeeping. Because `_NEWS_SELECT_SQL` names columns explicitly (never `SELECT *`),
adding columns is safe and invisible to the trigger.

**Constant edits required: NONE.** The schema below uses the exact names the constants already declare.

### 2.2 The migration — `alembic/versions/0065_news.py`

Head revision today is `0064_queued_actions` (revises `0063_alerts`)
([alembic/versions/0064_queued_actions.py:61-62](alembic/versions/0064_queued_actions.py)). The new
migration revises it. Pattern is copied verbatim from `0063_alerts.py` (inspector-guarded idempotent
upgrade/downgrade, `op.create_table` + `op.create_index`):

```python
"""news — structured per-story news table feeding the material_news trigger.

The material_news sensor (src/triggers/material_news.py) reads recent stories
per ticker and asks the LLM whether each is material to the thesis. Until this
table existed, scan() hit the _has_table guard and returned [] forever. This
migration creates the structured per-story store the sensor's _NEWS_* column
contract expects.

The six trigger-read columns (id, ticker, headline, url, published_at, snippet)
match src/triggers/material_news.py:100-106 EXACTLY. The remaining columns are
ingestion bookkeeping (which feed wrote the row, the source site, when it was
fetched) and are invisible to the trigger (its SELECT names columns explicitly).

published_at is stored as 'YYYY-MM-DD HH:MM:SS' in UTC (naive) so the sensor's
lexical `published_at >= ?` recency compare (_format_threshold, :210-216) is
also chronological. The loader is responsible for that format + UTC offset; the
TEXT column cannot enforce it (see the plan's Risks section).

Dedup: UNIQUE (ticker, url). One row per (ticker, article); the same syndicated
URL may legitimately appear under two tickers, so url alone is NOT unique.
The loader relies on this constraint for INSERT OR IGNORE idempotency.

Revision ID: 0065_news
Revises: 0064_queued_actions
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065_news"
down_revision: str | Sequence[str] | None = "0064_queued_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "news" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "news",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),  # 'YYYY-MM-DD HH:MM:SS' UTC
        sa.Column("snippet", sa.Text(), nullable=True),
        # --- ingestion bookkeeping (invisible to the trigger) ---
        sa.Column("source", sa.Text(), nullable=True),         # FMP 'site', e.g. 'Reuters'
        sa.Column(
            "source_feed",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'fmp_stock_news'"),         # which ingester wrote it
        ),
        sa.Column("fetched_at", sa.Text(), nullable=False),    # 'YYYY-MM-DD HH:MM:SS' UTC
        sa.UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),
    )
    # Serves the trigger's WHERE ticker = ? AND published_at >= ? ORDER BY published_at DESC.
    op.create_index(
        "ix_news_ticker_published",
        "news",
        ["ticker", sa.text("published_at DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "news" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("news")}
    if "ix_news_ticker_published" in existing:
        op.drop_index("ix_news_ticker_published", table_name="news")
    op.drop_table("news")
```

### 2.3 The single most important schema invariant — `published_at` is UTC `'YYYY-MM-DD HH:MM:SS'`

The trigger computes its recency threshold as
`datetime.now(UTC).replace(tzinfo=None)` minus 24h, formatted with `"%Y-%m-%d %H:%M:%S"`
([:210-216](src/triggers/material_news.py), :235, :495). The comparison is a **lexical** `published_at >= ?`.
That comparison is only chronologically correct if every stored value is:

1. **UTC** (naive), to match `datetime.now(UTC)`, and
2. **exactly `'YYYY-MM-DD HH:MM:SS'`** — fixed width, space separator, no `T`, no timezone suffix, no
   fractional seconds. An ISO-8601 value like `2026-05-30T14:00:00` sorts *after* a same-instant
   space-separated value (`'T'` 0x54 > `' '` 0x20), silently skewing the window.

The column is `TEXT` and cannot enforce this — **it is a loader-discipline invariant**, covered by a test
(see §6). FMP's `publishedDate` is already in `'YYYY-MM-DD HH:MM:SS'` shape but in **US Eastern**, not UTC
(see §7 Risk R1), so the loader must offset-convert before formatting.

---

## 3. Data-source decision (the central fork, resolved)

### 3.1 The options

**Option A — FMP stock-news API.** Fetch already-structured rows directly from FMP's stock-news endpoint
per ticker (`{symbol, publishedDate, title, text, url, site, image}`). No LLM needed for ingestion.
Pro: structured at the source, Pydantic-gateable, cheap (FMP API spend only), reliable schema, native
`url` for dedup and native `publishedDate` for the recency window. Con: depends on FMP coverage/quotas;
FMP relevance is noisy.

**Option B — LLM-structure the WebSearch markdown.** Keep the existing WebSearch news flow
(`generate_recent_developments` -> `call_llm_with_web`, [src/llm_client.py:1118+](src/llm_client.py),
rendered by [src/report/sections/recent_developments.py](src/report/sections/recent_developments.py),
cached as free markdown to `.tmp/news_cache/<TICKER>.json`), and add an Opus extraction module that parses
that prose into `{ticker, headline, url, published_at, snippet}` rows. Pro: reuses existing sourcing, Opus
gives high-quality extraction. Con: an Opus call per refresh per ticker (cost on top of the existing
WebSearch call); extraction errors; **and the fatal one — WebSearch prose rarely carries a precise,
parseable publish timestamp**, so populating `published_at` in the exact `'YYYY-MM-DD HH:MM:SS'` shape the
recency filter requires would mean *guessing* dates. That directly violates the trigger's core contract:
"Degrade, never fabricate" ([:11-17](src/triggers/material_news.py)).

**Option C — hybrid.** FMP primary; Opus-structuring of WebSearch markdown as a fallback for tickers FMP
doesn't cover.

### 3.2 Recommendation: **Option A**, with Opus applied to the trigger's existing classification

Reasoning, tied to project constraints:

1. **The contract needs precise, structured fields that FMP gives natively and WebSearch does not.** The
   sensor keys candidates on a stable integer `news_id`, dedups on the article, and filters on a
   `'YYYY-MM-DD HH:MM:SS'` UTC `published_at` within 24h. FMP returns `url` (stable dedup key) and
   `publishedDate` (precise, fixed-format) on every row. WebSearch markdown reliably gives neither.
   Fabricating a timestamp to satisfy the recency filter is the one thing the trigger forbids.
2. **Cost / billing.** Option A ingestion is pure FMP API spend — `FMP_API_KEY` is already wired
   ([execution/fetch_fmp_statements.py:58](execution/fetch_fmp_statements.py)) and the project runs in
   API-spend mode. Option B adds a *per-ticker Opus call every refresh* on top of the WebSearch call it
   already pays for — strictly more LLM spend for a strictly worse `published_at`.
3. **Reliability + house conventions.** FMP rows drop straight into the repo's existing fetch->validate->persist
   discipline (Pydantic pre-insert gate + schema-drift-halt dump,
   [fetch_fmp_statements.py:192-247](execution/fetch_fmp_statements.py)). WebSearch extraction has no schema
   guarantee and no clean drift signal.
4. **The materiality judgment — the irreducible LLM step — already lives in the trigger.** `scan()`
   classifies every headline in one batched, cached call and vetoes anything below the 0.6 relevance floor
   ([:487-545](src/triggers/material_news.py)). FMP's relevance noise is exactly what that veto exists to
   absorb. So the LLM belongs at *classification*, not ingestion.
5. **Coverage gaps are a no-regression.** For tickers FMP doesn't cover (recently-IPO'd, thin foreign
   names), the fetcher writes no rows and the trigger returns `[]` — identical to today's behavior. No
   regression; Option C's WebSearch fallback can be a later enhancement, not a v1 blocker.

### 3.3 Where Opus goes (honoring the explicit Opus instruction)

The project default model is **sonnet** (`DEFAULT_MODEL = "claude-sonnet-4-6"`,
[src/llm/cli.py:58](src/llm/cli.py)); the model is chosen per *purpose* via the `LLM_MODELS` table and
`_model_for(purpose)` ([:78-141](src/llm/cli.py)). The repo's Opus identifier is **`claude-opus-4-7`**,
already used by three purposes — `company_description`, `valuation_basis`, `saydo_importance`
([:99, :115, :118](src/llm/cli.py)).

Because Option A uses **no LLM for ingestion**, the only LLM module in the material-news path is the
trigger's existing materiality classification, called with `purpose="material_news_classification"`
([src/triggers/material_news.py:122](src/triggers/material_news.py), :359/:377). **That purpose is absent
from `LLM_MODELS`, so today it silently falls back to sonnet** via `_model_for`'s default branch
([src/llm/cli.py:127-141](src/llm/cli.py)). Per the explicit instruction that the LLM modules use the
Opus tier, the plan **registers it to Opus**:

```python
# add to LLM_MODELS in src/llm/cli.py
"material_news_classification": "claude-opus-4-7",
```

Use `claude-opus-4-7` to match the repo's three existing Opus entries (keep the pin uniform; bump all
together if the repo upgrades its Opus model). Note: the environment's newest Opus is `claude-opus-4-8`,
but consistency with the in-repo convention is the priority — do not introduce a one-off pin.

**Does a separate Opus "enrichment" step add value under Option A? No.** FMP's `title`/`text` already
populate `headline`/`snippet`; an Opus headline-rewrite would (a) cost an extra call, (b) risk drifting the
stored headline away from the source (a provenance problem — the alert quotes the headline verbatim,
[:452-462](src/triggers/material_news.py)), and (c) duplicate the materiality judgment the classifier
already makes. The correct and sufficient place for Opus is the classification call. No enrichment module.

---

## 4. Ingestion module

### 4.1 Pydantic validation gate — `FmpStockNewsRecord`

Added to [src/models/fmp_payloads.py](src/models/fmp_payloads.py), matching the house style there
(`model_config = ConfigDict(extra="ignore")`, FMP camelCase field names, no aliases, optional fields
default `None`):

```python
class FmpStockNewsRecord(BaseModel):
    """One record from FMP /api/v3/stock_news (or /stable/stock-news)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    publishedDate: str       # 'YYYY-MM-DD HH:MM:SS', US/Eastern at source (see Risk R1)
    title: str               # -> news.headline
    url: str                 # -> news.url  (confirm field name vs 'link' — Risk R2)
    text: str | None = None  # -> news.snippet
    site: str | None = None  # -> news.source
    image: str | None = None # unused; tolerated by extra="ignore"
```

This is used exactly like the statement fetcher's `_validate_response`
([fetch_fmp_statements.py:192-204](execution/fetch_fmp_statements.py)): validate `data[0]` before persist;
on `ValidationError`, dump the raw response to `.tmp/fmp_validation_failures/` and halt that ticker
(`schema_drift_halt`, :167-247) rather than caching a malformed shape.

### 4.2 Fetcher — `execution/fetch_fmp_news.py`

Mirrors `fetch_fmp_statements.py` for fetch/retry/validate, but **persists rows to the SQLite portfolio DB**
instead of JSON files. Design:

- **CLI flags** (mirror the trigger driver + statement fetcher so the stage covers the same ticker set):
  - `--tickers T1 T2 …` — default: active tracked tickers, resolved the same way the trigger driver does —
    `SELECT ticker FROM tracked_companies WHERE list_type IN ('portfolio','watchlist','evaluation')`
    ([execution/run_triggers.py:114-130](execution/run_triggers.py); `ACTIVE_LIST_TYPES_SQL` at
    [src/db.py:55-58](src/db.py)).
  - `--db-path PATH` — default `db.DB_PATH` (`data/portfolio.db`, [src/db.py:31](src/db.py)); same default
    resolution as `run_triggers.py` (:624-625).
  - `--days N` — lookback window for the FMP `from`/`to` params; default **2** (one day plus a safety
    margin over the trigger's 24h recency window, covering a missed morning and ET/UTC slop).
  - `--limit N` — max articles per ticker (default ~50; the trigger itself only ever reads the latest
    `_MAX_STORIES_PER_SCAN = 15`, :91).
- **Config / conventions** (copied from the statement fetcher):
  - `FMP_API_KEY = os.environ.get("FMP_API_KEY", "")`, `load_dotenv(ENV_PATH)`
    ([fetch_fmp_statements.py:57-58](execution/fetch_fmp_statements.py)); exit 1 with a logged error if unset
    (:287-289).
  - Base URL: **`https://financialmodelingprep.com/api/v3`** with endpoint `stock_news` (the documented,
    widely-used variant; see §7 R6 for the `/stable/stock-news` alternative). Request:
    `GET {BASE}/stock_news?tickers={T}&from={today-days}&to={today}&limit={N}&apikey=…`.
  - Retry/backoff helper identical to `_fetch_with_retry` — `RETRY_LIMIT = 3`, `RETRY_BACKOFF = 2.0`,
    fail-fast on 401/403, retry on 429/5xx, catch `requests.RequestException`
    ([:130-164](execution/fetch_fmp_statements.py)).
  - `ThreadPoolExecutor(max_workers=16)` across tickers ([:301-313](execution/fetch_fmp_statements.py)).
  - Structured JSON `_log(event, **kw)` to stderr (:121-123); exit 0 all-ok / 1 any-error (:315-322).
- **Transform (per validated record):**
  - `headline = title`, `url = url`, `snippet = text` (None/blank -> NULL), `source = site`,
    `source_feed = "fmp_stock_news"`, `fetched_at = now-UTC 'YYYY-MM-DD HH:MM:SS'`.
  - `published_at = to_utc(publishedDate)` formatted `"%Y-%m-%d %H:%M:%S"` — parse FMP's value as
    `America/New_York` (DST-aware via `zoneinfo`), convert to UTC, drop tzinfo. **This is the §2.3 / R1
    invariant.**
  - Skip + log any record missing `url` or `publishedDate` (can't dedup or place in time) — never
    fabricate. Pydantic makes both required, so this is a belt-and-suspenders guard.
- **Persist (idempotent upsert):** open `sqlite3.connect(db_path, timeout=30.0)` with
  `PRAGMA busy_timeout = 30000` (mirroring [src/db.py:65-75](src/db.py)), then per row:

  ```sql
  INSERT OR IGNORE INTO news
      (ticker, headline, url, published_at, snippet, source, source_feed, fetched_at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  ```

  relying on `UNIQUE (ticker, url)` for dedup — the repo's canonical idempotent-insert idiom
  (`INSERT OR IGNORE`, cf. [execution/categorize_ir_uploads.py:127-134](execution/categorize_ir_uploads.py)).
  `cur.rowcount` distinguishes inserted vs deduped for the log. Commit once per ticker.
- **Idempotency / cadence:** re-running the same morning inserts nothing new. The table is assumed
  pre-created by Alembic (executors never create schema — same assumption as every other loader); if the
  `news` table is absent the fetcher logs and exits non-zero rather than creating it inline.

No LLM is invoked anywhere in this module (Option A). Cost is bounded by `--limit × |tickers|` FMP calls.

---

## 5. Pipeline wiring

`run_morning_pipeline.py` runs three subprocess-isolated stages in order — triggers -> digest -> feed —
and **never aborts early** ([execution/run_morning_pipeline.py:10-26](execution/run_morning_pipeline.py)).
Add a **Stage 0 (news fetch) before the trigger stage** so the morning's fresh news is classified the same
run:

- New stage key `STAGE_NEWS = "stage_0_news"`, prepended to `_ALL_STAGE_KEYS` (:72) and to the stage list
  built in `_build_stages` (:124-172), ahead of `STAGE_TRIGGERS`.
- argv: `[sys.executable, str(exec_dir / "fetch_fmp_news.py"), *db_path_args]` — pass `--db-path` when set
  (so news lands in the same DB the trigger reads); tickers default to the active tracked set. The news
  fetcher takes neither `--user-id` nor `--max-cost-usd`, so it does **not** go through `_stage_args_for`
  (:108-121) unchanged — give it a bespoke 0-or-2-element db-path arg list.
- Timeout `_NEWS_TIMEOUT_S = 300` (5 min; the fetch is `|tickers|` HTTP calls fanned over a thread pool —
  fast, like the render stages at :65).
- **Skip semantics:**
  - Add `--skip-news` to skip Stage 0 while still running triggers (FMP down, or quota conservation).
  - `--skip-triggers` (the existing re-render-only path, :32-35/:134) should **also** skip news — there is
    no point fetching news when not classifying. So Stage 0 is built only when *neither* `--skip-triggers`
    nor `--skip-news` is set.
- **Resilience (already guaranteed):** `_run_stage` never raises and the loop never short-circuits
  (:175-227, :308-311). A failed/timed-out news fetch is logged loudly and the trigger stage still runs —
  the trigger then sees yesterday's rows (or none) and degrades to `[]`, identical to today. Exit code stays
  "count of failed stages" (:316-318).

**Cadence + cost bounds.** The pipeline is the daily cron entry. Stage 0 cost = FMP API spend only (no LLM).
The LLM cost is unchanged structurally — still one batched classification per ticker per run, cached in
`llm_artifacts` keyed on (ticker, sorted news ids, anchor sha) so same-morning re-runs are cache hits
([material_news.py:692-735](src/triggers/material_news.py)). The one delta: that call now runs on Opus
instead of sonnet (§3.3), so its per-call price rises; it remains bounded (≤15 headlines, one call, cached)
and is explicitly **not** gated by `--max-cost-usd` (scan-time cost is ungated by design,
[material_news.py:26-31](src/triggers/material_news.py)). Optionally add an `llm_budgets` row for
`material_news_classification` (table from migration 0052) to cap monthly Opus spend — deferred, not
required for v1.

---

## 6. Test strategy

All tests run against a temp SQLite DB and mock `requests.get` / `call_llm` — no live FMP or LLM calls
(mirrors [tests/test_trigger_material_news.py](tests/test_trigger_material_news.py) and the FMP fetcher
test conventions).

1. **Migration round-trip.** `0065_news` upgrade creates `news` with the six contract columns + bookkeeping;
   downgrade drops it; a second upgrade on an existing table is a no-op (inspector guard). Assert the six
   read columns exist with the exact names from `_NEWS_*`.
2. **Schema-matches-contract (the payoff guard).** Build the schema via the migration (not the inline test
   fixture), seed rows, and run `MaterialNewsTrigger.scan()` with `call_llm` mocked to return high-relevance
   scores — assert candidates are emitted. This proves the migration's table satisfies the sensor contract,
   not just a hand-written fixture. (Extends the existing scan test to also exercise the migrated schema.)
3. **Pydantic gate.** `FmpStockNewsRecord` validates a real FMP sample payload; a drifted shape (e.g. `link`
   instead of `url`, or a missing `publishedDate`) raises `ValidationError` and triggers the
   `schema_drift_halt` dump path.
4. **Fetch -> persist round-trip.** Mock `requests.get` to return a fixed FMP stock-news payload; run the
   fetcher against a temp DB; assert rows land with correct column mapping (`title`->`headline`,
   `text`->`snippet`, `site`->`source`, `source_feed='fmp_stock_news'`).
5. **Timezone normalization (R1 guard).** Given an FMP `publishedDate` known to be US/Eastern, assert the
   stored `published_at` equals the correct UTC `'YYYY-MM-DD HH:MM:SS'` (e.g. a winter EST `08:30:00` ->
   `13:30:00Z`; a summer EDT case for the DST boundary). Assert the format is exactly fixed-width, no `T`.
6. **Dedup / idempotency.** Run the fetcher twice over the same payload -> the second run inserts 0 rows
   (`UNIQUE (ticker, url)` + `INSERT OR IGNORE`). Separately, the same URL under two different tickers ->
   two rows (one per ticker), proving `(ticker, url)` (not `url` alone) is the right key.
7. **Empty / degraded paths.** FMP returns `[]` for a ticker -> no rows, no error, exit 0. A 401/403 ->
   logged `auth_error`, exit 1, no rows. Trigger over an empty or absent `news` table still returns `[]`
   (already covered by the existing suite; assert it still holds against the migrated empty table).
8. **Model registration.** `_model_for("material_news_classification")` returns `"claude-opus-4-7"` (guards
   the §3.3 one-liner against regression / accidental sonnet fallback).
9. **Pipeline wiring.** Stage 0 is present and ordered before triggers; `--skip-news` removes only Stage 0;
   `--skip-triggers` removes Stage 0 and Stage 1; a simulated news-stage failure does not stop the trigger
   stage (resilience contract).

---

## 7. Risks + open questions

- **R1 — `publishedDate` timezone (highest-impact).** FMP timestamps are widely reported as **US/Eastern**,
  not UTC; the trigger compares against `datetime.now(UTC)`. If the loader stores FMP's value unconverted,
  the 24h recency window is silently skewed ~4-5h and boundary stories are mis-included/excluded. **Mitigation:**
  normalize ET->UTC at ingestion (§4.2), test it (§6.5). **Open question (verify at build time):** fetch one
  article whose real publish time is known and confirm the source offset empirically — FMP's docs are
  inconsistent across endpoints and some are UTC; the conversion constant must be verified, not assumed.
- **R2 — FMP field name `url` vs `link`.** The stock-news endpoint returns `url` per the task spec and the
  v3 docs, but FMP's general-news / press-release endpoints use `link`/different shapes. **Mitigation:** the
  Pydantic gate (`url: str` required) halts immediately with a drift dump if the chosen endpoint returns
  `link`; confirm against a live sample and adjust the field (and the `->news.url` mapping) before merge.
- **R3 — FMP coverage gaps.** Recently-IPO'd, foreign, or thinly-covered tickers may return little or no
  news (cf. the project's note that FMP returns empty for most endpoints on recently-IPO'd names). The
  trigger degrades to `[]` for those — **no regression** vs today. Option C (WebSearch fallback) is the
  remedy if coverage proves inadequate; deferred.
- **R4 — FMP relevance noise + Opus cost.** FMP returns routine/low-signal stories. The trigger's 0.6
  materiality veto absorbs this, but every fetched story is fed to the (now Opus) classifier, so noisier
  feeds cost more per run. Bounded by `_MAX_STORIES_PER_SCAN = 15` and the artifact-store cache; consider an
  `llm_budgets` cap (deferred).
- **R5 — Syndication near-duplicates.** The same story republished under different URLs passes `(ticker,url)`
  uniqueness as distinct rows, so the classifier may judge both and two alerts may fire. Acceptable for v1;
  a fuzzy title/near-time dedup is a later enhancement. The `news_id` signature dedup
  ([material_news.py:561-569](src/triggers/material_news.py)) already prevents the *same* row firing twice.
- **R6 — `v3` vs `stable` base URL.** v3 `stock_news` is documented and matches the stated response shape;
  FMP also exposes `/stable/stock-news` (uses `symbols=` rather than `tickers=`), and other repo fetchers
  already use the `stable` base ([fetch_fmp_earnings_calendar.py:50](execution/fetch_fmp_earnings_calendar.py),
  [fetch_fmp_10q_json.py:45](execution/fetch_fmp_10q_json.py)). Pick one at build time; the Pydantic gate
  validates whichever is chosen. Recommendation: start on v3 `stock_news` (matches the task's documented
  shape); fall back to `stable` if v3 is deprecated on the account's plan.
- **R7 — No retention/pruning.** Rows accumulate indefinitely; the trigger only reads the last 24h so old
  rows are harmless but grow the table. A retention job (delete rows older than N days) is a deferred
  follow-up.
- **R8 — `published_at` format is loader-enforced, not column-enforced.** TEXT affinity can't guarantee the
  fixed-width UTC shape; a future second ingester (Option C) must honor the same format or the lexical
  compare breaks. Captured by the `source_feed` column (so a bad feed is traceable) and the §6.5 test.

---

## 8. Build sequence (ordered, each PR independently shippable)

1. **PR 1 — Migration `0065_news`.** Create the `news` table (§2.2) + the migration round-trip and
   schema-matches-contract tests (§6.1, §6.2). Ships **safe and inert**: the trigger's `_has_table` guard
   now passes, but with zero rows `scan()` still returns `[]` — no behavior change, and the schema is proven
   to match the sensor contract.
2. **PR 2 — Pydantic model + fetcher.** Add `FmpStockNewsRecord` to `fmp_payloads.py` and
   `execution/fetch_fmp_news.py` (§4) with tests §6.3-§6.7. After this PR, running the fetcher manually
   populates `news` and the trigger fires on the next driver run. No pipeline change yet.
3. **PR 3 — Opus registration.** Add `"material_news_classification": "claude-opus-4-7"` to `LLM_MODELS`
   ([src/llm/cli.py:78-124](src/llm/cli.py)) + test §6.8. One-line policy change; independent of PRs 1-2 but
   ordered here so the Opus tier is in place before the daily cron starts classifying automatically. Honors
   the explicit Opus instruction.
4. **PR 4 — Pipeline wiring.** Add Stage 0 (news fetch) before triggers in `run_morning_pipeline.py` with
   `--skip-news` and the `--skip-triggers`-implies-skip-news semantics (§5) + tests §6.9. After this PR the
   daily pipeline auto-populates news -> classifies on Opus -> fires alerts, all in one morning run.

End state: the material-news trigger is live — structured FMP news lands in `news` each morning, the Opus
classifier vetoes the noise, and material stories surface as alerts through the existing driver/digest/feed,
with **no edits to `material_news.py`**.
