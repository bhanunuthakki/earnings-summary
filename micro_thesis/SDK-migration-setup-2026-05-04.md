# Claude Code CLI Migration — User Setup

**Date:** 2026-05-04
**Scope:** Final ~5 minutes of work for you (the user) to activate the migration this session built. Everything in code is done — these are the runtime/auth steps that require your hands.

---

## What changed in code

`src/llm_client.py` no longer calls Gemini. All seven LLM functions (`generate_summary`, `generate_press_release_summary`, `generate_presentation_brief`, `generate_pairwise_analysis`, `generate_thesis_update`, `generate_strategic_analysis`, `identify_transcript_metadata`) now route through the Claude Code CLI via `subprocess.run(["claude", "-p", ...])`. Function signatures are preserved — none of the four caller files (`src/main.py`, `src/parser.py`, `execution/process_ir_documents.py`, `execution/update_thesis_tracker.py`) needed edits beyond the staleness-detection wiring already added in step #1.

`requirements.txt` no longer lists `google-generativeai`. The Gemini SDK is no longer a dependency.

---

## Why the Claude Code CLI and not the Claude Agent SDK

The Claude Agent SDK (`claude_agent_sdk` Python package) does NOT support subscription billing — it requires `ANTHROPIC_API_KEY` and bills against the metered API. This was the surprise from this session's research. The Claude Code CLI (`claude -p`) is the only path that bills against your existing Pro/Max plan.

**Critical gotcha**: even the CLI silently falls back to API billing if `ANTHROPIC_API_KEY` is set in the environment. The new `_verify_setup_once()` function in `src/llm_client.py` fails loud at first LLM call if either (a) `claude` is not in PATH or (b) `ANTHROPIC_API_KEY` is set.

---

## What you need to do (3 steps, ~5 min)

### 1. Install the Claude Code CLI

Per https://code.claude.com/docs/en/setup. On Windows the canonical command is:

```bash
npm install -g @anthropic-ai/claude-code
```

Verify install:

```bash
claude --version
```

### 2. Authenticate to your subscription

```bash
claude auth login
```

This opens a browser window. Sign in with the Anthropic account that holds your Claude Pro/Max subscription. After signing in:

```bash
claude auth status
```

Should report you as authenticated against your subscription account.

### 3. Unset `ANTHROPIC_API_KEY` in the shell that runs the pipeline

Your current environment has `ANTHROPIC_API_KEY` set — the lazy check in `src/llm_client.py` confirmed this. If you don't unset it, every CLI call will silently route to API billing instead of your subscription, which is exactly what this whole migration was meant to avoid.

```powershell
# PowerShell
Remove-Item env:ANTHROPIC_API_KEY
```

```bash
# Bash / Git Bash
unset ANTHROPIC_API_KEY
```

To make this permanent, also remove `ANTHROPIC_API_KEY` from any shell startup file (`.bashrc`, `.zshrc`, PowerShell `$PROFILE`) and the `.env` file at the project root.

---

## Smoke test once setup is done

```bash
claude -p "What is 2+2?"
```

Should return "4" or similar. Then:

```bash
python -c "
import sys; sys.path.insert(0, 'src')
import llm_client
print(llm_client._call_claude('Reply with the single word OK and nothing else.'))
"
```

Should print `OK`. If you see the RuntimeError about `ANTHROPIC_API_KEY` or `claude not in PATH`, fix that condition and retry.

---

## Recommended end-to-end validation run

Once the smoke test passes, validate the full pipeline against the patched schemas (after applying the `schema-mismatches-2026-05-04.md` diffs):

```bash
# 1. Confirm the index already has the 24 dropped docs registered (done this session)
python execution/register_dropped_documents.py --dry-run --all
# Should report 0 registered, ~24 skipped_existing.

# 2. LLM-summarize each registered doc (this is the big run; ~24 LLM calls)
python execution/process_ir_documents.py --all

# 3. Generate the 7 thesis trackers using the patched schemas + summaries
python execution/update_thesis_tracker.py --all
```

Expected output: 7 fresh `thesis-tracker-<TICKER>-2026-05-04.md` files (overwriting the session-mode versions from earlier this session). Compare against the session-mode versions to confirm the pipeline produces equivalent quality.

---

## Tunables you may want to adjust later

- **`DEFAULT_MODEL` in `src/llm_client.py`** — currently `claude-sonnet-4-6`. Set to `claude-opus-4-7` for higher-fidelity thesis tracker generation (slower, more usage); `claude-haiku-4-5-20251001` for cheaper summarization (already used for `identify_transcript_metadata`).

- **Per-call rate-limit sleeps** in `execution/process_ir_documents.py` — `RATE_LIMIT_SLEEP = 15` was a Gemini-free-tier guard. Claude Code subscription tolerates higher cadence; reduce to 1–2s once you confirm a full run completes cleanly.

- **`DEFAULT_TIMEOUT_SECONDS`** in `src/llm_client.py` — currently 600s (10 min). Long-context thesis prompts need this much; shorter prompts won't.

---

## What's now complete vs. what remains

| Item | Status |
|---|---|
| 6 prompt-tightening changes to `generate_thesis_update` | ✅ Done in code |
| `register_dropped_documents.py` + 24 docs registered | ✅ Done; index populated |
| Schema-validation pass on 7 holdings → mismatch report | ✅ Done; report at `micro_thesis/schema-mismatches-2026-05-04.md` |
| `src/llm_client.py` migrated from Gemini to Claude CLI | ✅ Done in code |
| Install `claude` CLI + auth + unset `ANTHROPIC_API_KEY` | ❌ Requires you to run the 3 commands above |
| Apply schema patches to 7 holdings JSONs | ❌ Recommended next session — see `micro_thesis/schema-mismatches-2026-05-04.md` for the per-ticker diffs |
| End-to-end pipeline run on patched schemas | ❌ After both of the above |
| GOOG / META / WIX / FLKR coverage | ❌ Requires you to drop docs into `micro_thesis/sources/<TICKER>/` then re-run register + process |
