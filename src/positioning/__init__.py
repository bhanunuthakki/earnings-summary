"""The owner's durable portfolio positioning — targets the fit math scores against.

Three seams:

  profile — :class:`~positioning.profile.PositioningProfile`, the Pydantic
            boundary model for the encoded quantitative targets (growth/value
            tilt, sector over/under-weights, vol posture, sleeves, life
            circumstances carried verbatim).
  store   — versioned persistence in ``positioning_intents`` (alembic 0145):
            append-and-supersede, exactly one ``is_latest`` per user; the
            prior saved state stays authoritative until new views land.
  target  — :func:`~positioning.target.resolve_target_context`, the single
            resolver Fit v2 and the Positioning panel consume. **Default =
            current book**: with no saved intent, every target equals the
            book's current reading, so target-relative factors read neutral
            and today's behavior is preserved exactly.

The conversational coach that authors profiles (socratic push-back, grounding
in live book data, owner-approved encoding) arrives in the next phase; this
package deliberately contains no LLM surface.
"""
