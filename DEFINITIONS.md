# Definitions

Canonical terminology for this project. Use these terms verbatim in code (variables, functions, types, columns), comments, commit messages, and PR descriptions. New domain terms must be added here before being used.

## Thought Partner

**Definition.** The program's operating identity — a living system that extracts, explores (Socratically), synthesizes, and learns a user Worldview over time; it treats captures as raw material for thinking, not records to file. Storage is the last step, not the product.
**Lives in.** Cross-cutting identity, realized by the capture → explore → distil → Worldview pipeline (`src/capture/`, the On My Mind feed, `src/synthesis/`, `src/llm/anchors.py`).
**Not to be confused with.** The per-ticker analyst workspace (the HTML report deliverable) — that is the *output* of analysis, not the thinking loop.
**Subsumes.** Informal "assistant" / "chatbot" / "CRUD app" descriptions of the program.

## On My Mind

**Definition.** The reverse-chronological living feed of what the analyst is currently thinking about and reading — each item indexed to themes, holdings, and overall positioning, carrying the action ladder **dismiss · save-for-later · discuss · incorporate-into-research**. The front-of-funnel where the LLM extracts and explores *before* anything is distilled.
**Lives in.** (to be built) the capture feed surfaced in Telegram and the dashboard notecard/library; a read model over `analyst_notes` (`source='capture'`); feeds the Worldview.
**Not to be confused with.** The Worldview (durable, synthesized) — On My Mind is transient working memory that feeds it.
**Subsumes.** The **Wondering** flag and its detection (`wondering_detect`, flag `LEDGER_RESEARCH_TAP`). On My Mind is strictly broader — reading and exploration, not just self-posed questions — and absorbs it.

## Worldview

**Definition.** The durable, evolving model of how the analyst thinks — the synthesized set of Tenets that subtly conditions investment reasoning (hold / add / trim / sell / evaluate).
**Lives in.** (to be built) a durable tenets store; injected into thesis / ask / decision reasoning via the anchor mechanism (`src/llm/anchors.py`).
**Not to be confused with.** A per-ticker thesis (company-specific, in `micro_thesis/holdings/`) — the Worldview is cross-company, about the analyst's *own* reasoning.
**Subsumes.** The merged `influence` analyst-notes kind (PR #701), which is superseded by Tenets.

## Tenet

**Definition.** A single revisable belief-unit in the Worldview — a principle about *how the analyst invests* — with provenance to the insights that formed it; the system proposes revisions the analyst approves and flags contradictions when a new insight conflicts with a standing Tenet.
**Lives in.** (to be built) `insight_notes` with `kind='tenet'`; composes the Worldview.
**Not to be confused with.** A **conviction** (see below) — a `conviction` is a *1–5 confidence rating on a position/decision* (`bucket_for_conviction`, conviction calibration/Brier in `src/advisor/`, and the `conviction` field on `decision_capture`). A Tenet is a cross-company belief about *method*, not a confidence level on a name. Also distinct from a `musing` (an in-the-moment captured thought) and an `insight_note` of `kind='theme'` (a topic cluster, not a belief).
**Subsumes.** — (was proposed as "Conviction" 2026-07-01; renamed to avoid collision with the entrenched `conviction` rating.)

## conviction (rating)

**Definition.** A 1–5 confidence score the analyst assigns to a position/stance/decision, used for calibration (hit-rate by conviction bucket, Brier scoring).
**Lives in.** `src/advisor/context.py`, `src/advisor/memos.py`, the sizing-audit conviction column, `src/research/decision_capture.py` (`conviction` field).
**Not to be confused with.** A **Tenet** (a Worldview belief-unit). Lowercase `conviction` = a rating; a Tenet = a belief.
