# Directive: Process Earnings Transcripts

## Goal

Run one or more earnings call transcript PDFs through the full pipeline to produce a **company-level Master PDF** for each ticker. The output contains a clickable Table of Contents, a pairwise Say-Do strategic analysis (when ≥ 2 quarters are present), per-quarter cover pages, 1–2 page LLM-generated summaries, and the full original transcript.

## Inputs

| Input | Where |
|---|---|
| PDF transcripts (one per quarter) | `transcripts_in/` |
| Gemini API key | `.env` → `GEMINI_API_KEY` |
| (Optional) existing cache | `cache/` |

**Filename format**: `Company_Qx_YYYY.pdf` (e.g. `NVDA_Q2_2024.pdf`).  
The `smart_rename_files()` pre-pass in `src/parser.py` will attempt to auto-rename files that don't match this pattern using the LLM.

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary pipeline** | `execution/run_pipeline.py` |
| Verify API key & models | `python check_models.py` |

### run_pipeline.py responsibilities
`execution/run_pipeline.py` is a thin wrapper that activates the venv and delegates to `src/main.py`. It should:
1. Activate the virtualenv (`venv/`)
2. Set `PYTHONPATH` to `src/`
3. Run `python src/main.py`
4. Print a clear success/failure message

## Pipeline Steps (inside `src/main.py`)

1. **Auto-Rename** — `smart_rename_files(INPUT_DIR)` using LLM if filenames don't match pattern
2. **Ingest** — Move PDFs from `transcripts_in/` → `transcripts_processed/`
3. **Group by Company** — Sort by `(company, year, quarter)`, group per ticker
4. **Manifest Check** — Compare `cache/<Company>_manifest.json` against current file set + mtimes; skip companies that are up-to-date
5. **Per-Quarter Processing** (for each quarter):
   - Extract text from PDF (`src/parser.py`)
   - Generate or load cached 1–2 page summary (`cache/<Co>_<Q>_<Y>_summary.txt`)
   - Create cover page and summary PDF in `.tmp/`
   - Wait 30 s between fresh Gemini calls (rate-limit guard)
6. **Pairwise Strategic Analysis** (if ≥ 2 quarters):
   - For each consecutive pair, generate or load `cache/SayDo_<Co>_<Qprev>_<Yprev>_<Qcurr>_<Ycurr>.txt`
   - Compile into `.tmp/<Company>_strategic_analysis.pdf`
7. **Assemble Master PDF**:
   - Merge: strategic analysis → cover + summary + transcript (per quarter)
   - Prepend a clickable ToC PDF
   - Write final to `transcripts_master/<Company>_Master_Transcripts.pdf`
8. **Save Manifest** — Record file list + mtimes to `cache/<Company>_manifest.json`

## Outputs

| Artifact | Location |
|---|---|
| Master PDFs | `transcripts_master/<Company>_Master_Transcripts.pdf` |
| Summary cache | `cache/<Company>_<Q>_<Y>_summary.txt` |
| Analysis cache | `cache/SayDo_<Company>_…_….txt` |
| Manifest | `cache/<Company>_manifest.json` |
| Intermediates | `.tmp/` (can be deleted and regenerated) |

## Edge Cases & Known Constraints

- **Rate limits**: Gemini free-tier is ~15 RPM. A `time.sleep(30)` is inserted between fresh summary generations. If you hit quota errors (`429`), increase the sleep or switch to a paid plan.
- **Filename mismatch**: Files not matching `Company_Qx_YYYY.pdf` will attempt auto-rename via LLM. If that fails they are skipped with a warning.
- **Single-quarter companies**: Strategic (pairwise) analysis is skipped; only the per-quarter summary is included.
- **Manifest corruption**: If `cache/<Co>_manifest.json` is unreadable, the company is fully rebuilt.
- **venv dependency**: `execution/run_pipeline.py` expects `venv/` at the project root. If missing, run `python -m venv venv && venv\Scripts\pip install -r requirements.txt` first.

## Verification

After running, confirm:
- [ ] `transcripts_master/<Company>_Master_Transcripts.pdf` exists and is readable
- [ ] No files remain in `transcripts_in/` (all moved to `transcripts_processed/`)
- [ ] `cache/<Company>_manifest.json` is updated
- [ ] Re-running the pipeline immediately prints "Skipping <Company> (Up to date)." confirming the manifest cache works
