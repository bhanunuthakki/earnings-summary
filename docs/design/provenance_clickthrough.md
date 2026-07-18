# Provenance Click-Throughs — Design

Status: proposed. Author: agent design pass, 2026-07-17. No code changes accompany this
document; it is the spec an implementation agent executes against, phase by phase.

**P0 mandate**: clicking a number's source must land on the actual evidence — the IR
slide, the 10-K/10-Q table cell, the transcript quote — not an opaque tier label. New
extractions must not be able to land without a renderable locator.

---

## 0. Ground truth: what exists today (read before implementing)

This is not a greenfield problem. The substrate is roughly 60% built; the gap is
narrower and more specific than "add provenance" — it is **"finish the locator
schema, wire it to something that renders, and stop letting new facts skip it."**

### 0.1 `FactLocator` already exists (alembic 0075, `src/models/facts.py`)

```python
class FactLocator(BaseModel):
    section: str | None = None          # 10-K/10-Q section key
    transcript_line: int | None = None  # transcript_segments line id
    pdf_page: int | None = None         # 1-based PDF page (IR decks)
    json_path: str | None = None        # FMP record pointer, e.g. "[3].netIncome"
```

Serialized via `.to_json()` (all-empty → `None`, never `"{}"`) into the nullable
`financial_facts.locator` / `kpi_facts.locator` TEXT(JSON) columns. Documented in
`directives/data_provenance.md` §7. `segment_quarterly_framework.md` §4.1 has already
committed `segment_dimensions.locator` to this **same** shape ("do not invent a second
locator shape for segments") — so the schema below is additive to `FactLocator`, not a
replacement, and segments inherit it for free.

**Every field today is a bare scalar with no cell/row/column identity and no
captured value-in-situ.** `section="Income Taxes"` tells a reader which 10-K note to
open, not which row or column inside it. `pdf_page=7` says which page, nothing about
where on it. This is exactly the "opaque label" the owner is describing — the schema
already gestures at the right shape, but stops one hop short of "renderable."

### 0.2 Who writes a locator today, and what's actually in it

| writer | locator written | source_excerpt |
|---|---|---|
| `compute/income_statement.py` / `balance_sheet.py` / `cashflow.py` (`compute/_common.py::extract_facts_with_spec`) | `json_path="[<i>].<fmpField>"` | none — `financial_facts` has no excerpt column |
| `compute/as_reported.py` | `json_path="[<i>].data.<xbrl_tag>"` | none |
| `pipeline/sec_xbrl.py` (companyfacts) | `json_path="facts.<ns>.<tag>.units.<unit>[<i>]"` | none |
| `table_extractors/generic_xbrl_capture.py` (Stage A, capture-every-number) | `FactLocator(section=section_key)` **only** — drops the row label, axis path, and column header it already has in hand (`XbrlRow.label`, `.axis_path`, `.period_labels[col]` from `table_extractors/base.py::iter_xbrl_table`) | yes — `"{label}: {raw} [{currency} {scale}]"`, clipped 1024 chars, but not linked to the locator |
| `compute/kpi_extract_summaries.py` (LLM over quarterly transcript summaries) | none — source is a derived summary doc | yes — LLM-returned verbatim snippet |
| `execution/extract_kpis_from_ir.py` (manual/in-session IR PDF readout) | `pdf_page` via manifest JSON | yes, via manifest `source_excerpt` |
| `compute/fmp_derived_kpis.py` (derived: margins, ratios, YoY, ROE) | none by design — see `computed_from` below | n/a |
| `ir_pipeline/ingest.py` (IR spreadsheet) | none — cells have no JSON/PDF position | n/a |
| `compute/s1_financials.py` | none — no stable line anchor | n/a |
| `execution/extract_8k_overrides.py` / `src/provenance/edgar_8k.py` (8-K exhibit → `fact_overrides`) | **none** — `fact_overrides` (alembic 0111) has `source_excerpt` but no locator column at all | yes |
| `execution/extract_commitments_from_transcript.py` / alert evidence (`src/dashboard/evidence_drawer.py`) | a **different, unrelated** freeform `locator: str` on alert citations (composed from `{period, line_number}` for `earnings_tone`) — do not confuse with `FactLocator`; flagged in §1.6 as a naming collision to resolve, not extend | n/a |

`kpi_facts.computed_from` (alembic 0087, `directives/data_provenance.md` §9) is a
second, separate JSON column for **derived** KPI lineage: `{"display": "...",
"inputs": [{"ref", "item", "period_end", "doc_id", "tier"}, ...]}`. This is the
`derived` locator's ancestor — `docs/design/bottoms_up_metrics_engine.md` §3 extends it
with `formula_id`, `method_flags`, and `metric_computation_attempts` for the
not-computable case. The schema in §1 folds `computed_from` into the locator
vocabulary as the `derived` variant rather than leaving it a third parallel shape.

### 0.3 Where it dead-ends today — the "FMP XYZ" click-through

`src/ui/source_chip.py::viewer_href()` is the one function that turns a locator into a
link, and it only handles two of the four cases:

```python
if isinstance(line, int):          suffix = f"#L{line}"                     # transcript_line
elif isinstance(section, str):     suffix = f"?section=..."                 # section
# pdf_page and json_path: NO branch — no link is ever built for them
```

`json_path` and `pdf_page` locators render as a raw JSON string in the popover
(`.src-pop-locator`, `_esc(src.locator)` — literally `{"json_path":"[3].revenue"}`
shown as text) with **zero click-through**. This is the concrete "clicking further
gives no UX-friendly view" the owner is naming. Two separate reasons, one per type:

- **`json_path` (FMP statement facts — by far the highest-volume writer)**: the target
  file (`data/historical/fmp/{T}_income_statement_quarterly.json` etc.) is never
  served by `/source/<doc_id>` at all. `pipeline/source_viewers.py` only knows two
  doc-type families: `_TRANSCRIPT_DOC_TYPES` and `_FORM_JSON_DOC_TYPES =
  {fmp_10k_json, fmp_10q_json}`. A plain FMP statement endpoint dump falls through to
  `render_fallback_page` — a metadata card with an outbound link, not the table.
- **`pdf_page` (IR-deck KPIs)**: there is no PDF-rendering route in the app at all.
  `pypdf` and, for the heavy-deck fast path, `fitz` (PyMuPDF) are already dependencies
  (`src/ir_uploads.py::_fingerprint_pdf_pymupdf`, used today only for first-page
  fingerprinting during upload triage) — **PyMuPDF's `page.get_pixmap()` is exactly
  the primitive needed for page-image rendering and requires no new dependency.**

### 0.4 What already renders well (reuse, don't replace)

- `pipeline/source_viewers.py::render_transcript_page` — numbered-line transcript with
  `id="L<n>"` anchors; `#L<n>` deep-links and highlights via `:target` CSS. This is the
  `transcript_span` renderer, essentially done.
- `render_form10k_page` — parsed 10-K/10-Q JSON with a section nav (`?section=`); a
  generic label/value row renderer (`_render_section`) tolerant of FMP's list-of-dicts
  shape. This is 80% of the `fmp_json_table` renderer — it needs cell-level highlight
  and the FMP *statement-endpoint* doc types added to its dispatch, not a rewrite.
- The peek primitive (`src/pipeline/peeks.py`, dispatched at `/api/peek/*` in
  `execution/comments_server.py`) is the correct delivery vehicle end-to-end: fetch →
  inject into a positioned popover, `?fragment=1` gives every source-viewer page a
  chrome-less variant for exactly this purpose, `VIEWER_CONTENT_CSS` is shared so a
  fragment injected into the shell renders identically to the full page. **Nothing new
  needs inventing here — §2 is almost entirely "route a new fragment through the
  existing dispatcher."**
- `source_chip.py::_lineage_rows` already renders one flat level of `computed_from`
  with per-input tier-colored mini-chips linking `/source/<doc_id>`. The `derived`
  locator in §1 makes this **recursive** (an input can itself be `derived`) and gives
  it a dedicated peek instead of a popover row list.

### 0.5 Biggest retrofit risk (headline finding)

**Volume is inverted relative to renderer readiness.** The highest-volume writer
(`json_path` — every FMP statement fact, i.e. most of `financial_facts`) points at a
document family (`fmp_income_statement_*`, `fmp_balance_sheet_*`, ...) that has **no
viewer at all** today, not even a section-level one — building `fmp_json_table`
rendering for these plain array-of-records JSON files (vs. the already-solved
section-keyed 10-K/10-Q JSON) is genuinely new work, not a retrofit of an existing
page. Meanwhile `pdf_page` (IR decks) is comparatively low-volume but needs a wholly
new rendering *capability* (page-to-image) the repo has never shipped. Phase A
therefore targets the high-volume-but-structurally-simple case first (§7).

---

## 1. Canonical locator schema

### 1.1 Design principle

One typed, versioned Pydantic model. `FactLocator` is **extended in place** (new
optional fields + a discriminated `kind` + `locator_version`), not replaced — every
existing row with a scalar-only locator (`{"section": "..."}`,
`{"pdf_page": 7}`) is a valid `locator_version=1` document; new writers emit
`locator_version=2` documents with a `kind` tag and richer per-kind payloads. A
reader that seees no `locator_version` key treats the row as version 1 (today's
shape) — no migration, no backfill required to keep old rows valid.

### 1.2 The model (`src/models/facts.py`, extending `FactLocator`)

```python
class LocatorKind(StrEnum):
    """Discriminant for a v2 locator. Absent (v1 rows) implies a legacy scalar
    locator inferred from whichever bare field is set (section/transcript_line/
    pdf_page/json_path) — see FactLocator.effective_kind()."""

    FMP_JSON_TABLE = "fmp_json_table"
    PDF_SLIDE = "pdf_slide"
    TRANSCRIPT_SPAN = "transcript_span"
    HTML_SPAN = "html_span"
    DERIVED = "derived"
    VENDOR_FIELD = "vendor_field"


class TableCellRef(BaseModel):
    """WHERE inside a cached JSON/HTML table a value sits — row + column
    identity, not just a record-index pointer. All fields optional so a
    partial capture (row label known, column header not parsed) still gives
    the render path something to highlight."""

    section: str | None = None        # top-level / note-table section key
    table_title: str | None = None    # inner title, e.g. "Debt (Details)"
    row_label: str | None = None      # as-filed row label, e.g. "Term loan, net"
    row_axis_path: list[str] = Field(default_factory=list)  # XBRL axis chain
    column_header: str | None = None  # as-filed column header, e.g. "Dec. 31, 2025"
    cell_value_as_extracted: str | None = None  # raw cell text before scaling/parsing
    json_path: str | None = None      # exact pointer into the cached JSON, kept
                                       # for the render path's direct-lookup fast case


class TranscriptSpanRef(BaseModel):
    doc_id: int | None = None         # documents.id of the transcript (redundant
                                       # with source_doc_id on the fact row, but a
                                       # derived input may reference a doc that
                                       # ISN'T the fact's own source_doc_id)
    transcript_line: int | None = None      # existing field, kept as the anchor
    speaker_turn_index: int | None = None   # ordinal turn, independent of line count
    char_start: int | None = None
    char_end: int | None = None


class HtmlSpanRef(BaseModel):
    doc_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote: str | None = None          # the exact substring, for re-anchoring on drift


class VendorFieldRef(BaseModel):
    """The honest floor: a plain vendor endpoint value with no underlying
    filing/document at all (a live quote, a market-cap snapshot). There is
    nothing to "open" — the peek renders the raw JSON fragment instead."""

    endpoint: str            # e.g. "quote", "key-metrics-ttm"
    field: str                # e.g. "marketCap"
    period: str | None = None


class DerivedInputRef(BaseModel):
    """One input to a derived value — a recursive pointer to ANOTHER fact's
    locator (by fact table + id) OR a fully inline locator when the input
    fact row isn't separately queryable. Mirrors kpi_facts.computed_from's
    existing inputs[] shape (directives/data_provenance.md §9) — this is
    that shape promoted into the typed locator union, not a third format."""

    ref: str                  # "financial_fact" | "kpi_fact" | "segment_fact"
    fact_id: int | None = None       # row id in the named table, when resolvable
    item: str                 # line_item / KPI label / "<segment> <metric>"
    period_end: str | None = None
    doc_id: int | None = None
    tier: str | None = None


class DerivedRef(BaseModel):
    formula_id: int | None = None    # -> formula_definitions.id (metrics engine)
    display: str | None = None       # human-readable formula, e.g. "OI ÷ revenue (%)"
    method_flags: list[str] = Field(default_factory=list)
    inputs: list[DerivedInputRef] = Field(default_factory=list)


class FactLocator(BaseModel):
    # --- v1 fields, UNCHANGED (existing rows must keep validating) ---
    section: str | None = None
    transcript_line: int | None = None
    pdf_page: int | None = None
    json_path: str | None = None

    # --- v2 fields ---
    locator_version: int = 1          # bump to 2 the moment ANY v2 field is set
    kind: LocatorKind | None = None
    table_cell: TableCellRef | None = None
    pdf_bbox: tuple[float, float, float, float] | None = None  # (x0,y0,x1,y1), page coords
    transcript_span: TranscriptSpanRef | None = None
    html_span: HtmlSpanRef | None = None
    vendor_field: VendorFieldRef | None = None
    derived: DerivedRef | None = None

    # Verbatim snippet captured AT EXTRACTION TIME — every locator kind
    # carries this. It is the fallback rendering source: if the cached JSON
    # is later re-parsed differently, moved, or deleted, the peek still has
    # something concrete to show. ±context means "the extracted text plus a
    # short window either side" (row label + cell for tables, sentence
    # window for transcript/HTML spans) — NOT the whole page.
    verbatim_snippet: str | None = Field(default=None, max_length=2000)

    def effective_kind(self) -> LocatorKind | None:
        """kind if set (v2); else inferred from whichever v1 scalar is
        present, for legacy rows and the renderer dispatch in §2."""
        if self.kind is not None:
            return self.kind
        if self.pdf_page is not None:
            return LocatorKind.PDF_SLIDE
        if self.transcript_line is not None:
            return LocatorKind.TRANSCRIPT_SPAN
        if self.section is not None or self.json_path is not None:
            return LocatorKind.FMP_JSON_TABLE
        return None
```

Notes:

- `to_json()` / `from_json()` keep their exact current semantics (exclude-none dump,
  `None` for an all-empty locator). A v1 row and a v2 row round-trip through the same
  two functions unchanged — no reader needs a branch for "which version am I
  parsing," only `effective_kind()` for dispatch.
- `pdf_bbox` is optional even on `pdf_slide` locators — a bare page number without a
  bounding box is still fully renderable (§2 shows the whole page, no highlight box);
  the bbox is an enhancement, never a requirement, because most PDF text extractors
  (`pypdf`) don't give you bboxes for free and PyMuPDF's `page.search_for(quote)` is
  the fallback path (see §3.2).
- `derived` is recursive by construction (`DerivedInputRef` can itself resolve to a
  fact whose own `locator.kind == DERIVED`) — the peek in §2.5 walks this as a tree,
  not a fixed one-level lineage list like today's `_lineage_rows`.
- `vendor_field` deliberately has **no doc_id-shaped path anywhere** in it — it is the
  documented floor for values that are honestly just an API field with no filing
  behind them (a live `quote` price, `key-metrics-ttm.marketCap`). The peek for this
  kind (§2.6) still renders something concrete (the raw endpoint JSON fragment,
  fetched-at) rather than a dead end — "vendor_field" is a legitimate terminal answer
  to "where did this come from," not a cop-out, provided the render path proves it.

### 1.3 Storage — extends, does not break, today's column

No new migration is *required* to start writing v2 shapes: `locator` is already
`TEXT(JSON)` on `financial_facts` / `kpi_facts`, and per §4.1's contract note ("no
DB-level CHECK... validity is the write path's job") a richer JSON payload is
accepted by the column as-is. What §4 does add:

- **`segment_dimensions.locator`** (currently mid-flight per
  `segment_quarterly_framework.md` §4.1) should land with this v2 schema already in
  mind, not the v1 scalar-only shape — cheap now, expensive to retrofit once segment
  rows accumulate (see §7's explicit note on interleaving with that framework).
- **`financial_facts` gains a `source_excerpt` column** (alembic migration, additive,
  nullable `TEXT`, mirrors `kpi_facts.source_excerpt` at the same 1024-char clip). This
  is the one genuine schema gap: `directives/data_provenance.md` §7's writer table
  says "n/a (financial_facts has no excerpt column)" for every FMP-statement writer —
  meaning the highest-volume fact table has never been able to carry a verbatim quote.
  `FactLocator.verbatim_snippet` (§1.2) is the primary carrier going forward and lives
  inside the locator JSON itself (so it never needs its own column on every table),
  but a first-class `source_excerpt` column on `financial_facts` is still worth adding
  for parity with `kpi_facts` and because some existing readers
  (`report/sections/financials.py`) already expect a flat excerpt field pattern.
  **Decision: carry the verbatim snippet inside `FactLocator.verbatim_snippet` for
  ALL fact tables going forward (segment_dimensions included); do not also add
  `financial_facts.source_excerpt` — one representation, not two.** (Superseding the
  bullet above — noted so an implementer doesn't do both.)
- `fact_overrides` (alembic 0111) gains a nullable `locator TEXT` column (additive) so
  8-K-derived overrides (§3, Phase C) can carry a locator the same way facts do.

### 1.4 Validation contract

- A `FactLocator` with `kind` set but the corresponding nested ref `None` is invalid —
  enforce via a Pydantic `model_validator(mode="after")` (`kind=FMP_JSON_TABLE`
  requires `table_cell` non-None, etc.). This is the schema-level half of "cannot land
  without a renderable locator" (§4 adds the persist-time half).
  - `vendor_field` is the sole kind allowed to have **no doc_id anywhere in the
    locator** — every other kind's nested ref must resolve to a `doc_id` (either the
    fact's own `source_doc_id`, or, for `transcript_span`/`html_span` refs used
    *inside* a `derived.inputs[]` entry, an explicit `doc_id` on that entry).
- `verbatim_snippet` max length 2000 (deliberately larger than the existing
  `source_excerpt` 1024 cap — a table-cell snippet needs room for a row label +
  several column values, not just one number-in-context).

### 1.5 What each `kind` covers (mapping back to the owner's list)

| kind | covers | primary key fields |
|---|---|---|
| `fmp_json_table` | FMP statement-endpoint arrays, FMP form-10-K/10-Q section tables, generic_xbrl_capture note tables, segment crosstab cells | `table_cell.{section, table_title, row_label, column_header, json_path}` |
| `pdf_slide` | IR deck / supplement PDF pages | `pdf_page` (v1 field, reused) + optional `pdf_bbox` |
| `transcript_span` | earnings-call / IR transcript quotes | `transcript_span.{transcript_line, speaker_turn_index, char_start/end}` |
| `html_span` | SEC EDGAR HTML filings read directly (not the FMP-parsed JSON), 8-K exhibit HTML | `html_span.{char_start/end, quote}` |
| `derived` | any `fmp_derived_kpis` / metrics-engine formula output | `derived.{formula_id, display, inputs[]}` (recursive) |
| `vendor_field` | plain FMP endpoint scalar with no filing behind it (quote, market cap, analyst estimates) | `vendor_field.{endpoint, field, period}` |

### 1.6 Naming collision to resolve (found during ground-truth pass)

`src/dashboard/evidence_drawer.py::_Citation.locator` is an **unrelated** freeform
string used for alert-evidence citations (composed from `{period, line_number}` for
things like `earnings_tone` triggers). It predates this design, is not
`FactLocator`-typed, and must **not** be conflated with the schema in §1.2 — do not
rename it to match, and do not have it start emitting `FactLocator` JSON; it is a
different concept (an alert's evidence pointer, not a fact's source position) that
happens to share a field name. Flag this in code review of any PR touching that file
so a future reader doesn't assume the two are the same contract.

---

## 2. Peek rendering per locator type

### 2.1 Endpoint shape

`GET /api/peek/provenance/<fact_ref>` where `fact_ref` is `<table>:<id>` (e.g.
`kpi_facts:88213`, `financial_facts:40921`, `segment_dimensions:5510`) — a single
dispatcher, not one route per kind, because the kind is a property of the *row*
(`locator.effective_kind()`), not knowable from the URL alone. Registered in
`execution/comments_server.py` alongside the existing `/api/peek/*` family; the
handler lives in `src/pipeline/peeks.py` as `render_provenance_peek_v2` (name
disambiguated from the existing freshness-dots `render_provenance_peek`, which is an
unrelated function despite the similar name — see that file's existing docstring
list).

```python
@app.route("/api/peek/provenance/<fact_ref>", methods=["GET"])
def peek_fact_provenance(fact_ref: str):
    from pipeline.peeks import render_fact_provenance_peek
    html = render_fact_provenance_peek(db_path, repo_root, fact_ref)
    if html is None:
        abort(404)
    return Response(html, mimetype="text/html")
```

Loads the row, `FactLocator.from_json(row.locator)`, dispatches on
`.effective_kind()`. Every branch below degrades per §2.7 rather than 404ing once the
row itself is found — a 404 means "no such fact," never "couldn't render its
evidence."

### 2.2 `fmp_json_table` — highlighted table render

Reuses and extends `pipeline/source_viewers.py::render_form10k_page` rather than
writing a new renderer:

1. Load the cached JSON (`documents.file_path`) — works for BOTH the already-handled
   `fmp_10k_json`/`fmp_10q_json` section-keyed shape AND, newly, the plain
   array-of-records FMP statement endpoints (`fmp_income_statement`,
   `fmp_balance_sheet`, `fmp_cashflow`, `fmp_as_reported`, sec_xbrl companyfacts).
   Add a second parse branch: when the JSON root is a `list[dict]` (the statement
   endpoint shape) rather than a `dict` (the section-keyed 10-K/10-Q shape), render
   it as one HTML table — rows = records (periods), columns = the fields present in
   that record — instead of the label/value section reader.
2. Locate the cited cell: `table_cell.json_path` gives the exact `[i].field`
   pointer for a fast direct lookup; `table_cell.row_label` +
   `table_cell.column_header` are the fallback match (string equality against the
   parsed row/column headers) when `json_path` is stale (the source file changed
   shape since extraction — rare, but the reason both are stored).
3. Render the table with the cited `<td>` given a `sv-cell-hit` class (same
   `:target`-flavored highlight treatment `.sv-lines li:target` already uses for
   transcript lines — warn-tinted background, outline, `scrollIntoView` on load).
4. Footer: "open full filing →" link to the un-highlighted `/source/<doc_id>` page.

`fragment=True` mode returns the chrome-less shape exactly like the existing
transcript/10-K fragments — the peek popover embeds it directly.

### 2.3 `pdf_slide` — page image render

New capability (§0.3/§0.5 flagged this as genuinely new work):

1. `pipeline.pdf_render.render_page_image(repo_root, doc_id, page_number) -> Path`:
   opens the source PDF with **PyMuPDF (`fitz`)** — already a soft dependency via
   `ir_uploads.py`'s fingerprinting path, so no new install — renders the page at a
   fixed DPI (150 is a reasonable default; legible for a slide, small enough to cache)
   to a PNG via `page.get_pixmap(dpi=150).save(...)`.
2. **Idempotent caching**: `.tmp/pdf_pages/<doc_id>/p<page_number>.png` (documents are
   content-addressed by `sha256`, so `<doc_id>/p<n>.png` is stable — re-requesting the
   same page is a filesystem check, not a re-render; a changed source PDF gets a new
   `documents` row per the provenance contract §2, hence a new `doc_id`, hence a new
   cache path — no invalidation logic needed). `.tmp/` because these are
   regenerable artifacts, not deliverables (per this repo's `.tmp/` convention).
3. When `pdf_bbox` is present, overlay a highlight rectangle on the served image
   (draw directly with PyMuPDF's `page.draw_rect()` before rasterizing, or as an
   absolutely-positioned `<div>` over an `<img>` sized to the pixmap's pixel
   dimensions — the latter is simpler and avoids re-rendering per-highlight).
4. When `pdf_bbox` is absent (the common case — most locators will only carry
   `pdf_page`), render the whole page unhighlighted; the caption states
   "cited value is on this page" and shows `verbatim_snippet` as a text callout below
   the image so the reader isn't hunting blind.
5. Route: `GET /source/<doc_id>?page=<n>&fragment=1` — extends the *existing*
   `/source/<doc_id>` dispatcher (`comments_server.py::source_viewer`) with a new
   doc-type branch (`doc_type LIKE 'ir_%'` and the file is a `.pdf`) rather than a
   parallel route, so the same fallback-page safety net (`render_fallback_page`) and
   `fragment=1` contract apply unchanged.

### 2.4 `transcript_span` — quote-highlighted turn

Extends `render_transcript_page` (already renders numbered lines with `#L<n>`
anchors): when `speaker_turn_index` or `char_start/char_end` are present, additionally
wrap the cited substring in a `<mark class="sv-quote-hit">` span within its line(s),
not just anchor-scroll to the line. Degrades to today's exact behavior
(line-level anchor only) when only `transcript_line` is set (every existing row).

### 2.5 `derived` — recursive formula tree

New peek shape (extends, but does not replace, `source_chip.py::_lineage_rows`'s
flat display):

```
render_derived_peek(conn, repo_root, locator: DerivedRef, depth=0) -> str
```

Renders: `display` as the formula header, then one row per `DerivedInputRef` —
each row is itself a `data-peek-url="/api/peek/provenance/<ref>:<fact_id>"` doorway
(same self-referential peek pattern `render_fit_peek`'s "full what-if workup"
footer link already uses in `pipeline/peeks.py`) so clicking an input re-renders the
popover one level deeper. Depth-limited to 4 (matches `_MAX_LINEAGE_INPUTS`-style
guard already in `source_chip.py`) with a "+N more" footer past that, both to bound
render cost and because a formula tree deeper than 4 is itself a smell worth
surfacing rather than rendering. A leaf input (one whose own locator is NOT
`derived`) renders its evidence inline instead of another doorway — i.e. the
recursion terminates at whichever concrete kind (`fmp_json_table`,
`transcript_span`, ...) actually grounds the number.

### 2.6 `vendor_field` — the honest floor

Never a dead end, never pretends to be more than it is:

```
render_vendor_field_peek(locator: VendorFieldRef, raw_endpoint_json: dict) -> str
```

Shows: `endpoint` + `field` + `period`, the fetched-at timestamp (from
`fmp_endpoint_status`, already tracked per `pipeline/peeks.py::_prov_ticker_rows`),
and the raw JSON fragment for that field pretty-printed (not the whole endpoint
payload — just the relevant key(s), to keep the popover small). Caption: "Vendor
field — no underlying filing; this is the value FMP's API returned." This is the
correct terminal answer for a live quote or a market-cap snapshot — the design
requirement is that it *say so explicitly* rather than showing a bare "FMP" tier
chip with nothing behind it.

### 2.7 Graceful floor for legacy / poor locators — never a dead end

For a row whose locator is `None`, v1-scalar-only-and-unmapped, or whose target
`kind`'s render branch throws (file moved, JSON re-shaped, PDF missing):

```
render_legacy_provenance_peek(doc_row, locator_raw, verbatim_snippet) -> str
```

Shows whichever of these exist, best-effort: the `documents` row's identity (doc
type, accession, filing date — same `_doc_meta_html` block `source_viewers.py`
already renders), a link to `/source/<doc_id>` (un-fragmented — whatever that
dispatcher's fallback gives), the raw locator JSON if any (today's
`.src-pop-locator` text block, kept as-is), and a `<span class="k-chip
k-chip-warn">provenance_quality: legacy</span>` badge (composes the existing kit
chip primitive per `directives/design_language.md` — no new freehand pill). This is
literally today's behavior (§0.3) **minus the silent dead end** — it always renders
*something* and always names what it couldn't do, which is the one behavior change
from today's raw-JSON-text status quo.

---

## 3. Extraction-time capture — the pipeline broadening

### 3.1 Shared helper — `src/pipeline/locators.py`

New module, single source of truth every extractor calls into (mirrors how
`persist_manifest` is already "the single source of truth" for the KPI insert path
per that module's own docstring):

```python
def table_cell_locator(
    *, section: str | None, table_title: str | None, row_label: str | None,
    row_axis_path: list[str], column_header: str | None, json_path: str | None,
    cell_value_as_extracted: str,
) -> FactLocator: ...

def pdf_slide_locator(
    *, pdf_page: int, verbatim_snippet: str, bbox: tuple[float, float, float, float] | None = None,
) -> FactLocator: ...

def transcript_span_locator(
    *, transcript_line: int, verbatim_snippet: str,
    speaker_turn_index: int | None = None, char_start: int | None = None, char_end: int | None = None,
) -> FactLocator: ...

def vendor_field_locator(*, endpoint: str, field: str, period: str | None = None) -> FactLocator: ...

def verify_quote_in_source(quote: str, source_text: str) -> bool:
    """Whitespace/case-normalized substring check — the anti-hallucination gate
    for LLM-returned anchors (§3.3). False means: reject the locator, do not
    persist a fabricated anchor."""
```

Each `*_locator` helper stamps `locator_version=2` and the matching `kind`
automatically — an extractor cannot accidentally emit a v2-shaped payload without
the discriminant, which keeps `effective_kind()` reliable.

### 3.2 Per-extractor changes

| extractor | today | change required | cost |
|---|---|---|---|
| `table_extractors/generic_xbrl_capture.py::_walk_section` | `FactLocator(section=section_key)` only; `row.label`, `row.axis_path`, `period_labels[col]` are ALL already local variables in this function, just not passed through | call `locators.table_cell_locator(section=section_key, table_title=inner_title, row_label=row.label, row_axis_path=row.axis_path, column_header=rows[0].period_labels[col], json_path=None, cell_value_as_extracted=str(raw))` in place of the current bare `FactLocator(...)` at line ~456 | **near-zero** — every value is already a local variable in scope; this is a pure enrichment, not new extraction logic |
| `compute/_common.py::extract_facts_with_spec` (FMP statement extractors) | `json_path="[<i>].<field>"` only, no row/column identity | add `table_cell_locator(json_path=..., row_label=<humanized field name>, column_header=<record's `date`/`period`>, cell_value_as_extracted=str(raw_value))` — the record dict is already in hand at this call site | low — same data, one more constructor call |
| `pipeline/sec_xbrl.py` (companyfacts) | `json_path="facts.<ns>.<tag>.units.<unit>[<i>]"` | same enrichment: `row_label=<tag>`, `column_header=<end date from the unit entry>` | low |
| `compute/segment_crosstabs_llm.py` | (verify current state before implementing — not directly inspected in this pass beyond confirming it is `document_table_extractor`-registered like `generic_xbrl_capture`) — expected to already have row/column/axis identity available from the same `iter_xbrl_table` walker shape; wire through `table_cell_locator` identically | low-medium |
| `execution/extract_kpis_from_ir.py` (IR deck PDF readout) | `pdf_page` in manifest `locator`; `source_excerpt` separately | switch the manifest schema to accept `locator: {"pdf_page": N, "bbox": [...]}` optionally, and start requiring `source_excerpt` non-empty whenever a `locator.pdf_page` is present (schema-level pairing — a page without a quote is a weaker locator) | low — manifest-JSON contract change, in-session LLM already reads the PDF and can report a bbox from PyMuPDF's `page.search_for()` on its own quote if asked to |
| `execution/extract_commitments_from_transcript.py` | writes alert-evidence citations (§1.6's *different* locator concept), not `FactLocator` rows — out of scope for the fact-table locator work; if/when this pipeline starts writing `kpi_facts`/`financial_facts` rows directly (it currently feeds the alert/evidence system, not the fact tables), it adopts `transcript_span_locator` at that point, not before | n/a today |
| `execution/extract_8k_overrides.py` / `src/provenance/edgar_8k.py` | `source_excerpt` only, no locator, and `fact_overrides` has no locator column | (a) add the `fact_overrides.locator` column (§1.3), (b) have `edgar_8k.py`'s LLM extraction return an `html_span` (char offsets + quote into the fetched EX-99.1 HTML text) validated via `verify_quote_in_source`, (c) `execution/extract_8k_overrides.py --apply` persists it alongside the existing `source_excerpt` | medium — new locator kind wired end-to-end through one pipeline, good Phase-C pilot for `html_span` |
| `compute/fmp_derived_kpis.py` | writes `computed_from`, no `locator` | wrap `computed_from` into `FactLocator(kind=DERIVED, derived=DerivedRef(...))` at write time — same payload, promoted into the typed union so `viewer_href`/the peek dispatcher can find it via `.locator` instead of a side-channel column read | low — data already assembled, just re-homed into `locator` (keep `computed_from` itself as-is for backward read compatibility; `locator.derived` becomes the canonical read path going forward, per the "one vocabulary" alignment goal) |

### 3.3 LLM-extraction contract change — anchor grounding + verification

Every LLM extractor that populates a locator (`kpi_extract_summaries.py`,
`extract_kpis_from_ir.py`, `edgar_8k.py`, and any future LLM writer) must return, per
value, a `quote` field the schema validates as **non-empty and present verbatim in
the source text** — not just a `source_excerpt` describing the value, but the literal
grounding span. Concretely:

1. The LLM's structured output schema gains a required `anchor_quote: str` field
   (min length ~10 chars — long enough to be a real quote, not a fragment that
   matches everywhere) alongside the existing `value`/`unit`/`confidence`.
2. `pipeline.locators.verify_quote_in_source(anchor_quote, source_text)` runs BEFORE
   persistence — a normalized (whitespace-collapsed, case-folded) substring check
   against the actual source document text (the transcript file, the extracted PDF
   text, the 8-K exhibit HTML-to-text). A failing check is **not** a "reject silently
   and drop the value" outcome — it degrades the same way the rest of this repo's
   LLM-call failure policy works (`directives/data_provenance.md`'s
   `llm_extracted` tier already starts in "validation quarantine"): the value is still
   persisted (never drop a real number), but the locator itself is downgraded to a
   `verbatim_snippet`-only, `kind=None` (legacy-shaped) locator, a `validation_issues`
   row is written (`rule=hallucinated_anchor`, `severity=warn`), and confidence takes
   the existing LLM-method penalty per `pipeline/confidence.py`. This makes a
   hallucinated anchor visible and downgraded, never silently trusted, and never a
   reason to lose the extracted value entirely.
3. `verify_quote_in_source` is intentionally a simple substring check, not fuzzy
   matching — a locator that can't be found verbatim is, by definition, not something
   the peek can highlight reliably; fuzzy-matched "close enough" quotes are worse than
   an honest legacy fallback.

---

## 4. Structural enforcement (the P0 mechanism)

Three layers, cheapest-to-strongest, matching how this repo already gates other
cross-cutting contracts (the UI kit's `tests/test_ui_controls.py` registry pattern is
the explicit precedent named in the brief).

### 4.1 Persist-time enforcement — `persist_manifest` / fact-store insert helpers

`pipeline/kpi_persistence.py::persist_manifest`, `compute/_common.py::
insert_financial_facts`, and the segment-fact insert path each gain a **required**
`locator: FactLocator | LegacyEscapeHatch` argument (a typed union, not `Optional`
with a silent `None` default):

```python
class LegacyEscapeHatch(BaseModel):
    """Explicit, grep-able opt-out for a writer that genuinely cannot produce a
    renderable locator yet. Requires a reason — 'I forgot' is not machine-
    checkable, but a reviewer can grep `LegacyEscapeHatch(` in a diff and ask
    why. Every use is logged (validation_issues, severity=info,
    rule=locator_escape_hatch) so the coverage audit (§4.3) can count them."""

    reason: str = Field(min_length=8)
```

`persist_manifest(..., locator: FactLocator | LegacyEscapeHatch)` — a caller passing
neither is a type error at the call site, not a runtime `None` default that silently
propagates. Existing call sites (§3.2's table) are updated in the SAME PRs that wire
their locator enrichment, so by the time this gate lands, every registered
extractor already has a real `FactLocator` to pass — the escape hatch exists for
(a) truly exceptional future writers and (b) the retrofit window (§5), not as the
default path for today's extractors.

`FactLocator` itself is validated per §1.4's `model_validator` — a `kind` set
without its matching nested ref raises at construction time, before it ever reaches
`persist_manifest`. This is the "fail at persist time" half of the requirement:
between (a) the constructor validator and (b) the required-argument signature
change, there is no code path in the registered extractors that produces a fact row
with an unrenderable locator without EITHER passing the typed model correctly OR
explicitly invoking `LegacyEscapeHatch(reason=...)`.

### 4.2 CI guard test — `tests/test_extractor_locator_coverage.py`

New test file, same shape as `tests/test_ui_controls.py`'s `REGISTERED` +
"surface not in registry fails CI" pattern:

```python
# Every extractor that writes financial_facts/kpi_facts/segment_dimensions,
# and the locator_version it is asserted to emit on its own fixture. Adding
# a new extractor means adding it here (or being caught by
# test_every_extractor_is_registered failing) AND proving >=2 on a fixture.
REGISTERED_EXTRACTOR_LOCATOR_VERSIONS: dict[str, int] = {
    "table_extractors.generic_xbrl_capture": 2,
    "compute._common.extract_facts_with_spec": 2,
    "compute.as_reported": 2,
    "pipeline.sec_xbrl": 2,
    "compute.segment_crosstabs_llm": 2,
    "execution.extract_kpis_from_ir": 2,
    "compute.fmp_derived_kpis": 2,
    "provenance.edgar_8k": 2,
    # legacy, intentionally pinned at 1 until Phase C/backfill lands:
    "compute.s1_financials": 1,  # no stable anchor; documented exception
    "ir_pipeline.ingest": 1,     # spreadsheet cells, no JSON/PDF position
}


def test_every_locator_writer_is_registered():
    """Mirrors test_ui_controls.py's surface-discovery check: grep src/ +
    execution/ for `locator=FactLocator(` / `persist_manifest(` call sites,
    fail if one isn't in REGISTERED_EXTRACTOR_LOCATOR_VERSIONS."""


def test_registered_extractors_emit_locator_version_on_fixture():
    """For each REGISTERED entry with version >= 2: run the extractor over a
    small canned fixture (reuses the existing fixtures in
    tests/test_extractor_locators.py where present) and assert the emitted
    FactLocator.locator_version >= the registered floor, AND that
    .effective_kind() is not None, AND that verbatim_snippet is non-empty."""
```

This directly follows the brief's "registry + fail-on-unregistered" instruction and
extends the EXISTING `tests/test_extractor_locators.py` (which already has per-writer
fixture tests, just without a version-floor assertion or a registry that fails when a
new writer is added and forgotten).

### 4.3 Coverage audit CLI — `execution/provenance_coverage_report.py`

Modeled directly on the existing `pipeline/capture_coverage.py` /
`execution/capture_coverage_report.py` pattern (§0, `capture_coverage.py`'s own
docstring: "coverage is queryable across runs rather than scrolling past in a log
line" — same philosophy, different metric):

```
$ python execution/provenance_coverage_report.py [--ticker T] [--table financial_facts]

financial_facts     :  84,213 rows | locator NULL: 61,004 (72%) | v1-only: 9,880 (12%)
                       | v2 renderable: 13,329 (16%)
kpi_facts           :  22,401 rows | locator NULL: 3,102 (14%) | v1-only: 6,655 (30%)
                       | v2 renderable: 12,644 (56%)
segment_dimensions  :   8,004 rows | locator NULL: 8,004 (100%) | v1-only: 0 | v2: 0
  by extracted_by:
    capture_xbrl_v1     :  41,200 rows | v2 renderable: 38,900 (94%)
    fmp (statement)     :  40,000 rows | v2 renderable: 0 (0%)   <- Phase A target
    ir_spreadsheet      :   3,100 rows | v2 renderable: 0 (0%)   <- documented exception
```

Query shape: `SELECT extracted_by, COUNT(*),
SUM(CASE WHEN locator IS NOT NULL AND json_extract(locator,'$.locator_version')>=2
  AND json_extract(locator,'$.kind') IS NOT NULL THEN 1 ELSE 0 END) FROM <table> GROUP BY
extracted_by` — cheap, one pass, no joins; safe to run against the production DB
read-only (same `mode=ro` sqlite connection convention every peek reader already uses).
Surfaced as a System-console panel later (Phase B/C, not blocking) but the CLI alone
satisfies "measurable retrofit progress" from day one.

---

## 5. Retrofit strategy

### 5.1 Mechanically backfillable (re-run over cached JSON, no LLM, no re-fetch)

- **`generic_xbrl_capture` facts** — the source `fmp_10k_json` files are already
  cached on disk (`data/historical/fmp/{T}_form_10k_{Y}.json`); re-running
  `execution/extract_document_tables.py --table-kind xbrl_capture_all --refresh` after
  §3.2's enrichment lands **re-derives every locator with row/column identity from the
  exact same cached files**, no new fetch, no LLM cost. This is the highest-value,
  lowest-risk retrofit — do it first, and it alone likely moves `kpi_facts` from the
  §4.3 example's 56% to something close to that table's ceiling.
- **`compute/segment_crosstabs_llm.py` cells** — same shape, same re-run story,
  contingent on that extractor's own enrichment (§3.2) landing first.
- **FMP statement facts (`financial_facts` via `extract_facts_with_spec`)** — also
  mechanically re-derivable from the cached JSON (the record + field are already
  known; enrichment only adds `row_label`/`column_header`, no new data source needed)
  — re-run `execution/extract_income_statement.py` (and the balance-sheet/cashflow
  siblings) with `--refresh` semantics. This is Phase A's biggest lift by volume
  (§0.5) but the *retrofit* itself is cheap once the renderer (§2.2's array-shape
  branch) and the enrichment (§3.2) both exist — the cost is in building those, not in
  re-running the extractor.

### 5.2 Requires new capability before backfill is possible

- **IR-deck (`pdf_page`) facts** — cannot be backfilled at all until §2.3's
  page-image rendering ships (Phase B). Once it ships, EXISTING `pdf_page`-only
  locators become renderable immediately with zero data change — the render
  capability, not the data, was the blocker. No re-extraction needed for facts that
  already carry a bare `pdf_page`.
- **8-K override facts** — no locator today at all; needs Phase C's `html_span`
  wiring PLUS a one-time re-run of `execution/extract_8k_overrides.py` per existing
  override to backfill `fact_overrides.locator` (this one DOES require a fresh LLM
  pass per override, since the original extraction didn't capture the anchor quote —
  unlike §5.1's cases, the raw exhibit HTML is still fetchable from EDGAR by
  accession number so no data is lost, just an extra LLM cost per row).

### 5.3 Stays legacy-badged (no realistic backfill)

- Old LLM extractions (`compute/kpi_extract_summaries.py` rows written before §3.3's
  anchor-verification contract) — the source summaries may still exist, but without
  the original LLM call having returned a grounding quote, there's nothing to verify
  against retroactively; a "re-run the LLM extraction" pass is possible in principle
  (per the user's own standing preference for re-running real LLM extraction over
  hand-insertion — see `feedback_run_llm_extraction_for_new_quarters` in the
  platform's memory) but is a Phase C prioritization call, not automatic.
- `compute/s1_financials.py` and `ir_pipeline/ingest.py` rows — no stable anchor by
  design (documented in `directives/data_provenance.md` §7 as intentional); these stay
  `provenance_quality: legacy` permanently unless those extractors are rewritten,
  which is out of scope here.

### 5.4 Prioritization — surface visibility first

Retrofit order should follow what's actually rendered to the owner today, not raw row
count: run the §4.3 coverage CLI scoped to (a) portfolio holdings and (b) active
evaluation-tickers first (the same "held or being evaluated" set that drives cron
tiering elsewhere in this platform), and backfill §5.1's mechanical cases for THOSE
tickers before running a full-universe pass. A 10,000-row improvement on a ticker
nobody is looking at is worth less than a 200-row improvement on a name currently
open in the workspace.

---

## 6. UI contract

### 6.1 Source-chip changes (`src/ui/source_chip.py`)

- `viewer_href()` gains branches for `kind == fmp_json_table` (link to
  `/api/peek/provenance/<table>:<id>`, the new dispatcher, not a raw `/source/<doc_id>`
  — the peek is now the primary click target, `/source` stays the "open full
  document" escape valve) and `kind == pdf_slide` / `derived` / `vendor_field`
  likewise. `data-peek-url` attribute is added to the chip's summary element
  wherever a peek endpoint resolves — this is the existing peek-hook contract
  (`data-peek-url` + `data-peek-title`, already used throughout `pipeline/peeks.py`'s
  footer links), not a new interaction pattern.
- **Chip label** changes from the bare tier abbreviation (`FMP`, `SEC`, `LLM`) to
  tier abbreviation **plus a locator hint** wherever one resolves: `"10-Q Q3'25 ·
  Segment note"`, `"IR deck p.14"`, `"derived · 3 inputs"`, `"vendor field"`. The tier
  abbreviation stays first (color-coding by trust tier is load-bearing per
  `SOURCE_CHIP_ABBREV`/tier CSS classes) — the hint is appended, not a replacement.
  Composes via the existing `.k-chip` primitive family (`k-chip-mono` for the
  hint-bearing variant, since it's now carrying more text than the bare 3-letter
  abbreviation) — **no new freehand chip markup**; this is the one place §4's
  `directives/design_language.md` gate is directly in play for this feature.
- The popover's raw-JSON `.src-pop-locator` block (today's `_esc(src.locator)` dump)
  is replaced by a compact "kind: <effective_kind>" line plus the verbatim snippet
  (truncated) for anything that ISN'T going to get a full peek render (i.e. the
  legacy floor case, §2.7) — the raw JSON stays available but demoted to a
  `<details>` inside the popover rather than shown by default, since it's debug
  information, not the primary read.

### 6.2 Test gate

Any change to `source_chip.py`, `peeks.py`, or `source_viewers.py` touches CSS-
emitting surfaces already in `tests/test_ui_controls.py`'s `REGISTERED` set
(`pipeline/peeks.py` and `pipeline/source_viewers.py` are both already listed —
confirmed in that file). **Run `python -m pytest tests/test_ui_controls.py -q`** for
any of this work — it will catch a freehand pill/badge substituting for `.k-chip`/
`.k-pill`. Because §2's `fmp_json_table` table render and §2.3's PDF page render are
new CSS blocks, they must use on-scale tokens (`var(--fs-*)`, `var(--radius)`,
`color-mix` tones) from day one, not "clean it up later" — the guard only catches
component drift (the `kit-badge` check), not raw-hex/off-scale usage in a genuinely
new block, so review that by hand per the AGENTS.md §4 note.

Any change touching `report/renderers/*` (if the `fmp_json_table` peek ever gets
promoted into the static workspace report, beyond the peek popover) additionally
needs `GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py` and a manual
diff review of the regenerated golden — do not blind-regenerate and commit without
reading the diff.

---

## 7. Phasing with blast radius

### Phase A — schema + persist-time enforcement + `fmp_json_table` + `vendor_field` (biggest volume)

Scope: §1 (schema), §3.1 (`locators.py` helper), §3.2's enrichment for the FMP
statement extractors + `generic_xbrl_capture` + `sec_xbrl`, §4.1 (required-locator
argument + `LegacyEscapeHatch`), §4.2 (CI registry test), §2.2 (`fmp_json_table`
peek, including the new array-shape branch in `source_viewers.py`), §2.6
(`vendor_field` peek), §6.1's chip changes for these two kinds only, §5.1's
mechanical re-run backfill.

Blast radius: touches the highest-traffic write path (`persist_manifest`,
`insert_financial_facts`) — every existing extractor call site needs updating in
lockstep with the signature change (§4.1), so this phase must land as one
coordinated PR set, not an incremental rollout of the required-argument change
alone (a partial rollout would break every un-migrated extractor's next run).
Recommend: land `locators.py` + schema + peek rendering first (additive, nothing
breaks), migrate every registered extractor's call sites in the SAME PR wave, THEN
flip `persist_manifest`'s argument from optional to required as the final commit of
the wave — never merge the required-argument flip before every call site is updated.

**Blocks / interleaves with the metrics engine (Phase 1)**: `bottoms_up_metrics_engine.md`'s
own Phase 1 writes `kpi_facts.computed_from` for every derived value. Per this
design's §1.2/§3.2 (`derived` locator, `fmp_derived_kpis.py` row), that Phase 1 work
should emit `locator.derived` in the SAME shape from day one rather than writing
plain `computed_from` now and retrofitting the typed union later — retrofitting
`computed_from` → `locator.derived` across every derived KPI row after the metrics
engine ships would be strictly more expensive than aligning the two designs during
Phase A. **Recommendation to the metrics-engine implementer: coordinate timing so
Phase A of this design and Phase 1 of the metrics engine land the `derived` locator
kind together, not sequentially.**

### Phase B — PDF slide rendering + IR-deck retrofit

Scope: §2.3 (PyMuPDF page-image rendering + `.tmp/pdf_pages/` caching), §3.2's
`extract_kpis_from_ir.py` manifest schema change (bbox-optional), §5.2's IR-deck
"renders immediately, no re-extraction" backfill, §6.1's chip hint for `pdf_slide`
("IR deck p.14"), the coverage CLI's (§4.3) per-source breakdown surfaced as a
System-console panel (optional polish, not blocking).

Blast radius: purely additive (new route, new render module, no existing write path
touched) — lowest-risk phase, can ship independently of A's completion for the
render half, though the chip-hint UI change (§6.1) wants Phase A's chip refactor
landed first so it isn't done twice.

### Phase C — transcript/8-K/LLM-anchor verification + derived-formula tree peek

Scope: §2.4 (quote-highlighted transcript span, beyond today's line-anchor), §2.5
(recursive derived-formula peek — depends on Phase A's `derived` kind existing),
§3.3 (LLM anchor-quote schema + `verify_quote_in_source` gate, applied to
`kpi_extract_summaries.py` and `edgar_8k.py`), §1.3/§3.2's `fact_overrides.locator`
column + 8-K `html_span` wiring, §5.2's 8-K backfill (re-run with LLM cost), §5.3's
explicit non-backfill decision for old ungrounded LLM extractions.

Blast radius: the anchor-verification gate changes LLM extractor OUTPUT SCHEMAS
(new required `anchor_quote` field) — this is a breaking change to any manifest-JSON
producer/consumer pair, so every LLM-extraction call site (not just the persist
side) needs the schema bump in the same PR. Lowest urgency of the three phases
(transcript/8-K volume is far smaller than Phase A's FMP-statement volume), and the
one phase most safely deferred if time runs short — a fact with a legacy-badged
locator (§2.7) is a worse UX than a renderable one, but it is never a broken one.

### Cross-phase note on `segment_quarterly_framework.md`

That framework's `segment_dimensions.locator` migration (its own §4.1, not yet
landed as of this writing per the ground-truth pass) should target THIS document's
v2 schema directly rather than the plain v1 scalar shape its own spec currently
describes — its authors already committed to reusing `FactLocator` verbatim ("do
not invent a second locator shape"); this design is the concrete v2 extension of
that promise. Whichever of the two designs' implementations lands second should
explicitly check the first hasn't drifted from this shared schema.
