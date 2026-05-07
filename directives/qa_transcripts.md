# Directive: QA Validation + Audio-Cache Cleanup for Transcripts

## Goal

Provide a single source of truth for whether a transcript in `transcripts/raw/` is structurally sound, and gate cached-audio cleanup on that truth. The fetcher and synthesizer call this on every run; the standalone CLI exists for retroactive backfills, threshold tightening, and orphan-audio cleanup.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Validators (library) | `src/transcript_qa.py` |
| CLI (backfill / report / clean) | `execution/qa_transcripts.py` |

## Validator dispatch

`validate_transcript(path, source)` routes by registered source:

| Source label | Validator | Notes |
|---|---|---|
| `yt_dlp_whisper_*` / `unknown_legacy` | `validate_audio_transcript` | Expects `[start s -> end s] text` per line |
| `synthesized_text` | `validate_synthesized_transcript` | Expects banner header + at least one `=== SECTION ===` |

Both return a `QaResult` with `status: ok | failed`, structural metrics, and an `issues: list[str]` of what failed (empty when status=ok).

## Audio-transcript thresholds

Tuned against the 24 May 2026 backfill transcripts. Edit constants at the top of `src/transcript_qa.py` to tighten/relax; then run `python execution/qa_transcripts.py --rerun-all`.

| Check | Threshold | Failure signal |
|---|---|---|
| File size | ≥ 10 KB | Empty / truncated output |
| Line count | ≥ 100 | Whisper crashed mid-call |
| Timestamped fraction | ≥ 95 % of lines | Output corrupted with non-segment text |
| Duration covered | ≥ 10 min (max-end − min-start) | Audio cut off / wrong video |
| Words per second | 0.5 – 5.0 | Silence (low) or babble/hallucination (high) |
| Adjacent-repeat ratio | ≤ 30 % | Whisper's known repetition-loop failure mode |

## Synthesized-transcript thresholds

| Check | Threshold | Failure signal |
|---|---|---|
| File size | ≥ 10 KB | Sources fetched but yielded no text |
| Header banner present | First line begins with `=== SYNTHESIZED QUARTERLY UPDATE` | Wrong writer / corruption |
| Section count | ≥ 1 real section beyond header | All inputs failed silently |

## CLI modes

```
python execution/qa_transcripts.py [--ticker T] [--backfill] [--rerun-all]
                                   [--clean-orphan-audio] [--report]
```

| Flag | Behaviour |
|---|---|
| (none) | Default: `--backfill --clean-orphan-audio` (idempotent automatic mode) |
| `--backfill` | Run QA on transcripts whose index entry has no `qa_status` |
| `--rerun-all` | Re-validate every transcript regardless of prior status |
| `--ticker SYMBOL` | Filter every mode to one ticker |
| `--clean-orphan-audio` | Delete `.tmp/temp_audio_<...>.<ext>` whose transcript is `qa=ok`; keep audio for `qa=failed` so the user can rerun Whisper without re-downloading |
| `--report` | Print a status table for every indexed transcript |

## Outputs

| Artifact | Where | Shape |
|---|---|---|
| Per-transcript QA fields | `.tmp/transcript_index.json[KEY]` | `qa_status`, `qa_details` (full `QaResult`), `qa_checked_at` |
| Cached audio | `.tmp/temp_audio_<TICKER>_Q<N>_<YEAR>.<ext>` | Present iff QA failed or transcribe in flight; cleaned otherwise |

## Operational rules

- **Skip-existing is QA-aware.** `fetch_audio_transcripts.py` will not re-transcribe a file that already exists and is `qa=ok`; it will refuse to overwrite a `qa=failed` file (delete the file by hand to retry).
- **Audio cleanup is QA-gated.** Successful Whisper runs delete their `.tmp/temp_audio_*` immediately. Failed runs preserve the audio so the operator can rerun with a stronger model or higher beam size without paying the YouTube round-trip again.
- **Backfill is automatic.** The first time the fetcher (or the standalone CLI) sees a transcript without `qa_status`, it validates and records — no manual step.
- **Threshold changes require `--rerun-all`.** Editing constants in `src/transcript_qa.py` does not auto-revalidate; the recorded statuses persist until you ask for re-evaluation.

## Verification

After running `python execution/qa_transcripts.py`:
- [ ] `[qa-summary]` reports `failed=0` (or surfaces specific issues to investigate).
- [ ] `[clean-summary]` reports `deleted` matching count of pre-existing `qa=ok` orphans, `kept=0`.
- [ ] Re-running immediately reports `skipped=N` matching the transcript count and `deleted=0`.
- [ ] `--report` for any ticker lists every quarter with `qa=ok` and `0` issues.
