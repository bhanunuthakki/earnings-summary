"""The canonical, versioned owner process-context substrate.

Two seams:

  models — Pydantic shapes for retained next-dollar weights and explicit
           behavioral rules.
  store  — versioned persistence in ``owner_profile_facts`` (alembic 0159):
           append-and-supersede, exactly one ``is_latest`` row per
           (user_id, category, key). Gated assertion (§7.1): every row lands
           at the status the caller passes; only :func:`store.affirm_fact`
           promotes 'proposed' -> 'affirmed', and that always requires an
           explicit owner action.

The package contains no LLM surface or prompt-injection wiring. Prompt callers
select only explicit appetite and behavioral facts for owner-process context.
"""
