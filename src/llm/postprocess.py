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
"""

from __future__ import annotations

import re

__all__ = ["strip_llm_preamble"]

# First-person process narration openers ("Having exhausted…", "I now have…",
# "I've gathered…", "Writing from…"). Matched against the lead line only.
_META_LEAD_RX = re.compile(
    r"""^(?:
        having\s |
        i\s+now\s |
        i[’']ve\s |
        i[’']ll\s+now\s |
        i\s+will\s+now\s |
        i\s+have\s+(?:now\s+)?(?:enough|gathered|completed|finished|exhausted|reviewed)\b |
        i\s+am\s+now\s |
        i[’']m\s+now\s |
        now\s+(?:that\s+)?i\s |
        writing\s+from\s |
        based\s+on\s+my\s+(?:search|research|review)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# "Here is the …:" / "Here's the …:" — meta only when the line ENDS with the
# colon (announcing the artifact), so a sentence merely containing the phrase
# survives.
_HERE_IS_RX = re.compile(r"^here[’']?s?\s+(?:is\s+)?(?:the|your|a)\b.*:\s*$", re.IGNORECASE)

# The meta lead must be short — a real paragraph is content, however it opens.
_MAX_META_LINES = 2


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
