# Directive: Folder Structure

## Goal

To enforce a standardized, deterministic directory hierarchy for data flow and state management. This prevents organic folder sprawl, ensures reproducible pipeline stages, and maintains consistent locations for input and output artifacts.

## Architecture

| Directory | Purpose | Lifecycle / Safety |
|---|---|---|
| `transcripts/raw/` | The entry point for the pipeline. Any manually uploaded or automatically fetched transcripts/audio files land here. | Source material. Safe to delete only after successful processing. |
| `transcripts/processed/` | Archive of raw files that have been successfully parsed, transcribed, or evaluated. | Retained for reference and raw text serving. |
| `transcripts/master/` | Final, consolidated Master PDFs. These are the deliverable artifacts presented to the user. | Long-term storage. Can be rebuilt from `.tmp` and `processed/` if necessary. |
| `.tmp/` | Canonical ephemeral state storage. Used for caches, indexes, temporary audio downloads, JSON status dumps, intermediate PDF building blocks, and test scripts. | Ephemeral. Safe to wipe completely. |
| `execution/` | Isolated, deterministic Python tools that act as Layer 3 executors. No ad-hoc debug scripts. | Source code. Should not be written to programmatically by the pipeline. |
| `directives/` | Layer 1 Intent definitions and rules. | Immutable baseline, updated only manually. |
| `micro_thesis/` | Thesis tracker module: `holdings/` (per-ticker JSON KPI specs), `sources/` (per-ticker document drop folders), and periodic tracker notes. | Working data for the micro-thesis tracking module. |
| `examples/` | Example artifacts and templates (e.g., `examples/dcf/` containing sample Excel DCF models). | Reference material. |
| `src/` | Core application code: Flask app, pipeline main, LLM client, parser, PDF builder, index manager, alias manager. | Source code. |

## Rules

1. **No Sprawl**: Scripts **must not** create intermediate or ad-hoc folders like `transcripts_in`, `cache`, `tmp_tests`, etc. at the project root.
2. **State Management**: Any intermediate data that needs to persist across pipeline stages (e.g. `transcript_index.json`, status caches, summary text, downloaded audio, tickers cache) must be written exclusively to `.tmp/`.
3. **Artifact Promotion**: Files move strictly from `raw/` to `processed/`. The final assembled document goes into `master/`.
4. **Root Hygiene**: The project root should contain only: `.env`, `.gitignore`, `README.md`, `requirements.txt`, and the directories listed above. Test/debug scripts belong in `.tmp/test_scripts/`.
5. **Audio Downloads**: Temporary audio files (from yt-dlp) must download to `.tmp/` and be cleaned up after transcription.
