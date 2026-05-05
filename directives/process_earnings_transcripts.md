# Directive: Process Earnings Transcripts

## Goal

Run one or more earnings call transcripts (or raw audio) through the full pipeline to produce a **company-level Master PDF** for each ticker. The output contains a clickable Table of Contents, a pairwise Say-Do strategic analysis (when ≥ 2 quarters are present), per-quarter cover pages, 1–2 page LLM-generated summaries, and the beautifully formatted full original text transcript.

## Inputs

| Input | Where |
|---|---|
| Transcript files (`.txt`, `.pdf`, `.mp3`, `.m4a`) | `transcripts/raw/` |
| Gemini API key | `.env` → `GEMINI_API_KEY` |
| (Optional) existing cache | `.tmp/` |

**Filename format**: `Company_Qx_YYYY.ext` (e.g. `NVDA_Q2_2024.txt` or `AAPL_Q1_2026.m4a`).  
The `smart_rename_files()` pre-pass in `src/parser.py` will attempt to auto-rename files that don't match this pattern using heuristics or LLM parsing.

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary pipeline** | `execution/run_pipeline.py` |
| Verify API key & models | `python check_models.py` |

### run_pipeline.py responsibilities
`execution/run_pipeline.py` triggers audio fetching and delegates analysis to `src/main.py`. It should:
1. Activate the virtualenv (`venv/`)
2. Call `fetch_audio_transcripts.py` to retrieve audio for the latest quarters if a target company is specified.
3. Run `python src/main.py`
4. Print a clear success/failure message

## Pipeline Steps (inside `src/main.py`)

1. **Auto-Rename** — `smart_rename_files(INPUT_DIR)` using heuristics or LLM if text/PDF filenames don't match pattern
2. **Audio Processing** — Directly transcribes `.mp3`, `.m4a`, `.wav` files via `faster-whisper`, yielding `.txt` files in `transcripts/raw/`. The original audio is archived to `transcripts/processed/`.
3. **Ingest** — Move `.txt` and legacy `.pdf` files from `transcripts/raw/` → `transcripts/processed/`
4. **Manifest Check** — Compare `.tmp/<Company>_manifest.json` against current file set + mtimes; skip companies that are up-to-date
5. **Per-Quarter Processing** (for each quarter):
   - Extract text from `.txt` or `.pdf`
   - Generate or load cached 1–2 page summary (`.tmp/<Co>_<Q>_<Y>_summary.txt`)
   - Create cover page, summary PDF, and full transcript PDF (`pdf_builder.py`) in `.tmp/`
   - Wait 30 s between fresh Gemini calls (rate-limit guard)
4. **Pairwise Strategic Analysis** (if ≥ 2 quarters):
   - For each consecutive pair, generate or load `.tmp/SayDo_<Co>_<Qprev>_<Yprev>_<Qcurr>_<Ycurr>.txt`
   - Compile into `.tmp/<Company>_strategic_analysis.pdf`
7. **Assemble Master PDF**:
   - Merge: strategic analysis → cover + summary + transcript (per quarter)
   - Prepend a clickable ToC PDF
   - Write final to `transcripts/master/<Company>_Master_Transcripts.pdf`
8. **Save Manifest** — Record file list + mtimes to `.tmp/<Company>_manifest.json`

## Outputs

| Artifact | Location |
|---|---|
| Master PDFs | `transcripts/master/<Company>_Master_Transcripts.pdf` |
| Summary cache | `.tmp/<Company>_<Q>_<Y>_summary.txt` |
| Analysis cache | `.tmp/SayDo_<Company>_…_….txt` |
| Manifest | `.tmp/<Company>_manifest.json` |
| Intermediates | `.tmp/` (can be deleted and regenerated) |

## Edge Cases & Known Constraints

- **Local Transcriber Resources**: Transcribing audio takes significant CPU processing time on the `large-v3-turbo` model.
- **Rate limits**: Gemini free-tier is ~15 RPM. A `time.sleep(30)` is inserted between fresh summary generations. 
- **Filename mismatch**: Unformatted text/PDFs will be auto-renamed. Audio files *must* be named intuitively prior to upload since text-extraction pre-parsing is not possible for audio.
- **venv dependency**: Expects `venv/` at the project root with `faster-whisper` and `yt-dlp` installed.

## Verification

After running, confirm:
- [ ] `transcripts/master/<Company>_Master_Transcripts.pdf` exists and contains the locally transcribed raw text section.
- [ ] No files remain in `transcripts/raw/` (all moved to `transcripts/processed/`)
- [ ] `.tmp/<Company>_manifest.json` is updated
