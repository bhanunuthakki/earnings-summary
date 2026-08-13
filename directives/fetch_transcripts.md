# Directive: Fetch Earnings Transcripts

## Goal

Audio/webcast retrieval is retired and denied before network access. Text transcripts are acquired from policy-authorized issuer or aggregator sources; existing audio-transcript artifacts remain readable for provenance and QA only.

## Input modes

`execution/fetch_audio_transcripts.py` retains its legacy CLI shape only to fail closed with the canonical `webcast_excluded` policy. It does not search, download, or transcribe.

| Mode | Flags | Use when |
|---|---|---|
| **Curated URL** | `--ticker --year --quarter --url <YT_URL>` | You have a verified link (preferred — bypasses search) |
| **Manifest batch** | `--links-file <path> [--only-ticker T]` | Bulk run across many quarters (see `.tmp/youtube_earnings_links.json`) |
| **Smart search** | `--ticker --year --quarter` | No curated link; falls back to `ytsearch5` + scoring |

The smart search replaces the previous naive `ytsearch1`. It enumerates 5 candidates and scores them; rejects anything outside the 25–130 min duration band, anything missing the ticker / quarter / year token in the title, and anything containing `analysis`, `recap`, `highlights`, `breakdown`, `review`, etc. (analyst-commentary indicators). If no candidate qualifies, it raises rather than guessing — at that point, curate the URL into the manifest.

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary Fetcher & Transcriber** | `execution/fetch_audio_transcripts.py` |
| **Fallback when no YouTube audio exists** | Ask the user to drop a transcript PDF into `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` (or `.pdf`). The brief's §5 Earnings section will skip the quarter cleanly if neither exists. |

### fetch_audio_transcripts.py Responsibilities

1. Validates its inputs via Pydantic (`FetchSpec`, `LinksManifest`).
2. **Skip-existing (automatic, QA-gated)** — if `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` already exists:
   - Ensure it has an index entry (register a stub with `source=unknown_legacy` if missing).
   - If `qa_status` is unset, run `validate_transcript` now and persist the result.
   - If `qa_status=ok` → print `[skip]` and return without re-downloading.
   - If `qa_status=failed` → print `[skip-failed-qa]` with issue list; user must delete the file to retry.
3. Resolves the YouTube URL (curated → manifest → smart search).
4. Downloads audio via `yt-dlp` to `.tmp/temp_audio_<TICKER>_<Q>_<YEAR>.<ext>`.
5. Transcribes via `faster-whisper` (defaults `distil-large-v3` + `beam_size=1`) → `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt`.
6. **QA gate (automatic)** — runs `validate_audio_transcript` on the produced file and records `qa_status` + `qa_details` in the index:
   - `qa=ok` → cached audio at `.tmp/temp_audio_*.<ext>` is **deleted**; print `[done] ... qa=ok audio_cleaned`.
   - `qa=failed` → cached audio is **kept** so the user can rerun with a different `--whisper-model` / `--beam-size` without re-downloading; print `[done-qa-failed]` with issue list.
7. Index entry source label is one of:
   - `source=yt_dlp_whisper_url` (explicit URL)
   - `source=yt_dlp_whisper_links` (manifest)
   - `source=yt_dlp_whisper_search` (smart search)
   - `source=unknown_legacy` (file existed before any source was recorded; backfilled by skip-existing)

### Links manifest schema

`.tmp/youtube_earnings_links.json` (or any path passed to `--links-file`):

```json
{
  "_meta": { "...": "free-form" },
  "links": [
    {
      "ticker": "NOW",
      "year": 2025,
      "quarter": 4,
      "url": "https://www.youtube.com/watch?v=...",
      "title": "optional"
    },
    {
      "ticker": "NVO",
      "year": 2025,
      "quarter": 4,
      "url": null,
      "gap_reason": "Not on YouTube; ask user to drop a transcript PDF"
    }
  ]
}
```

Entries with `url: null` are skipped with the `gap_reason` printed — they are the trigger to ask the user to drop a transcript PDF into `transcripts/raw/`.

## Outputs

| Artifact | Location |
|---|---|
| Raw Transcript Text | `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` |
| Index Update | `.tmp/transcript_index.json` (includes `has_qa: true\|false\|null`) |

## Q&A Section Detection

Every transcribed file is inspected for analyst Q&A content immediately after Whisper finishes. Detection is structural — three regex signals against established transcript conventions:

1. CallStreet/FactSet `QUESTION AND ANSWER SECTION` header.
2. CallStreet `<Q – ...>` analyst-question speaker tag.
3. ≥2 operator analyst-introduction phrases (`next question is from X`, `first question comes from Y`).

Outcomes:

| `has_qa` | Meaning | What to do |
|---|---|---|
| `true` | Q&A present | Proceed normally. |
| `false` | Prepared remarks only | Fetcher prints `WARN missing_qa: ...` to stderr and the batch summary lists the affected (ticker, quarter). Either re-pull from a longer YouTube source or ask the user to drop a full-call PDF into `transcripts/raw/`. |
| `null` | Text too short to determine (<2 KB) | Treat as broken file; investigate. |

The same verdict is also persisted on `transcripts.has_qa_section` during ingest (see migration `0019_transcripts_has_qa_section`), so downstream extractors (Say-Do, commitments) can filter to `has_qa_section = 1` rows when analyst-question content is required.

## Edge Cases & Known Constraints

- **Hardware Limits**: defaults are `distil-large-v3` + `beam_size=1` (greedy) on CPU/int8 — roughly 20–30 min per 60-min call. Override with `--whisper-model` / `--beam-size` (or `WHISPER_MODEL` / `WHISPER_BEAM_SIZE` env vars) when you need a higher-WER model. The original `large-v3-turbo` + `beam_size=5` was ~6-8× slower for marginal accuracy gain.
- **ffmpeg required**: yt-dlp needs it for audio extraction; faster-whisper for decode. On Windows where it isn't on PATH, set `FFMPEG_LOCATION` env var or pass `--ffmpeg-location <DIR>`. Default Windows fallback is `C:\ffmpeg\bin`.
- **Dynamic file extensions**: yt-dlp output may be `.m4a`, `.webm`, etc.; the script discovers the produced file by basename match (largest if multiple, in case of `.part` leftovers).
- **First-run model download**: `large-v3-turbo` (~1.6 GB) is fetched on first invocation and cached in the user's HF cache.
- **Manual uploads**: drop `.mp3` / `.m4a` files into `transcripts/raw/` and re-run the fetcher; its skip-existing path will transcribe them in place.

## QA validation

QA logic lives in `src/transcript_qa.py`. Defaults are tuned against the May 2026 backfill of 24 known-good transcripts. Tighten via the constants at the top of that module; rerun `python execution/qa_transcripts.py --rerun-all` after any change.

| Check (audio transcripts) | Threshold |
|---|---|
| File size | ≥ 10 KB |
| Line count | ≥ 100 |
| Timestamped fraction | ≥ 95 % |
| Duration covered (max-end − min-start) | ≥ 10 min |
| Words / second | 0.5 – 5.0 |
| Adjacent-repeat ratio (Whisper hallucination signal) | ≤ 30 % |

(Synthesized transcripts produced by older pipelines used a separate validator — that path was retired in the cleanup; see git history for `synthesize_quarterly_update.py` if you need that QA spec.)

## Verification

After running, confirm:
- [ ] `.txt` transcript file appears in `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt`.
- [ ] Re-running the fetcher with the same args prints `[skip] ... qa=ok` and does not re-download.
- [ ] `.tmp/transcript_index.json` entry has correct `source` label, `qa_status=ok`, and `qa_details` populated.
- [ ] No `.tmp/temp_audio_<TICKER>_Q<N>_<YEAR>.*` file remains after a successful run.
