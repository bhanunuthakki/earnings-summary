"""Output post-processing shared by the brief/memo writers.

``strip_llm_preamble`` removes the model's process narration when it leaks
into a persisted artifact — meta-commentary like *"Having exhausted my web
budget, I now have enough data to write the memo. Here is the brief:"* was
observed verbatim in served product copy (red-team wave B, B8). The prompt
asks for the artifact only; when the model narrates anyway, the writer strips
the lead at PERSIST time (and defensive render paths may re-apply it).

Deliberately conservative: only a SHORT lead (≤2 lines before the first blank
line / heading) whose first words match a first-person process-narration
pattern is dropped. Content that opens with a heading, a claim, or data is
never touched, and the stripper never empties a document.

``strip_inline_markdown`` is the SCALAR-field sibling: LLM extraction paths
were observed persisting markdown inside scalar fields (a Say-Do metric name
stored as ``**Risk-adj. NIM**``, a thesis priority stored as
``**Priority #1 — Mexico momentum**``), which then renders raw in surfaces
that treat scalars as plain text. The rule across persist paths: scalars are
plain, prose/body fields keep markdown (``ui.prose.render_prose`` owns that
boundary). Apply this at persist time, not in prompts — prompts drift.
"""

from __future__ import annotations

import re

__all__ = ["strip_inline_markdown", "strip_llm_preamble"]

# First-person process narration openers ("Having exhausted…", "I now have…",
# "I've gathered…", "Writing from…"). Matched against the lead line only.
_META_LEAD_RX = re.compile(
    r"""^(?:
        having\s |
        i\s+now\s |
        i[\N{RIGHT SINGLE QUOTATION MARK}']ve\s |
        i[\N{RIGHT SINGLE QUOTATION MARK}']ll\s+now\s |
        i\s+will\s+now\s |
        i\s+have\s+(?:now\s+)?(?:enough|gathered|completed|finished|exhausted|reviewed)\b |
        i\s+am\s+now\s |
        i[\N{RIGHT SINGLE QUOTATION MARK}']m\s+now\s |
        now\s+(?:that\s+)?i\s |
        writing\s+from\s |
        based\s+on\s+my\s+(?:search|research|review)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# "Here is the …:" / "Here's the …:" — meta only when the line ENDS with the
# colon (announcing the artifact), so a sentence merely containing the phrase
# survives.
_HERE_IS_RX = re.compile(
    r"^here[\N{RIGHT SINGLE QUOTATION MARK}']?s?\s+(?:is\s+)?(?:the|your|a)\b.*:\s*$",
    re.IGNORECASE,
)

# The meta lead must be short — a real paragraph is content, however it opens.
_MAX_META_LINES = 2


# Inline-markdown shapes for SCALAR fields. Paired markers only: an unpaired
# ``**`` (e.g. the filing-footnote marker in ``Commerce (**)``) must survive,
# and lone underscores are never touched so snake_case identifiers survive.
_BOLD_STAR_RX = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER_RX = re.compile(r"__([^_]+)__")
# Single-star italic: content may not start/end with whitespace or ``*``, so
# arithmetic like "5 * 3 * 2" is left alone.
_ITALIC_STAR_RX = re.compile(r"\*([^*\s](?:[^*]*[^*\s])?)\*")
_CODE_SPAN_RX = re.compile(r"`([^`]+)`")
_HEADING_MARK_RX = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)


def strip_inline_markdown(text: str) -> str:
    """Reduce a NATURAL-LANGUAGE SCALAR field to plain text: unwrap
    ``**bold**`` / ``__bold__`` / ``*italic*`` / `` `code` `` pairs and drop
    leading heading markers. Idempotent; unpaired markers and intra-word
    underscores are untouched.

    CONTRACT — natural-language scalars ONLY (KPI names, chart priorities,
    memo titles). It is NOT safe for identifier/formula/code-bearing columns:
    paired ``*``/``__`` are treated as emphasis, so ``3*4*5`` -> ``345``,
    ``__init__ handler`` -> ``init handler``, and ``a__b__c`` -> ``abc``. The
    corrupting boundary is pinned by regression tests in
    ``tests/test_llm_postprocess.py`` — point this at a formula- or
    identifier-bearing field only after consciously revisiting those tests."""
    if not text:
        return text
    out = text
    # Fixpoint loop so nested shapes like ``**a *b* c**`` fully unwrap.
    while True:
        step = _HEADING_MARK_RX.sub("", out)
        step = _BOLD_STAR_RX.sub(r"\1", step)
        step = _BOLD_UNDER_RX.sub(r"\1", step)
        step = _ITALIC_STAR_RX.sub(r"\1", step)
        step = _CODE_SPAN_RX.sub(r"\1", step)
        if step == out:
            break
        out = step
    return out.strip()


def _is_meta_lead(line: str) -> bool:
    return bool(_META_LEAD_RX.match(line) or _HERE_IS_RX.match(line))


def strip_llm_preamble(text: str) -> str:
    """Drop a short process-narration lead from LLM output; otherwise return
    ``text`` unchanged. See the module docstring for the (conservative)
    matching rules."""
    if not text:
        return text
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return text
    first = lines[i].strip()
    # Content that opens with a heading or a non-meta sentence is untouched.
    if first.startswith("#") or not _is_meta_lead(first):
        return text
    # The lead block: consecutive non-blank, non-heading lines from the top.
    j = i
    while j < len(lines) and lines[j].strip() and not lines[j].lstrip().startswith("#"):
        j += 1
    if j - i > _MAX_META_LINES:
        return text  # a real paragraph, not a ≤2-line announcement
    # Skip trailing blank lines after the meta lead.
    k = j
    while k < len(lines) and not lines[k].strip():
        k += 1
    if k >= len(lines):
        return text  # never strip a document down to nothing
    return "\n".join(lines[k:])
