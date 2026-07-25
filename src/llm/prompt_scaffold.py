"""Derive a purpose's instruction scaffold from its CAPTURED renders
(meta_eval_governance.md §4.1, extended).

§4.1 defines a prompt variant as an edit-splice on "the instruction scaffold —
the parts identical across all captured renders of that purpose". The original
build took that scaffold from a hand-supplied ``--template-file`` pointing at a
checked-in constant. That does not generalise: most purposes here build their
prompt inline (``src/llm_client.py`` and friends), so there is no single
constant to point at, and any registry mapping purpose → constant drifts the
moment a prompt is edited.

This module derives the scaffold from the capture corpus instead. The captured
renders ARE the ground truth of what production sent, so a scaffold derived from
them cannot drift from the deployed prompt.

The derivation doubles as the ANCHOR SPACE. ``apply_edits`` requires every
``find`` to occur exactly once in every render; a block is admitted here only
when it satisfies exactly that. So a proposer restricted to quoting these blocks
proposes inside the legal space by construction, which turns the old
"propose → validate → reject after the fact" loop into "propose within bounds".
Anchor rejection stops being the common case.

Algorithm (line-structured, deterministic, no LLM):

1. Split every render into lines. A candidate line appears EXACTLY ONCE in
   EVERY render — that is the anchor condition lifted to the line level, and it
   drops the per-ticker data region automatically (data lines differ across
   tickers; boilerplate data lines that happen to repeat fail the once-only
   test).
2. Glue candidate lines that are consecutive in the reference render into
   blocks — instruction paragraphs survive whole rather than as loose lines.
3. Re-verify each glued block as a substring: exactly once in every render.
   Gluing can only ever narrow the match set, but a block that survives step 2
   and fails here would be an anchor bomb, so it is checked, not assumed.
4. Keep blocks of at least ``min_block_chars`` — a 12-character block is a
   useless anchor and a needless collision risk.

A purpose whose renders share no scaffold (single captured render, or fully
templated-away instructions) yields an EMPTY scaffold. That is a real,
reportable state — ``Scaffold.eligible`` is False and the A/B cycle skips the
purpose LOUDLY rather than falling back to some weaker anchor rule that would
look like it worked.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

# A block shorter than this is a poor anchor: high collision risk, and too small
# for a proposer to attach a meaningful instruction edit to.
MIN_BLOCK_CHARS = 48

# Derivation needs at least two renders — "identical across renders" is
# meaningless with one, and a one-render scaffold would happily admit the
# per-ticker data region as "scaffold".
MIN_RENDERS = 2

# Guardrail on proposer context: the biggest blocks carry the instructions.
MAX_BLOCKS = 40


@dataclass(frozen=True, slots=True)
class ScaffoldBlock:
    """One contiguous run of scaffold text, verified to occur exactly once in
    every render it was derived from — i.e. a legal ``PromptEdit.find`` region."""

    text: str
    ordinal: int  # position in the reference render, 0-based

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class Scaffold:
    """The derived scaffold plus the provenance needed to judge whether to
    trust it. ``eligible`` is the single gate callers check."""

    blocks: tuple[ScaffoldBlock, ...]
    n_renders: int
    coverage: float  # scaffold chars / reference render chars
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return bool(self.blocks)

    @property
    def scaffold_chars(self) -> int:
        return sum(b.chars for b in self.blocks)

    def render_block_menu(self, *, max_chars: int = 9000) -> str:
        """The numbered block list handed to the proposer as its legal anchor
        space. Truncated by TOTAL budget, biggest-block-first, then restored to
        document order so the proposer reads the prompt in its real sequence."""
        chosen: list[ScaffoldBlock] = []
        spent = 0
        for block in sorted(self.blocks, key=lambda b: -b.chars):
            cost = block.chars + 24  # + the "[block N]" framing
            if spent + cost > max_chars:
                continue
            chosen.append(block)
            spent += cost
        chosen.sort(key=lambda b: b.ordinal)
        return "\n\n".join(f"[block {b.ordinal}]\n{b.text}" for b in chosen)


def _candidate_lines(renders: list[str]) -> set[str]:
    """Lines occurring exactly once in every render (step 1)."""
    per_render = [Counter(r.splitlines()) for r in renders]
    first = per_render[0]
    return {
        line
        for line, count in first.items()
        if count == 1 and all(other.get(line, 0) == 1 for other in per_render[1:])
    }


def derive_scaffold(
    renders: list[str],
    *,
    min_block_chars: int = MIN_BLOCK_CHARS,
    max_blocks: int = MAX_BLOCKS,
) -> Scaffold:
    """Derive the instruction scaffold shared by ``renders``.

    Returns an ineligible (empty-block) Scaffold with a populated ``reason``
    whenever derivation cannot honestly run — never a degraded guess.
    """
    usable = [r for r in renders if r and r.strip()]
    if len(usable) < MIN_RENDERS:
        return Scaffold(
            blocks=(),
            n_renders=len(usable),
            coverage=0.0,
            reason=(
                f"need >={MIN_RENDERS} captured renders to derive a scaffold, "
                f"got {len(usable)} — harvest more before A/B-ing this purpose"
            ),
        )

    shared = _candidate_lines(usable)
    if not shared:
        return Scaffold(
            blocks=(),
            n_renders=len(usable),
            coverage=0.0,
            reason=(
                "no line occurs exactly once in every render — this purpose has "
                "no stable instruction scaffold (fully templated or single-shape)"
            ),
        )

    reference = usable[0]
    # Step 2: glue runs of consecutive shared lines from the reference render.
    runs: list[list[str]] = []
    current: list[str] = []
    for line in reference.splitlines():
        if line in shared:
            current.append(line)
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    # Step 3+4: verify each glued run as a whole-substring anchor, keep the
    # substantial ones.
    blocks: list[ScaffoldBlock] = []
    for ordinal, run in enumerate(runs):
        text = "\n".join(run)
        if len(text) < min_block_chars:
            continue
        if any(r.count(text) != 1 for r in usable):
            continue  # gluing produced a non-unique span — not a legal anchor
        blocks.append(ScaffoldBlock(text=text, ordinal=ordinal))

    if not blocks:
        return Scaffold(
            blocks=(),
            n_renders=len(usable),
            coverage=0.0,
            reason=(
                f"shared lines exist but no contiguous block reaches "
                f"{min_block_chars} chars — scaffold too fragmented to edit safely"
            ),
        )

    # Cap by size, keeping the biggest (the instruction bodies), then restore
    # document order so ordinals still read left-to-right.
    if len(blocks) > max_blocks:
        blocks = sorted(
            sorted(blocks, key=lambda b: -b.chars)[:max_blocks], key=lambda b: b.ordinal
        )

    return Scaffold(
        blocks=tuple(blocks),
        n_renders=len(usable),
        coverage=round(sum(b.chars for b in blocks) / len(reference), 4) if reference else 0.0,
    )


def block_containing(scaffold: Scaffold, find: str) -> ScaffoldBlock | None:
    """The scaffold block a proposed ``find`` was quoted from, if any.

    The cycle uses this to reject edits that anchor OUTSIDE the derived
    scaffold: such an edit may still splice cleanly on today's sample yet is not
    provably scaffold, so it risks mutating the per-ticker data region on a
    future render.
    """
    for block in scaffold.blocks:
        if find in block.text:
            return block
    return None
