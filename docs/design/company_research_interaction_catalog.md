# Company Research Interaction Catalog

This catalog is the front-end-to-backend contract for the company research mockup. It prevents an attractive control from implying that a safe backend action already exists. The current mockup remains static: no prototype action writes to the database or runs a research pipeline.

## Classification

- **Ready now** — an existing read-only or conversational seam can support the action without inventing a write path.
- **Adapter needed** — the underlying backend operation exists, but the surface needs durable identity, context, or revision metadata before it can call it safely.
- **New governed capability** — the product needs a new preview-and-approve contract. The UI must not bypass the module that currently owns validation and persistence.
- **Owner approval** — any mutation remains a proposed change until the human owner explicitly confirms it. LLM analysis may draft or explain; it cannot silently ratify.

## Capability matrix

| Capability ID | Surface and UI behavior | Classification | Existing seam | Required linkage or backend work |
|---|---|---|---|---|
| `research.change_feed.chat` | **What changed** card → contextual Chat opens Ask with the company and evidence-delta prompt prefilled. | Ready now | `src/pipeline/ask_dock.py`; `POST /api/ask/stream` in `execution/comments_server.py`. | Pass `ticker`, `capability_id`, card label, and the selected evidence identifiers in `context_spec`; preserve exact source doorways in the answer. |
| `research.thesis_contracts.chat` | **Thesis contracts / falsifiers** card → Chat asks about the nearest break, supporting evidence, or counterargument. | Ready now | Ask dock and `/api/ask/stream`; contract context is already part of the research corpus. | Add the decision/contract identifiers to the request so the answer is anchored to the visible contracts rather than ticker-only context. |
| `research.thesis_contracts.edit` | **Thesis contracts / falsifiers** card → Edit opens a proposal surface, shows the current contract, and previews a replacement before approval. | New governed capability | `src/decision_conditions.py::attach_conditions()` owns extraction, validation, and stamping. Existing report comments also recognize `edit_thesis` and `edit_structured`. | Add a preview endpoint and an approve endpoint that reuse the decision-condition owner module. Require decision ID, current revision, proposed text/structured thresholds, rationale, idempotency key, audit record, and Owner approval. Never write `decisions.decision_conditions` directly from the UI. |
| `research.decision_kpis.chat` | **Decision KPIs** card → Chat explains a movement or compares a series with the thesis threshold. | Ready now | Ask dock and `/api/ask/stream`; governed facts already expose source references. | Send the visible `fact_ref` values and date range in `context_spec`; an adapter is optional only for richer exact-series selection. |
| `research.open_questions.chat` | **Open questions** card or question row → Chat starts a scoped tangent without converting it into an approved conclusion. | Ready now | Ask dock and `/api/ask/stream`. | Include `note_id` when available plus the question text, ticker, and card context. A chat response may suggest follow-up work but must not resolve or supersede the note. |
| `research.open_questions.edit` | **Open questions** card or question row → Edit previews revised wording or routing. | Adapter needed | Journal routes expose `/api/notes/<note_id>/supersede`; `src/user_state/notes.py` owns `supersede_note`. | Render durable `note_id` and current revision on each question. Save as a superseding revision, not an in-place overwrite; show the diff and require Owner approval before the call. |
| `research.catalysts.chat` | **Catalyst calendar** card → Chat prepares the event, expected evidence, and action rule. | Ready now | Ask dock and `/api/ask/stream`. | Pass event identity, date, linked decision condition, and ticker in the scoped prompt. Add a durable event ID later if calendar rows are hydrated from more than one source. |
| `research.latest_brief.chat` | **Latest research artifact** card → Chat discusses the current brief in company context. | Ready now | Unified Ask endpoint plus the existing report route `POST /chat/<ticker>`; report changes already use an apply boundary. | Prefer the unified Ask dock for discussion. If a response proposes a report edit, route it into the existing diff/apply workflow rather than mutating the artifact from Chat. |
| `research.capability_catalog.review` | **Interaction review** navigation item → opens an aggregate readiness view of all proposed research actions. | Ready now | Front-end-only catalog view backed by this document. | When hydrated, expose capability status, owner, last verification, blocked dependency, and target release from a small registry rather than duplicating labels in templates. |

## Shared action envelope

Every interactive research control should carry a stable `data-capability` and send the same minimum envelope when connected:

```json
{
  "capability_id": "research.open_questions.edit",
  "ticker": "NU",
  "surface": "company_desk",
  "item_id": "note-or-domain-id",
  "revision": "current-revision",
  "intent": "chat-or-edit"
}
```

Chat is read-only and can open immediately. Edit is a proposed mutation: load the current revision, preview a diff, validate through the owning module, require Owner approval, then persist with an idempotency key and audit event. Stale revisions must fail closed and ask the user to refresh.

## Aggregate review queue

The first implementation pass should review classifications in aggregate rather than building one-off handlers:

1. Connect all **Ready now** Chat actions to the existing Ask dock with scoped `context_spec` payloads.
2. Add note identity and revision metadata, then connect `research.open_questions.edit` to the existing supersede route.
3. Design and test the preview/approve contract for `research.thesis_contracts.edit` with `decision_conditions` as the single write owner.
4. Hydrate the Interaction Review from a registry only after the capability contracts stabilize.
