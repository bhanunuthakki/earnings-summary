"""
src/llm/style.py
----------------
Cross-prompt presentation rules — the single source of truth for how LLM
briefs format numbers.

Today only the holding-scorecard's ``_HARD_RULES_BLOCK`` (in ``llm_client.py``)
constrains the model's output shape, and only for two prompts. Every other
brief generator — per-quarter summary, press release, presentation, event,
recent developments, pairwise SayDo, bear case, scorecard pass A/B,
valuation basis, earnings-tone diff, and the ~14 synthesis lenses —
hand-rolls its prompt and inherits no consistent convention for decimals,
magnitude suffixes, percentage-vs-bps disambiguation, or signed-delta
direction.

This module exports:

  - ``NUMBER_FORMATTING_BLOCK`` — the rule body. Spliced inline into the
    11 brief generators in ``llm_client.py`` + the earnings-tone Jinja
    template + the exec-comp alignment prompt.

  - ``compose_brief_prompt(body)`` — append-the-block helper for callers
    that prefer not to thread an inline splice. Used by the lens runners
    in ``src/synthesis/lenses/`` so all 14 lenses (and any future ones)
    pick up the rule with a single point of change.

  - ``style_block_cache_token()`` — stable cache-key token. Lens runners
    add this to ``cache_inputs`` so a block edit invalidates cached
    artifacts automatically (no manual ``prompt_version`` bump required).

Pure-text prompts (qa_topics, saydo_filter, company_description,
platform_diagram, transcript_metadata) skip the block — they rarely emit
numbers and the prompt-budget cost (~600 chars) is not justified.

Edit policy:
- Tighten or extend the rules here; do NOT fork per-generator copies.
- A material edit changes the hash returned by ``style_block_cache_token``,
  which downstream lens runners include in their cache key, so cached
  lens artifacts will regenerate on the next run. For the inline-spliced
  prompts (llm_client.py + earnings-tone Jinja), the rendered prompt text
  itself feeds the artifact-store SHA, so invalidation is automatic there
  too — no separate version bump anywhere.
"""

from __future__ import annotations

import hashlib

# Global number-formatting rule for all brief generators.
#
# Mirrors the conventions a senior buy-side analyst would write to in a
# memo: signed deltas, explicit comparison bases, magnitude suffixes on
# levels, bps for sub-1pp moves. The block is prose-style (not a JSON
# schema) so it composes cleanly with the existing markdown prompts.
NUMBER_FORMATTING_BLOCK = """**Number formatting (apply consistently throughout):**

- Percentages: 1 decimal, "%" suffix, signed for deltas — "12.3%", "+5.2% YoY", "−180 bps".
- Use **bps** for sub-1pp moves ("+45 bps"), **pp** for 1pp+ rate-on-rate moves ("+1.2 pp NIM"),
  **%** for growth rates.
- Dollar levels: magnitude suffix + 1 decimal — "$1.4T", "$12.3B", "$847M", "$45K".
  Sub-$1M → integer dollars ("$450,000"). Never raw "$2,300,000,000" or unitless "$2.3".
- Unit counts (subs, units, employees): same suffix scheme without "$" — "1.4B users", "12.3M subs".
- Multiples: 1 decimal + "x" — "23.5x EV/EBITDA", "8.2x P/E".
- Per-share metrics: 2 decimals — "$1.23 EPS", "$4.56 FCF/share".
- Every number carries a unit AND a comparison base where it's a change ("+12.3% YoY", not "+12.3%").
- Distinguish levels (Revenue = $50.2B) from changes (Revenue growth = +12.3% YoY).
"""


def compose_brief_prompt(body: str) -> str:
    """Append ``NUMBER_FORMATTING_BLOCK`` to a rendered prompt ``body``.

    Convenience entry point for callers that produce a brief but do not
    want to thread an inline ``{NUMBER_FORMATTING_BLOCK}`` splice through
    their prompt template — most notably the synthesis lens runners,
    where each lens's prompt template is opaque to the runner.

    Appending (rather than prepending) keeps the formatting rule as the
    last instruction the model sees, which empirically improves rule
    adherence vs. burying it in a long preamble. The ``---`` separator
    keeps the block visually distinct in raw prompt logs.
    """
    return f"{body.rstrip()}\n\n---\n\n{NUMBER_FORMATTING_BLOCK}"


def style_block_cache_token() -> str:
    """Return a stable cache-key token for ``NUMBER_FORMATTING_BLOCK``.

    Callers that key cached LLM artifacts on an input-SHA list (lens
    runner, earnings-tone trigger, anything that uses
    ``llm_artifact_store.compute_input_sha256``) should append this token
    to ``cache_inputs`` so that editing the block invalidates every
    cached brief on the next run — no per-caller ``prompt_version`` bump
    required.

    Truncated to 16 hex chars (still 2**64 collision-resistant for this
    use) so the token reads cleanly in debug logs.
    """
    digest = hashlib.sha256(NUMBER_FORMATTING_BLOCK.encode("utf-8")).hexdigest()[:16]
    return f"style_block_sha={digest}"


__all__ = [
    "NUMBER_FORMATTING_BLOCK",
    "compose_brief_prompt",
    "style_block_cache_token",
]
