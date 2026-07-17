"""The canonical, versioned owner-context substrate (tenet-2 Phase 1).

Two seams:

  models — per-fact-kind Pydantic value shapes (``TaxBucketBalances``,
           ``GlidePosture``, ``LifeEventFact``, ``HumanCapitalBucket``, ...)
           a producer validates against before persisting. Deliberately no
           money/comp fields anywhere in this package — decision 1
           (docs/design/tenet2_advisory_program.md §7): the wealthplan import
           is derived summaries only.
  store  — versioned persistence in ``owner_profile_facts`` (alembic 0159):
           append-and-supersede, exactly one ``is_latest`` row per
           (user_id, category, key). Gated assertion (§7.1): every row lands
           at the status the caller passes; only :func:`store.affirm_fact`
           promotes 'proposed' -> 'affirmed', and that always requires an
           explicit owner action (the packet-walk ratification card).

This package contains no LLM surface and no prompt-injection wiring — Phase 1
builds the substrate only; the anchor slot and PreAnalysis capacity block are
Phase 2 (§6 of the strategy doc).
"""
