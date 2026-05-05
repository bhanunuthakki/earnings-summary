# Directive: Fetch Earnings Transcripts

## Goal

Automate the retrieval of company earnings call audio and transcribe it locally to generate text transcripts. This bypasses anti-bot protected sites and paid APIs by pulling audio from YouTube and running private, cost-free transcription via `faster-whisper`. The fetched transcripts will be processed and formatted into the ingestion pipeline.

## Input modes

`execution/fetch_audio_transcripts.py` has three mutually exclusive modes:

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
| **Fallback when no YouTube audio exists** | `execution/synthesize_quarterly_update.py` (see `directives/synthesize_quarterly_update.md`) |

### fetch_audio_transcripts.py Responsibilities

1. Validates its inputs via Pydantic (`FetchSpec`, `LinksManifest`).
2. Checks `.tmp/transcript_index.json` and `transcripts/raw/` to skip if already done.
3. Resolves the YouTube URL (curated → manifest → smart search).
4. Downloads audio via `yt-dlp` to `.tmp/temp_audio_<TICKER>_<Q>_<YEAR>.<ext>`.
5. Transcribes via `faster-whisper` `large-v3-turbo` (CPU, int8) → `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt`.
6. Registers in `.tmp/transcript_index.json` with one of:
   - `source=yt_dlp_whisper_url` (explicit URL)
   - `source=yt_dlp_whisper_links` (manifest)
   - `source=yt_dlp_whisper_search` (smart search)

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
      "gap_reason": "Not on YouTube; route via synthesize_quarterly_update.py"
    }
  ]
}
```

Entries with `url: null` are skipped with the `gap_reason` printed — they are the trigger to invoke the synthesis fallback.

## Outputs

| Artifact | Location |
|---|---|
| Raw Transcript Text | `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` |
| Index Update | `.tmp/transcript_index.json` |

## Edge Cases & Known Constraints

- **Hardware Limits**: defaults are `distil-large-v3` + `beam_size=1` (greedy) on CPU/int8 — roughly 20–30 min per 60-min call. Override with `--whisper-model` / `--beam-size` (or `WHISPER_MODEL` / `WHISPER_BEAM_SIZE` env vars) when you need a higher-WER model. The original `large-v3-turbo` + `beam_size=5` was ~6-8× slower for marginal accuracy gain.
- **ffmpeg required**: yt-dlp needs it for audio extraction; faster-whisper for decode. On Windows where it isn't on PATH, set `FFMPEG_LOCATION` env var or pass `--ffmpeg-location <DIR>`. Default Windows fallback is `C:\ffmpeg\bin`.
- **Dynamic file extensions**: yt-dlp output may be `.m4a`, `.webm`, etc.; the script discovers the produced file by basename match (largest if multiple, in case of `.part` leftovers).
- **First-run model download**: `large-v3-turbo` (~1.6 GB) is fetched on first invocation and cached in the user's HF cache.
- **Dual-Mode Sync**: `src/main.py` ingestion pipeline also handles direct manual `.mp3` / `.m4a` uploads from the web interface.

## Verification

After running, confirm:
- [ ] `.txt` transcript file appears in `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt`.
- [ ] Re-running the fetcher with the same args prints `[skip] transcript already exists` and exits clean.
- [ ] `.tmp/transcript_index.json` contains an entry with the correct `source` label.
