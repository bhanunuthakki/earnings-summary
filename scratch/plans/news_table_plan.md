# Plan: `news` table + dual-source ingestion for the material-news trigger

> Status: PLAN FOR REVIEW — not implemented. This document is the only artifact in this branch.
> Author: planning session. Created 2026-05-30; revised 2026-05-30 to add an FMP-independent fallback
> ingestion pipeline (FMP is moving to a free/limited-API tier soon, so the design must not depend on FMP
> alone).
> Scope: design the structured `news` SQLite table and a **two-source** ingestion path that fills it, so the
> already-built `material_news` trigger ([src/triggers/material_news.py](src/triggers/material_news.py))
> produces real alerts and keeps producing them after FMP's API is throttled.

---

## 1. Summary

The material-news sensor is **fully built, wired into the driver, typed, and tested** — but **dormant**.
Its `scan()` reads a `news` table that no migration creates, so the `_has_table` guard short-circuits and
it returns `[]` forever ([src/triggers/material_news.py:225-233](src/triggers/material_news.py), docstring
:33-44). It is registered in `ENABLED_TRIGGERS` (registry.py:24-29) and its artifact purpose is already a
member of `FACT_DEPENDENT_PURPOSES` ([src/llm_artifact_store.py:330](src/llm_artifact_store.py)), so nothing
else in the trigger framework has to change.

**The fix, in two sentences:** Add an Alembic migration creating a `news` table whose six trigger-read
columns match the sensor's `_NEWS_*` constants exactly, plus a **two-source** ingestion path that writes
rows through one validated persistence layer — a **primary FMP stock-news fetcher** and an **FMP-independent
WebSearch+Opus fallback** — slotted into `run_morning_pipeline.py` as a new stage before the trigger stage
so each morning's fresh news is classified the same run. **No edits to `material_news.py` are required** —
the schema satisfies its existing contract verbatim.

**Why two sources (the new constraint):** FMP is moving to a free, rate-limited API tier. A single-source
FMP design would silently stop feeding the trigger the day the quota bites. So FMP is the *primary* feed
(structured, cheap, native timestamps/URLs) while it lasts, and a WebSearch-driven, Opus-structured feed is
the *fallback* — fully independent of FMP, reusing the repo's existing `call_llm_with_web` news
infrastructure. The fallback is also where an Opus *ingestion* LLM module legitimately belongs (turning
free-text news into structured rows), satisfying the explicit "Opus in the LLM modules" instruction.

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

| column         | runtime type required by `_load_recent_news`        | notes |
| :------------- | :-------------------------------------------------- | :---- |
| `id`           | `int` (else row skipped, :257-263)                  | the candidate key — `signature_key_evidence` hashes `news_id` alone (:561-569) |
| `ticker`       | `str` (filter param)                                 | per-ticker association column |
| `headline`     | `str` (else row skipped)                             | |
| `url`          | `str` (else row skipped)                             | dedup key (see 2.2) |
| `published_at` | `str` (else row skipped)                             | **must be UTC `'YYYY-MM-DD HH:MM:SS'`** — see 2.3 |
| `snippet`      | `str \| None` (kept only if non-empty str, :264)    | nullable |

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
adding columns is safe and invisible to the trigger. **Constant edits required: NONE.**

### 2.2 The migration — `alembic/versions/0065_news.py`

Head revision today is `0064_queued_actions` (revises `0063_alerts`)
([alembic/versions/0064_queued_actions.py:61-62](alembic/versions/0064_queued_actions.py)). Pattern copied
verbatim from `0063_alerts.py` (inspector-guarded idempotent upgrade/downgrade, `op.create_table` +
`op.create_index`):

```python
"""news — structured per-story news table feeding the material_news trigger.

The six trigger-read columns (id, ticker, headline, url, published_at, snippet)
match src/triggers/material_news.py:100-106 EXACTLY. The remaining columns are
ingestion bookkeeping; `source_feed` records WHICH ingester wrote the row
('fmp_stock_news' | 'websearch_opus') so the two feeds coexist and are auditable.
All bookkeeping columns are invisible to the trigger (its SELECT names columns).

published_at is stored as 'YYYY-MM-DD HH:MM:SS' in UTC (naive) so the sensor's
lexical `published_at >= ?` recency compare (_format_threshold, :210-216) is
chronological. The persistence layer (src/news/store.NewsRow) enforces that
format; the TEXT column cannot (see Risks).

Dedup: UNIQUE (ticker, url). One row per (ticker, article); a syndicated URL may
legitimately appear under two tickers, so url alone is NOT unique. Both feeds
rely on this for INSERT OR IGNORE idempotency.

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
        sa.Column("source", sa.Text(), nullable=True),         # publication, e.g. 'Reuters'
        sa.Column(
            "source_feed",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'fmp_stock_news'"),         # 'fmp_stock_news' | 'websearch_opus'
        ),
        sa.Column("fetched_at", sa.Text(), nullable=False),    # 'YYYY-MM-DD HH:MM:SS' UTC
        sa.UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),
    )
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

The trigger computes its threshold as `datetime.now(UTC).replace(tzinfo=None)` minus 24h, formatted
`"%Y-%m-%d %H:%M:%S"` ([:210-216](src/triggers/material_news.py), :235, :495), and compares **lexically**
(`published_at >= ?`). That is only chronologically correct if every stored value is **UTC (naive)** and
**exactly `'YYYY-MM-DD HH:MM:SS'`** — fixed width, space separator, no `T`, no zone suffix, no fractional
seconds. An ISO-8601 value (`2026-05-30T14:00:00`) sorts *after* a same-instant space value (`'T'` > `' '`),
silently skewing the window.

Both feeds produce timestamps from different sources (FMP's US-Eastern `publishedDate`; Opus-extracted dates
from web sources), so this invariant is enforced **in code at the persistence boundary** by a Pydantic
validator on `NewsRow` (§4.1) — not left to each loader. The TEXT column can't enforce it.

---

## 3. Data-source decision (the central fork, resolved)

### 3.1 The options

**Option A — FMP stock-news API.** Structured rows from FMP's stock-news endpoint per ticker
(`{symbol, publishedDate, title, text, url, site, image}`). No LLM for ingestion. Pro: structured at source,
Pydantic-gateable, cheap, native `url` (dedup) + native `publishedDate` (recency). Con: **depends on FMP
quota — which is about to become free/limited** — and FMP relevance is noisy.

**Option B — LLM-structure the WebSearch markdown.** Reuse the existing WebSearch news flow
(`generate_recent_developments` -> `call_llm_with_web`, [src/llm_client.py:1118-1206](src/llm_client.py),
rendered by [src/report/sections/recent_developments.py](src/report/sections/recent_developments.py)) and
add an Opus pass that emits structured `{ticker, headline, url, published_at, snippet}` rows. Pro:
**FMP-independent**, reuses existing sourcing, Opus extraction is high quality, and the existing prompt
*already* instructs per-item `[Source: outlet, YYYY-MM-DD, URL]` output
([src/llm_client.py:1188-1189](src/llm_client.py)) — so dates and URLs are already in scope. Con: an Opus
web call per ticker (cost); dates are best-effort (some stories carry no parseable date — handled by dropping
them, never fabricating); dedup is fuzzier than FMP's stable URLs.

**Option C — hybrid.** FMP primary; WebSearch+Opus fallback for the FMP-limited future and for tickers FMP
doesn't cover.

### 3.2 Recommendation: **Option C (hybrid)** — driven by the FMP-going-limited constraint

A single-source design is now ruled out: Option A breaks when FMP throttles; Option B pays an Opus web call
for every ticker every day even while a perfectly good cheap FMP feed exists. **Option C gets the best of
both:**

- **Primary — FMP** while its quota allows. Structured, cheap, native precise timestamps and stable URLs;
  drops straight into the repo's fetch->validate->persist discipline
  ([fetch_fmp_statements.py:192-247](execution/fetch_fmp_statements.py)).
- **Fallback — WebSearch+Opus**, fully FMP-independent. Triggered when FMP returns auth/quota failures
  (401/403/429), returns nothing for a ticker, or is disabled by config once FMP is known-limited. Reuses
  the proven `call_llm_with_web` infrastructure already in the repo.

Both feeds write through **one validated persistence layer** (`src/news/store.py`, §4.1), so the table
contract — especially the UTC timestamp format — is enforced once, regardless of source. `source_feed`
records provenance per row.

Why FMP stays primary rather than going Opus-only now: while FMP works it is materially cheaper (no LLM) and
gives guaranteed-precise timestamps; the trigger's 0.6 materiality veto ([:526](src/triggers/material_news.py))
already absorbs FMP's relevance noise. The fallback exists so the trigger never goes dark when FMP tightens.

**On the available FMP MCP server.** This environment exposes an FMP-style MCP (`mcp__…__news`, plus
statements/quote/secFilings/etc.). It is *not* the right basis for either pipeline path: (a) it is
interactively-authenticated and may be **absent in headless/cron runs** (the documented MCP caveat), and the
news ingestion runs under the morning cron; (b) it is the **same data source** FMP is limiting, so it is no
hedge against the quota change. The REST key path remains primary; the WebSearch+Opus path is the genuine
FMP-independent fallback. (The MCP is fine for *interactive* ad-hoc use by an analyst, just not for the
automated stage.)

### 3.3 Where Opus goes (honoring the explicit Opus instruction)

The project default model is **sonnet** (`DEFAULT_MODEL = "claude-sonnet-4-6"`, [src/llm/cli.py:58](src/llm/cli.py));
model is chosen per *purpose* via `LLM_MODELS` + `_model_for(purpose)` ([:78-141](src/llm/cli.py)). The repo's
Opus identifier is **`claude-opus-4-7`**, already used by `company_description`, `valuation_basis`,
`saydo_importance` ([:99, :115, :118](src/llm/cli.py)). Under Option C there are **two LLM modules, both on
Opus**:

1. **`material_news_classification`** — the trigger's materiality judgment
   ([material_news.py:122](src/triggers/material_news.py), :359/:377). It is **absent from `LLM_MODELS`, so
   today it silently falls back to sonnet** via `_model_for`'s default branch ([cli.py:127-141](src/llm/cli.py)).
   Register it: `"material_news_classification": "claude-opus-4-7"`.
2. **`news_structuring`** — the fallback's WebSearch->rows extraction (the genuine Opus *ingestion* module).
   Register it: `"news_structuring": "claude-opus-4-7"`.

**Plumbing note:** `call_llm` resolves model from purpose ([cli.py:428-435](src/llm/cli.py)), but
`call_llm_with_web` does **not** — it takes a positional `model=DEFAULT_MODEL` ([cli.py:448-449](src/llm/cli.py))
and passes `purpose` only to the ledger (see `generate_recent_developments`, which gets sonnet today,
[llm_client.py:1203](src/llm_client.py)). So the fallback must get Opus one of two ways:
- **(recommended)** make `call_llm_with_web`'s `model` parameter `str | None = None` and resolve via
  `_model_for(purpose)` when `None` — a backward-compatible change that centralizes model policy in
  `LLM_MODELS` (existing callers passing an explicit model are unaffected; `recent_developments` should be
  added to `LLM_MODELS` as `DEFAULT_MODEL` to keep it on sonnet and silence the unknown-purpose warning), or
- pass `model="claude-opus-4-7"` explicitly from the fallback fetcher.

Use `claude-opus-4-7` for both, matching the repo's existing Opus pins (keep the pin uniform; the
environment's newest Opus is `claude-opus-4-8`, but consistency with the in-repo convention wins — don't
introduce a one-off). No Opus *enrichment* step is added on the FMP path: FMP's `title`/`text` already fill
`headline`/`snippet`, and rewriting headlines would cost more, risk drifting the stored headline from source
(the alert quotes it verbatim, [:452-462](src/triggers/material_news.py)), and duplicate the classifier.

---

## 4. Ingestion modules

Structure (mirrors the repo's `src/alerts/store.py` + `execution/fetch_fmp_*.py` split):

```
src/news/store.py              # canonical NewsRow (Pydantic) + upsert_news_rows() — the ONE persist gate
src/models/fmp_payloads.py     # + FmpStockNewsRecord (FMP wire shape)
execution/fetch_fmp_news.py    # PRIMARY feed: FMP -> NewsRow -> store
execution/fetch_news_websearch.py  # FALLBACK feed: WebSearch+Opus -> NewsRow -> store
execution/fetch_news.py        # dispatcher: --source {fmp,websearch,auto}; pipeline calls this
```

### 4.1 The persistence gate — `src/news/store.py`

One canonical row type both feeds map into, and one idempotent writer. The Pydantic validator is where the
§2.3 UTC-format invariant becomes code-enforced.

```python
class NewsRow(BaseModel):
    """A validated news row ready to persist. The single contract gate for the
    `news` table — both the FMP and WebSearch+Opus feeds map into this."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    headline: str
    url: str
    published_at: str    # UTC 'YYYY-MM-DD HH:MM:SS' — validated below
    snippet: str | None = None
    source: str | None = None
    source_feed: str     # 'fmp_stock_news' | 'websearch_opus'

    @field_validator("published_at")
    @classmethod
    def _utc_fixed_format(cls, v: str) -> str:
        # Must parse EXACTLY as the trigger's lexical compare expects. Raises
        # (rejecting the row) rather than coercing — a bad timestamp is a feed
        # bug, not something to paper over.
        datetime.strptime(v, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 (naive UTC by contract)
        return v


def upsert_news_rows(conn: sqlite3.Connection, rows: Iterable[NewsRow]) -> tuple[int, int]:
    """INSERT OR IGNORE each row; returns (inserted, deduped). Idempotent on
    UNIQUE (ticker, url). `fetched_at` stamped here in UTC at write time."""
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r.ticker, r.headline, r.url, r.published_at, r.snippet, r.source, r.source_feed, fetched_at),
        )
        inserted += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    return inserted, 0  # deduped = len(rows) - inserted at the call site
```

`INSERT OR IGNORE` on `UNIQUE (ticker, url)` is the repo's canonical idempotent idiom (cf.
[execution/categorize_ir_uploads.py:127-134](execution/categorize_ir_uploads.py)). The table is assumed
pre-created by Alembic (executors never create schema); if absent, callers log and exit non-zero.

### 4.2 Primary feed — `execution/fetch_fmp_news.py`

Mirrors `fetch_fmp_statements.py` for fetch/retry/validate, but maps to `NewsRow` and persists via
`upsert_news_rows`.

- **CLI:** `--tickers` (default: active tracked tickers — `SELECT ticker FROM tracked_companies WHERE
  list_type IN ('portfolio','watchlist','evaluation')`, the exact set the trigger driver scans,
  [run_triggers.py:114-130](execution/run_triggers.py); `ACTIVE_LIST_TYPES_SQL` at [db.py:55-58](src/db.py));
  `--db-path` (default `db.DB_PATH`, [db.py:31](src/db.py)); `--days` (FMP `from`/`to` window, default 2 —
  margin over the 24h recency window); `--limit` (per-ticker cap, default ~50; the trigger reads only the
  latest `_MAX_STORIES_PER_SCAN=15`, [material_news.py:91](src/triggers/material_news.py)).
- **Fetch:** `FMP_API_KEY` via `load_dotenv` ([fetch_fmp_statements.py:57-58](execution/fetch_fmp_statements.py));
  **use the `stable` base, NOT `api/v3`** — FMP deprecated all `/api/v3/*` endpoints on **2025-08-31** and
  now returns `403 "Legacy Endpoint : ... only available for legacy users who have valid subscriptions prior
  August 31, 2025"` to any non-legacy (incl. free) account (confirmed, see Risk R6). The repo already uses the
  `stable` base for other fetchers ([fetch_fmp_earnings_calendar.py:50](execution/fetch_fmp_earnings_calendar.py),
  [fetch_fmp_10q_json.py:45](execution/fetch_fmp_10q_json.py)). Per-ticker request:
  `GET https://financialmodelingprep.com/stable/news/stock?symbols={T}&from=&to=&limit=&page=0&apikey=`
  (note `symbols=` plural, `stable/news/stock`). Use the `_fetch_with_retry` helper verbatim (RETRY_LIMIT=3,
  RETRY_BACKOFF=2.0, fail-fast on auth, retry 429/5xx, [:130-164](execution/fetch_fmp_statements.py));
  `ThreadPoolExecutor(max_workers=16)`.
- **Validate:** `FmpStockNewsRecord.model_validate(data[0])` before mapping; on `ValidationError`,
  `schema_drift_halt` dump to `.tmp/fmp_validation_failures/` ([:167-204](execution/fetch_fmp_statements.py)).
  The `stable` shape returns the same core fields as the old v3 `stock_news` plus a `publisher` field;
  `extra="ignore"` tolerates additions, and the gate halts loudly if a *required* field drifts — confirm the
  exact shape with the one-shot build-time probe (Risk R2/R3).

  ```python
  class FmpStockNewsRecord(BaseModel):   # in src/models/fmp_payloads.py, house style
      model_config = ConfigDict(extra="ignore")
      symbol: str
      publishedDate: str       # 'YYYY-MM-DD HH:MM:SS', US/Eastern at source (Risk R1)
      title: str               # -> headline
      url: str                 # -> url   (confirm field name via probe — Risk R2; gate halts on drift)
      text: str | None = None  # -> snippet
      site: str | None = None  # -> source
      publisher: str | None = None  # stable adds this; unused but documents the shape
      image: str | None = None
  ```
- **Map -> NewsRow:** `headline=title`, `url=url`, `snippet=text or None`, `source=site`,
  `source_feed="fmp_stock_news"`, `published_at = to_utc(publishedDate)` — parse as `America/New_York`
  (DST-aware `zoneinfo`), convert to UTC, format `"%Y-%m-%d %H:%M:%S"` (the §2.3 / R1 invariant). `NewsRow`'s
  validator is the backstop.
- **Persist:** `upsert_news_rows`. Structured JSON logs; exit 0 all-ok / 1 any-error. **Crucially, the
  per-ticker FMP result (ok / auth-or-quota-failure / empty) is returned to the dispatcher** so `auto` mode
  can fall back per ticker.
- No LLM on this path.

### 4.3 Fallback feed — `execution/fetch_news_websearch.py`

FMP-independent. One Opus `call_llm_with_web` call per ticker that searches recent news **and returns
structured JSON rows directly** — adapted from the proven `generate_recent_developments` prompt
([llm_client.py:1147-1201](src/llm_client.py)), which already targets Bloomberg/Reuters/CNBC/FT/WSJ + press
releases, caps the web budget (≤2 searches, ≤N fetches), and emits per-item outlet/date/URL.

- **New generator** (in `src/llm_client.py`, e.g. `structure_recent_news_json(ticker, news_days, anchor_block)`)
  that asks for **JSON only**:
  ```json
  [{"headline": "...", "url": "https://...", "published_at": "YYYY-MM-DD HH:MM:SS",
    "published_tz": "UTC|ET|...", "snippet": "...", "source": "Reuters"}]
  ```
  Hard rules in the prompt: return UTC where determinable; **if you cannot determine a publication date for
  an item from its source, OMIT that item — do not guess** (preserves the trigger's "degrade, never
  fabricate" contract). Reuse the JSON-fence stripping + one-shot retry discipline the trigger already uses
  ([material_news.py:324-387](src/triggers/material_news.py)).
- **Model:** `purpose="news_structuring"` -> Opus (§3.3). Call via `call_llm_with_web` (web tools needed); see
  the §3.3 plumbing note — either the purpose-resolution enhancement or an explicit `model` arg.
- **Map -> NewsRow:** normalize each item's `published_at` to UTC `'YYYY-MM-DD HH:MM:SS'` (convert from
  `published_tz` if not UTC); drop items missing url/headline/date; `source_feed="websearch_opus"`.
  `NewsRow`'s validator rejects any malformed timestamp.
- **Cache (cost bound):** cache the structured JSON in `llm_artifacts` (purpose `news_structuring`, keyed on
  (ticker, UTC date, anchor sha)) via the same `llm_artifact_store` API the trigger uses
  ([material_news.py:692-735](src/triggers/material_news.py)), so same-day re-runs are cache hits and don't
  re-call Opus.
- **Degrade:** on any LLM/web failure, log and return `[]` for that ticker (no rows) — same philosophy as the
  trigger.

### 4.4 Dispatcher — `execution/fetch_news.py`

The single entrypoint the pipeline calls. `--source {fmp,websearch,auto}` (default `auto`), plus the shared
`--tickers`/`--db-path`/`--days`/`--limit`.

- `fmp` — run only the FMP feed.
- `websearch` — run only the WebSearch+Opus feed (the setting once FMP is fully limited).
- `auto` (default) — run FMP first; for any ticker where FMP **refused or returned nothing**, run the
  WebSearch+Opus feed for that ticker. Degrades gracefully as FMP tightens: while FMP works the Opus path
  almost never runs (near-zero LLM cost); as FMP starts refusing, the fallback transparently picks up the
  slack — and if free-tier FMP refuses news for *every* ticker, the fallback silently becomes the de-facto
  primary, with no code change.
- Both feeds open one shared connection and write through `upsert_news_rows`; `(ticker, url)` dedup means a
  story seen by both feeds is stored once.

**The refusal predicate (the R3 resolution).** FMP signals "no data for you" in several ways, and — the key
gotcha — *not always with a 4xx status*. The dispatcher treats a per-ticker FMP result as **refused** (=>
fall back) when ANY of:

```python
def _fmp_refused(status: int, body: object) -> bool:
    # 401 bad key · 402 Payment Required · 403 legacy/plan-gated · 429 quota/rate · 5xx after retries
    if status in (401, 402, 403, 429) or status >= 500:
        return True
    # The silent gotcha: HTTP 200 but the body is NOT the expected JSON array —
    # FMP delivers quota/plan messages as a 200 with {"Error Message": "..."} (a dict),
    # or an empty/None body. The statement fetcher already guards this exact shape
    # (`if not isinstance(data, list)`, fetch_fmp_statements.py:152-153, 229-230).
    if not isinstance(body, list):
        return True
    return False
```

A 200 with an **empty list** `[]` is *not* a refusal — it is a genuine "no news in window" and must NOT
trigger the (costly) fallback. The distinction: `[]` (empty array) = no news; `{"Error Message": ...}` or
non-list = refused. This predicate is **source-policy-agnostic**: it does not matter whether FMP withholds
news by plan-gating, quota, or legacy-deprecation — every refusal mode lands in one of the branches above and
routes to the fallback. That is why the design does not need to know FMP's exact free-tier news policy in
advance (see Risk R3 for the one-shot probe that sets the *default* `--news-source`).

---

## 5. Pipeline wiring

`run_morning_pipeline.py` runs three subprocess-isolated stages in order — triggers -> digest -> feed — and
**never aborts early** ([execution/run_morning_pipeline.py:10-26](execution/run_morning_pipeline.py)). Add a
**Stage 0 (news fetch) before the trigger stage**:

- `STAGE_NEWS = "stage_0_news"`, prepended to `_ALL_STAGE_KEYS` (:72) and to `_build_stages` (:124-172),
  ahead of `STAGE_TRIGGERS`.
- argv: `[sys.executable, str(exec_dir / "fetch_news.py"), "--source", args.news_source, *db_path_args]` —
  pass `--db-path` when set so news lands in the DB the trigger reads. The news fetcher takes neither
  `--user-id` nor `--max-cost-usd`, so it does not use `_stage_args_for` unchanged (:108-121).
- New pipeline flag `--news-source {fmp,websearch,auto}` (default `auto`) forwarded to the dispatcher, so the
  operator can flip to `websearch` the day FMP is cut off, without code changes.
- Timeout: `_NEWS_TIMEOUT_S = 600` — the FMP path is fast, but an `auto`/`websearch` run may make an Opus web
  call per fallback ticker, so allow more headroom than the render stages (:65).
- **Skip semantics:** `--skip-news` skips Stage 0; `--skip-triggers` (the re-render-only path, :32-35/:134)
  also skips news (no point fetching when not classifying). Stage 0 is built only when neither flag is set.
- **Resilience (already guaranteed):** `_run_stage` never raises and the loop never short-circuits
  (:175-227, :308-311) — a failed news stage is logged loudly and triggers still run (seeing prior rows or
  none, degrading to `[]`, as today). Exit code stays "count of failed stages" (:316-318).

**Cadence + cost bounds.** FMP path = API spend only. Fallback path = one Opus `call_llm_with_web` per
fallback ticker per day, cached in `llm_artifacts` (so same-day re-runs are free) — in `auto` mode this is
~0 while FMP is healthy and ramps only as FMP refuses. The trigger's classification cost is structurally
unchanged (one batched call/ticker/run, cached) but now runs on Opus (§3.3) — bounded (≤15 headlines) and,
by design, **not** gated by `--max-cost-usd` ([material_news.py:26-31](src/triggers/material_news.py)).
Optionally add `llm_budgets` rows (table from migration 0052) for `material_news_classification` and
`news_structuring` to cap monthly Opus spend — deferred, not required for v1.

---

## 6. Test strategy

All tests use a temp SQLite DB and mock `requests.get` / `call_llm` / `call_llm_with_web` — no live FMP or
LLM calls (mirrors [tests/test_trigger_material_news.py](tests/test_trigger_material_news.py) and the FMP
fetcher test conventions).

1. **Migration round-trip.** `0065_news` upgrade creates `news` with the six contract columns + bookkeeping;
   downgrade drops it; a second upgrade on an existing table is a no-op (inspector guard).
2. **Schema-matches-contract (payoff guard).** Build the schema via the migration (not the inline fixture),
   seed rows, run `MaterialNewsTrigger.scan()` with `call_llm` mocked high-relevance -> assert candidates
   emitted. Proves the migration satisfies the sensor contract.
3. **`NewsRow` validator.** Accepts `'2026-05-30 14:00:00'`; rejects ISO-with-`T`, fractional seconds, empty,
   and a tz-suffixed value. This is the §2.3 invariant guard, source-agnostic.
4. **`upsert_news_rows` dedup/idempotency.** Same (ticker, url) twice -> one row, `inserted` counts correctly;
   same url under two tickers -> two rows (proves `(ticker, url)` is the key).
5. **FMP feed.** `FmpStockNewsRecord` validates a real sample; a drifted shape (`link` not `url`, or missing
   `publishedDate`) raises -> `schema_drift_halt`. Fetch->map->persist round-trip (mocked `requests.get`) lands
   correctly-mapped rows with `source_feed='fmp_stock_news'`. **ET->UTC normalization** test (winter EST
   `08:30` -> `13:30Z`; a summer EDT case for the DST boundary).
6. **WebSearch+Opus feed.** Mock `call_llm_with_web` to return a JSON array -> rows persisted with
   `source_feed='websearch_opus'`. An item with **no date is dropped** (not stored with `now()`). Malformed
   JSON -> retry-then-`[]`. Cache hit on a second same-day run makes no second `call_llm_with_web` call.
7. **Dispatcher `auto` + the `_fmp_refused` predicate.** Table-driven: `403` (legacy), `402`, `429`, `500`,
   and a **`200` with a `{"Error Message": ...}` (non-array) body** all -> `refused=True` -> the WebSearch+Opus
   feed runs for that ticker; a **`200` with `[]`** (genuine no-news) -> `refused=False` -> the fallback does
   NOT run (guards against burning Opus on quiet tickers); a `200` with a populated array -> FMP rows
   persisted, no fallback. `--source websearch` never calls FMP; `--source fmp` never calls Opus.
8. **Model registration / plumbing.** `_model_for("material_news_classification")` and
   `_model_for("news_structuring")` both return `"claude-opus-4-7"`; if the `call_llm_with_web` resolution
   enhancement is taken, a purpose with an Opus pin resolves to Opus when `model` is omitted.
9. **Empty / degraded.** FMP `[]` and WebSearch `[]` both -> no rows, no crash. Trigger over an empty/absent
   `news` table still returns `[]`.
10. **Pipeline wiring.** Stage 0 present and ordered before triggers; `--skip-news` removes only Stage 0;
    `--skip-triggers` removes Stage 0 + Stage 1; `--news-source` forwarded; a simulated news-stage failure
    does not stop triggers.

---

## 7. Risks + open questions

- **R1 — `publishedDate` timezone (highest-impact).** FMP timestamps are widely reported as **US/Eastern**,
  not UTC; the trigger compares against `datetime.now(UTC)`. Unconverted, the 24h window skews ~4-5h.
  **Mitigation:** ET->UTC at ingestion (§4.2) + the `NewsRow` validator + the §6.5 test. **Verify at build
  time:** fetch one article with a known real publish time and confirm the offset empirically — FMP docs are
  inconsistent across endpoints.
- **R2 — `stable/news/stock` exact field names.** The `stable` shape returns the v3 core fields plus
  `publisher` (`symbol, publishedDate, title, text, url, site, publisher, image`), but the field set could
  not be confirmed from a fetchable source (FMP's docs 403 to automated fetch). **Mitigation:** the Pydantic
  gate (`url`/`title`/`publishedDate`/`symbol` required, `extra="ignore"`) halts with a drift dump if a
  required field is renamed, so a wrong assumption fails loud, not silent. Resolve definitively with the R3
  probe.
- **R3 — FMP refusal detection (RESOLVED in design; one probe remains).** The reason the fallback exists.
  Detection is handled generically by the `_fmp_refused` predicate (§4.4): 401/402/403/429/5xx **and** the
  HTTP-200-with-non-array-body gotcha (FMP delivers quota/plan messages as a 200 `{"Error Message": ...}`),
  while a genuine empty array `[]` is NOT treated as refusal. This is source-policy-agnostic, so the design
  does not depend on knowing FMP's free-tier news policy ahead of time. **One build-time action remains** (not
  a blocker, sets a default): with the *actual free-tier key*, issue one
  `GET /stable/news/stock?symbols=AAPL&apikey=…` and record (status, body) to learn (a) whether news is
  included on free FMP or returns 402/403, and (b) the precise error-body shape. If news is plan-gated on
  free, set the pipeline default to `--news-source websearch` (skip the wasted FMP round-trip); if it is
  free-but-quota-limited, keep `--news-source auto`. Either way the trigger keeps eating; this only tunes
  cost.
- **R4 — Fallback Opus cost + web reliability.** The WebSearch+Opus path costs an Opus web call per fallback
  ticker. Bounded by the `auto` gating (runs only on FMP miss), the `llm_artifacts` same-day cache, and an
  optional `llm_budgets` cap. Web extraction quality varies; the "drop items without a confident date" rule
  trades recall for never fabricating timestamps.
- **R5 — Dedup across feeds / syndication.** `(ticker, url)` dedups exact URLs, including a story seen by
  both feeds. Near-duplicate syndications with different URLs still produce separate rows; the trigger's
  `news_id` signature dedup ([material_news.py:561-569](src/triggers/material_news.py)) still prevents the
  same row firing twice. Fuzzy title/near-time dedup is a deferred enhancement.
- **R6 — v3 is dead; use `stable` (RESOLVED).** FMP deprecated all `/api/v3/*` endpoints on **2025-08-31**;
  they now return `403 "Legacy Endpoint : ... only available for legacy users who have valid subscriptions
  prior August 31, 2025"` to non-legacy accounts (confirmed via a third-party report of `/api/v3/sec_filings`
  returning exactly this). Since the account is moving to a *fresh free* tier (not a pre-Aug-2025 legacy
  subscription), v3 `stock_news` would 403 for every call. **Decision:** use the `stable` per-ticker endpoint
  `GET /stable/news/stock?symbols={T}` — consistent with the repo's other `stable` fetchers
  ([fetch_fmp_earnings_calendar.py:50](execution/fetch_fmp_earnings_calendar.py),
  [fetch_fmp_10q_json.py:45](execution/fetch_fmp_10q_json.py)). (Note: legacy v3 deprecation is *also* why the
  cli.py "stale FMP news pre-pull" comment is historical — any old v3 news wiring would now 403.)
- **R7 — `call_llm_with_web` model resolution.** It doesn't resolve model from purpose today (§3.3). The
  recommended enhancement is backward-compatible but touches a shared function; the explicit-`model`-arg
  option avoids that if a tighter blast radius is preferred.
- **R8 — MCP not for cron.** The available FMP MCP is interactively-authenticated and may be missing in
  headless runs; not used by either pipeline path (§3.2).
- **R9 — No retention/pruning.** Rows accumulate; the trigger only reads the last 24h so old rows are
  harmless but grow the table. A retention job (delete rows older than N days) is a deferred follow-up.

---

## 8. Build sequence (ordered, each PR independently shippable)

1. **PR 1 — Migration `0065_news`.** Create the `news` table (§2.2) + migration round-trip and
   schema-matches-contract tests (§6.1-§6.2). Ships **safe and inert**: the trigger's `_has_table` guard now
   passes, but with zero rows `scan()` still returns `[]` — no behavior change.
2. **PR 2 — Persistence gate `src/news/store.py`.** `NewsRow` (with the UTC-format validator) +
   `upsert_news_rows` + tests §6.3-§6.4. Pure persistence layer; no fetching yet. Consumed by PRs 4-5.
3. **PR 3 — Opus model pins.** Register `material_news_classification` and `news_structuring` ->
   `claude-opus-4-7` in `LLM_MODELS`, and (recommended) the `call_llm_with_web` purpose-resolution
   enhancement + `recent_developments` pinned to `DEFAULT_MODEL` (§3.3) + tests §6.8. Tiny and independent;
   lands early so the trigger and both feeds use Opus from day one. (An unused `news_structuring` entry until
   PR 5 is harmless.)
4. **PR 4 — Primary FMP feed.** First, the **one-shot probe** (Risk R3): hit
   `GET /stable/news/stock?symbols=AAPL&apikey=…` with the actual free-tier key, record (status, body), and
   set the pipeline default `--news-source` accordingly. Then `FmpStockNewsRecord` (fmp_payloads.py) +
   `execution/fetch_fmp_news.py` against the **`stable`** endpoint (NOT v3 — R6) -> `NewsRow` -> store,
   returning the per-ticker (status, body) so the dispatcher's `_fmp_refused` can decide + tests §6.5. After
   this PR, running it manually populates `news` (or cleanly reports FMP refusal) and the trigger fires.
5. **PR 5 — Fallback feed + dispatcher.** `structure_recent_news_json` (llm_client.py, Opus/web, JSON +
   date-drop + `llm_artifacts` cache), `execution/fetch_news_websearch.py` -> `NewsRow` -> store, and
   `execution/fetch_news.py --source {fmp,websearch,auto}` with the `_fmp_refused` predicate (§4.4) + tests
   §6.6-§6.7. After this PR the trigger keeps eating even when FMP refuses every call.
6. **PR 6 — Pipeline wiring.** Stage 0 (`fetch_news.py --source auto`) before triggers in
   `run_morning_pipeline.py`, `--news-source` flag, `--skip-news` + `--skip-triggers`-implies-skip-news, and
   the resilience guarantee + tests §6.10. After this PR the daily pipeline auto-populates news -> classifies
   on Opus -> fires alerts, FMP-up or FMP-down.

End state: the material-news trigger is live and **resilient to FMP's tier change** — FMP feeds it cheaply
while the quota lasts, the WebSearch+Opus fallback covers the gaps and the limited-FMP future, the Opus
classifier vetoes the noise, and material stories surface as alerts through the existing
driver/digest/feed — with **no edits to `material_news.py`**.
