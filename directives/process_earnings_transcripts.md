# Directive: Process Earnings Transcripts

## Goal

Run one or more earnings call transcripts (or raw audio) through the full pipeline to produce a **company-level Master PDF** for each ticker. The output contains a clickable Table of Contents, a pairwise Say-Do strategic analysis (when ≥ 2 quarters are present), per-quarter cover pages, 1–2 page LLM-generated summaries, and the beautifully formatted full original text transcript.

## Analytical Principles (apply to every LLM-generated section)

These principles govern any narrative produced by this pipeline — summaries, Say-Do, strategic analysis, thesis trackers. They are not stylistic suggestions; downstream prompts enforce them and outputs that violate them are defective.

### 1. User-provided data is source of truth
- Materials the user has dropped into the working set (transcripts, 10-Q/10-K excerpts, IR docs, analyst forecasts) take precedence over any web-sourced or model-recalled figure.
- Web/third-party data is supplemental — only used to fill gaps the user's docs do not cover, and must be labeled as such.
- If a web-sourced figure conflicts with the user's data, flag the discrepancy explicitly and default to the user's version. Do not silently merge or average.

### 2. No hallucination — verifiable or absent
- Every number, date, quote, and named event in output must be traceable to a specific source document.
- If a figure is not disclosed in the available sources, write `[not disclosed]`. Do not invent, interpolate, or back-fill from prior knowledge.
- Verbatim quotes must use quotation marks and a source tag. Paraphrases must still cite the source.

### 3. Inline source citation
- Format: `[Source: <doc type>, <period>, <page/section or speaker>]`
  - Examples: `[Source: 10-Q, Q2 FY2026, Item 2 – MD&A]`, `[Source: Q4 2025 Transcript, CFO prepared remarks]`, `[Source: IR Press Release, 2026-02-14]`
- Cite at the point of claim, not just in a footer block.
- Tables: cite per row when sources differ, or once in a "Sources" column.

### 4. Adversarial loop on every material claim
Any judgment that drives a verdict, recommendation, or trigger evaluation must be stress-tested before it is asserted. The loop has four steps and every step is required:

```
Primary Thesis     : The claim and the strongest evidence for it (with sources).
Strongest Counter  : The most credible challenge to the claim — alternative
                     reading of the same data, contradicting datapoint, base-rate
                     argument, or management-credibility caveat.
Resolution         : How the two sides reconcile + Net Conviction (High / Medium / Low),
                     and the specific observable that would flip the verdict.
Sensitivity        : Quantified impact if the primary thesis is wrong (±X% on the KPI,
                     valuation, or trigger threshold).
```

A claim with no articulable counter is under-examined, not strong — push harder before asserting it. Apply this loop most rigorously to: (a) thesis status verdicts, (b) Say-Do attribution (execution vs. exogenous), and (c) valuation-trigger / break-condition evaluations.

## Inputs

| Input | Where |
|---|---|
| Transcript files (`.txt`, `.pdf`, `.mp3`, `.m4a`) | `transcripts/raw/` |
| Claude Code CLI (`claude`), authed via `claude auth login` | system PATH |
| (Optional) existing cache | `.tmp/` |

**Auth + billing precondition:** All LLM calls go through `claude -p` (subprocess) so they bill against the user's Claude Pro/Max subscription rather than the metered Anthropic API. `ANTHROPIC_API_KEY` MUST be unset in the runtime env or the CLI silently routes to API billing. `src/llm_client.py` fails loud at first call if either condition is wrong.

**Filename format**: `Company_Qx_YYYY.ext` (e.g. `NVDA_Q2_2024.txt` or `AAPL_Q1_2026.m4a`).  
The `smart_rename_files()` pre-pass in `src/parser.py` will attempt to auto-rename files that don't match this pattern using heuristics or LLM parsing.

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary pipeline** | `execution/run_pipeline.py` |
| Verify Claude CLI is installed + authed (no separate `check_models.py`; the lazy check in `src/llm_client.py` does this) | `claude auth status` |

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
   - Inter-call sleeps (currently 15-30s in scripts) were Gemini-free-tier rate-limit guards. The Claude Code subscription has different limits; these sleeps are now over-conservative — leaving as-is for safety until the user confirms an aggressive cadence is fine, then they can be reduced/removed.
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
- **Rate limits / billing**: All LLM calls route through `claude -p` (subprocess) and bill against the user's Claude Pro/Max subscription. Subscription-tier limits apply (much more permissive than the prior Gemini free-tier ~15 RPM). Existing inter-call sleeps in scripts are vestigial Gemini guards and can be dialed down once cadence is confirmed safe.
- **Filename mismatch**: Unformatted text/PDFs will be auto-renamed. Audio files *must* be named intuitively prior to upload since text-extraction pre-parsing is not possible for audio.
- **venv dependency**: Expects `venv/` at the project root with `faster-whisper` and `yt-dlp` installed.

## Verification

After running, confirm:
- [ ] `transcripts/master/<Company>_Master_Transcripts.pdf` exists and contains the locally transcribed raw text section.
- [ ] No files remain in `transcripts/raw/` (all moved to `transcripts/processed/`)
- [ ] `.tmp/<Company>_manifest.json` is updated
