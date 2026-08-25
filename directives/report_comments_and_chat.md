# Report comments and Work OS Copilot contract

**Status:** living contract. Historical proposals, estimates, and pre-coding questions are kept in
[`history/report_comments_and_chat_2026_06_2026_08.md`](history/report_comments_and_chat_2026_06_2026_08.md).

## Current behavior

- Inline comments remain report-scoped and attach to their typed report anchor. Comment intent is
  closed under `needs_triage`; an unknown intent is never silently flattened into a generic note.
- The former JSON-backed in-report composer and `/chat/<ticker>` mutation route are retired.
  Legacy `/chat` endpoints are non-writing `410` tombstones only.
- `workspace_chat.py` hands off to the single SQLite-backed Work OS Copilot at `/api/ask/stream`.
  It may synthesize and propose, but it does not silently mutate thesis, KPI, or report state.
- A thesis or KPI edit is a governed proposal: show the exact diff, require explicit Owner approval,
  preserve its typed target and provenance, and expose loading, failure, retry, and conflict state.
- A commentable computed surface supplies a typed anchor and, where its meaning needs an override,
  routes through the persisted domain owner rather than a render-time special case.

## Ownership boundary

This contract owns comment/Copilot semantics and proposal approval. `interaction_contract.md` owns
shared doorway and overlay laws; `data_provenance.md` owns provenance meaning; and
`operations_governance_surface.md` owns supported operator actions. UI composition remains subject
to `design_language.md`, the shared `frontend-quality` procedure, and executable registry/tests.
