# Direct agent logging

Use the existing project commands to record an owner-approved outcome from chat.
Resolve the canonical Windows database before operational use. On development
machines, use only an explicit disposable fixture or approved restored snapshot.
No command may infer live state from `data/portfolio.db` in a checkout.

| Outcome | Command |
|---|---|
| Note | `python execution/land_session_notes.py musing --db <configured-db> --text "..." --session-ref <id>` |
| Intent resolution | `python execution/land_session_notes.py close-intent --db <configured-db> --ref <ref> --verdict <verdict> --reason "..."` |
| Decision | `python execution/land_session_notes.py decision --db <configured-db> --checkpoint-payload <typed-checkpoint.json>` |
| Decision checkpoint through the dedicated writer | `python execution/record_owner_decision_checkpoint.py --db <configured-db> --payload <typed-checkpoint.json>` |

The typed checkpoint must satisfy `OwnerDecisionCheckpointPayload`. The session
bridge requires `source_channel='claude_session'`. Confirmation writes its linked
decisions and sizing intents atomically and is idempotent. Use the existing thesis
and owner-profile write interfaces for explicit thesis and process changes.

Landing notes and checkpoints makes no LLM call. Whole-session transcript capture,
automatic session distillation, Telegram capture, and automatic belief adoption
are retired. Do not store the full chat as a replacement history library.

`execution/session_context_pack.py` remains a read-only context interface. Its
output is evidence for the agent; it does not grant collection, generation, or
production-write authority.
