# Directive: Fetch Earnings Transcripts

## Goal

Automate the retrieval of company earnings call audio and transcribe it locally to generate text transcripts. This bypasses anti-bot protected sites and paid APIs by pulling audio from YouTube and running private, cost-free transcription via `faster-whisper`. The fetched transcripts will be processed and formatted into the ingestion pipeline.

## Inputs

| Input | Where |
|---|---|
| Ticker | `--ticker <SYMBOL>` |
| Year | `--year <YYYY>` |
| Quarter | `--quarter <1-4>` |

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary Fetcher & Transcriber** | `execution/fetch_audio_transcripts.py` |

### fetch_audio_transcripts.py Responsibilities
`execution/fetch_audio_transcripts.py` is a Layer 3 executable that:
1. Validates its inputs (Ticker, Year, Quarter).
2. Checks `.tmp/transcript_index.json` or `transcripts/raw/` to verify if the transcript already exists locally to avoid redundant processing.
3. Uses `yt-dlp` to search YouTube for the exact earnings call audio (e.g. `<Ticker> Q<Quarter> <Year> earnings call audio full`) and downloads the raw audio file.
4. Locally loads the `faster-whisper` `large-v3-turbo` model and transcribes the audio directly on the CPU.
5. Saves the resulting raw text into `transcripts/raw/<Ticker>_Q<Quarter>_<Year>.txt`.
6. Registers the new transcript in `.tmp/transcript_index.json` under `source=yt_dlp_whisper`.

## Outputs

| Artifact | Location |
|---|---|
| Raw Transcript Text | `transcripts/raw/<Company>_Q<Quarter>_<Year>.txt` |
| Index Update | `.tmp/transcript_index.json` |

## Edge Cases & Known Constraints

- **Hardware Limits**: `faster-whisper` uses intensive CPU/memory resources. It is configured to use `int8` precision on `cpu` for compatibility, which usually takes several minutes per transcript.
- **Dynamic File Extensions**: Audio files downloaded via `yt-dlp` may be `.m4a` or `.webm`. The script dynamically identifies the downloaded file before passing it to the transcriber.
- **Dual-Mode Sync**: The `main.py` ingestion pipeline also handles direct manual audio uploads (`.mp3`, `.m4a`) from the web interface, directly invoking `faster-whisper` to create the `.txt` transcript.

## Verification

After running, confirm:
- [ ] Appropriate `.txt` transcript file appears in `transcripts/raw/`.
- [ ] Running the fetcher again immediately for the same Ticker/Year/Quarter prints `Transcript already exists` and skips downloading securely.
