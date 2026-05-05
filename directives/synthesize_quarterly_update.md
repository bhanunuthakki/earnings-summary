# Directive: Synthesize Quarterly Update (Audio-Less Fallback)

## Goal

Produce a `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` file for quarters where no
earnings call audio is publicly available (NVO Q4 2025 and RBRK Q4 FY25 are the
two known cases as of 2026-05-04). The synthesized file fills the same slot
that a real Whisper transcript would, so downstream consumers — Say-Do pairwise
analysis, thesis tracker, master-PDF builder — keep working without per-ticker
special cases.

The output is **clearly labeled as synthesized** in a banner header. It is not
a substitute for live management commentary (no Q&A) and must not be cited as a
direct quote from a call.

## Inputs

| Input | Where |
|---|---|
| Ticker | `--ticker <SYMBOL>` |
| Year | `--year <YYYY>` |
| Quarter | `--quarter <1-4>` |
| Press release URL | `--press-release-url <URL>` |
| Press release PDF | `--press-release-pdf <PATH>` |
| Press release text | `--press-release-text "<text>"` (last-resort manual paste) |
| Recap URL(s) | `--recap-url <URL>` (repeatable) |
| Explicit IR PDF(s) | `--ir-pdf <PATH>` (repeatable) — e.g. annual report, supplemental |
| Disable IR auto-scan | `--no-auto-ir` |
| FMP period_end hint | `--fmp-period-end YYYY-MM-DD` (when calendarYear/period match is unreliable, e.g. RBRK fiscal year) |

By default the script also **auto-scans `micro_thesis/sources/<TICKER>/`** for any PDF whose filename contains both `Q<N>` and `<YEAR>` (case-insensitive) — these are folded in as IR-document sections. This is how the existing investor presentations / annual reports already in the repo become input to thesis tracking and say-do without restating them on the CLI.

At minimum, one of `--press-release-url` / `--press-release-pdf` / `--press-release-text` / `--recap-url` / `--ir-pdf` must yield content (or the auto-scan must find a match). FMP financials are pulled automatically when `FMP_API_KEY` is set.

## Source priority

1. **Company press release / quarterly update** — primary content. Either an HTML URL (parsed with BeautifulSoup; nav/footer/scripts stripped) or a PDF (parsed with pypdf).
2. **FMP financial snapshot** — quarterly income / balance / cashflow / key-metrics for the `(year, quarter)` (or matched on `--fmp-period-end`). Stored as the raw FMP JSON for that period so the downstream summarizer can compute KPIs without re-fetching.
3. **External recaps** — Motley Fool, Yahoo Finance, Seeking Alpha free portion, etc. Useful for analyst color and post-call market reaction context.

## Output structure

```
transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt
```

File begins with a `=== SYNTHESIZED QUARTERLY UPDATE ===` banner and a sources list, followed by sections:

```
=== COMPANY PRESS RELEASE / QUARTERLY UPDATE ===
[source: <URL or path>]
<extracted text>

=== FMP FINANCIAL SNAPSHOT (period_end=YYYY-MM-DD) ===
[source: FMP api/v3 quarterly statements for <TICKER>]
-- Income Statement (...) --
{ ...raw JSON... }
-- Balance Sheet (...) --
...

=== EXTERNAL RECAP / DISTILLATION ===
[source: <URL>]
<extracted text>
```

The script also registers the file in `.tmp/transcript_index.json` with `source=synthesized_text` and `has_qa=False`.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Synthesize the file | `execution/synthesize_quarterly_update.py` |
| Curated YouTube manifest (gap rows trigger this directive) | `.tmp/youtube_earnings_links.json` |

## When to invoke

- Whenever `execution/fetch_audio_transcripts.py --links-file ...` prints a `[gap] <TICKER> Q<N> <YEAR>: <reason>` line.
- For NVO and RBRK quarters where the real call is on the IR webcast only and no third-party YouTube re-upload exists.
- For new quarters added to the watchlist where YouTube coverage is missing — add the gap row to the links manifest with `gap_reason`, then run this script.

## Edge Cases & Known Constraints

- **Q&A absent**: synthesized files have no Q&A segment. The Say-Do pipeline must be tolerant of this — the source label `synthesized_text` and `has_qa=False` flag in the index let it gate accordingly.
- **Press release HTML quality varies**: some IR sites bury the body in nested divs or load via JS. When extraction returns < a few hundred chars, fall back to `--press-release-pdf` (download the press-release PDF from the IR site) or `--press-release-text` (manual paste from the rendered page).
- **FMP fiscal mismatch**: Rubrik's fiscal year ends Jan 31, so calendarYear/period from FMP may not align with how the call is labeled. Pass `--fmp-period-end` to disambiguate (e.g. `2025-01-31` for Q4 FY25).
- **No FMP key**: synthesis still runs; the FMP section is omitted with a `[warn]` to stderr.

## Verification

After running, confirm:
- [ ] `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` exists and starts with the synthesis banner.
- [ ] At least the press-release section is present and non-trivial in length.
- [ ] FMP section reports the correct `period_end` for the quarter.
- [ ] `.tmp/transcript_index.json` shows `source=synthesized_text` and `has_qa=false`.
- [ ] Re-running with the same args raises `FileExistsError` (idempotent guard — delete the .txt and the index entry to re-synthesize).
