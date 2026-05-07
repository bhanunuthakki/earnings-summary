# Directive: Fetch Q&A Transcript from Free Aggregators

## Goal

Pull just the **Question-and-Answer segment** of an earnings call from free, no-auth aggregator websites and write it into the same `transcripts/raw/` slot the audio fetcher and synthesizer use. This is the **primary** transcript-acquisition path for the say-do pipeline; audio (`fetch_audio_transcripts.py`) is the fallback when no aggregator has indexed the quarter yet.

## Why Q&A only

- **Prepared remarks** are 1:1 reproducible from the press release + investor deck (which `synthesize_quarterly_update.py` already pulls automatically). Re-transcribing them adds zero unique signal.
- **Q&A** is the unique audio-only content — analysts probing the edges of management's prepared message — and is exactly what say-do consistency analysis needs.
- Aggregators publish Q&A pre-segmented and **speaker-tagged** (e.g. `B Bipul Sinha`, `F Fatima Boolani`), which is structurally cleaner than diarising audio.
- Cost: $0 + ~1 second per quarter vs ~25 min of CPU + audio download + Whisper.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Aggregator fetcher chain | `src/aggregator_sources.py` |
| CLI orchestrator | `execution/fetch_qa_transcript.py` |

## Source chain (priority order)

Probed against the 11-name portfolio on 2026-05-06.

| # | Source | URL pattern | Coverage | Notes |
|---|---|---|---|---|
| 1 | `roic.ai` | `/quote/<TICKER>/transcripts/<YEAR>-year/<Q>-quarter` | **10/10 names**, including foreign ADRs (NVO, NU, MELI) and Brookfield (BN) | Direct URL — no list step. Uses **fiscal-year** labelling for fiscal-Q4 names (RBRK, VEEV). |
| 2 | `stockanalysis.com` | `/stocks/<ticker>/transcripts/<NUMERIC_ID>-q<N>-<YEAR>/` | 7/10 (misses NVO, GOOG, BN) | Two-step: list page → fetch. Numeric ID is internal. |
| 3 | `tickertrends.io` | `/transcripts/<TICKER>/Q<N>-earnings-transcript-<YEAR>` | Recent quarters; aggressive rate-limit on bursty access | Direct URL. Use sparingly. |

**Excluded** (paywalled / blocked / not-Q&A): `seekingalpha.com` (paywall), `gurufocus.com` (403), `marketscreener.com` (paywall now), `yahoo.com` finance (no Q&A in body), `public.com` (snippet only), `nasdaq.com` (paywall), `wallstreetzen.com` (no transcript subpage), `fool.com` (date-based URLs not predictable without a search index).

## Boundary detection

Q&A start and end are detected with templated-cue regexes — operator scripts are uniform across major IR call hosts (Q4 Inc, Notified, etc.) so the cues reproduce verbatim in every aggregator's output. **This is structural boundary detection on a known protocol, not keyword classification.**

| Cue | Pattern (regex) |
|---|---|
| Start | `first question (comes from\|is from)`, `we'll (now \|)open the line for questions`, `begin the question-and-answer session`, `i'll turn the call over to the operator` |
| End | `that concludes today's conference call`, `you may now disconnect`, `thank you for your participation. you may` |

If no start cue is found in a page's visible text, that source is skipped and the chain falls through to the next.

## Output

| Artifact | Path | Shape |
|---|---|---|
| Q&A transcript | `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` | Synthesizer banner header + speaker-paragraphed Q&A text |
| Index entry | `.tmp/transcript_index.json[KEY]` | `source=aggregator_<roic\|stockanalysis\|tickertrends>`, `has_qa=True`, `qa_status`, `qa_details` |

`transcript_qa.validate_transcript` routes `aggregator_*` sources to the synthesized-flavor validator (size + banner + section count) since these files have no Whisper timestamps.

## CLI

```
python execution/fetch_qa_transcript.py --ticker NOW --year 2026 --quarter 1 [--force]
python execution/fetch_qa_transcript.py --list-sources
```

## Verification

- [ ] `[done] ... source=aggregator_<name> qa=ok` printed.
- [ ] `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` opens with the `=== SYNTHESIZED QUARTERLY UPDATE — Q&A SEGMENT ONLY ===` banner.
- [ ] First narrative line begins at the operator's Q&A-start cue, last narrative line ends at the operator's call-conclude cue.
- [ ] Speaker turns appear as separate paragraphs (e.g. `B Bipul Sinha\n\nF Fatima Boolani\n\n...`).
- [ ] Re-running with the same args without `--force` prints `[skip] ... transcript file already exists`.
- [ ] `python execution/qa_transcripts.py --report --ticker <T>` shows `qa=ok` on the new entry.

## Edge cases

- **Fiscal-year tickers (RBRK, VEEV)**: roic.ai uses fiscal-year labelling. Pass `--year` and `--quarter` as the company reports them (e.g. RBRK Q4 FY26 = `--year 2026 --quarter 4`).
- **Most-recent quarter not yet indexed**: aggregators typically update within 12-48h of the call. If the call was within the last day, expect `[miss]` and fall back to `fetch_audio_transcripts.py`.
- **Speaker-tag formatting differs across sources**: roic.ai uses `<Letter> <FullName>` (e.g. `B Bipul Sinha`); stockanalysis uses uppercase names. Both are preserved verbatim — the say-do pipeline can normalise downstream.
- **Page footer leakage**: handled by the end-cue trim. If a new aggregator is added that uses a different end-of-call template, extend `QA_TAIL_RE` in `src/aggregator_sources.py`.
