"""Load the canonical public-evidence method and check narrative input/output bounds."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ResearchMode = Literal["company", "earnings", "thesis", "language"]
_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src/advisor/skills/earnings-summary-investing/references/research-method.md"
)
_SECTIONS = (
    "Evidence to judgment",
    "Moat: test the economic mechanism",
    "Earnings: preserve the bar before judging the result",
    "Q&A: assess the complete exchange",
    "Language and management credibility",
    "Placement within existing artifacts",
    "Delivery and runtime limits",
)


class ResearchOutputContractError(ValueError):
    """The method source or generated section inventory is invalid."""


class ResearchInputLimitError(ValueError):
    """Complete research inputs exceed the declared synthesis bound."""


@dataclass(frozen=True, slots=True)
class ResearchMethod:
    version: str
    source_sha256: str
    mode: ResearchMode
    instructions: str

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "source_sha256": self.source_sha256,
            "mode": self.mode,
            "instructions_sha256": hashlib.sha256(self.instructions.encode()).hexdigest(),
        }


def load_research_method(mode: ResearchMode, *, source_path: Path | None = None) -> ResearchMethod:
    """Read each time so source edits invalidate cache identities without restart."""
    raw = (source_path or _SOURCE).read_bytes()
    text = raw.decode("utf-8")
    matches = list(re.finditer(r"^## (.+)$", text, re.MULTILINE))
    sections = {
        match.group(1): text[
            match.start() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
        ].strip()
        for i, match in enumerate(matches)
    }
    missing = [title for title in _SECTIONS if title not in sections]
    if missing or len(sections) != len(matches):
        raise ResearchOutputContractError(
            f"Research method missing or duplicate sections: {missing}"
        )
    for title in _SECTIONS:
        if not sections[title].partition("\n")[2].strip():
            raise ResearchOutputContractError(f"Research method empty section: {title}")
    selected = _SECTIONS
    if mode == "language":
        selected = (_SECTIONS[0], _SECTIONS[3], _SECTIONS[4], _SECTIONS[6])
    elif mode not in ("company", "earnings", "thesis"):
        raise ResearchOutputContractError(f"Unknown research mode: {mode}")
    return ResearchMethod(
        version="public-evidence@1",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        mode=mode,
        instructions="\n\n".join(sections[title] for title in selected),
    )


def validate_research_input(prompt: str, *, max_chars: int = 320_000) -> None:
    if len(prompt) > max_chars:
        raise ResearchInputLimitError(
            f"Complete research prompt exceeds input bound: {len(prompt)} > {max_chars} characters"
        )


def validate_research_markdown(text: str, expected_titles: Sequence[str]) -> None:
    """Check structure only. Semantic judgments still require research evaluation."""
    matches = list(re.finditer(r"^## (.+?)\s*$", text, re.MULTILINE))
    titles = [match.group(1) for match in matches]
    if titles != list(expected_titles):
        raise ResearchOutputContractError(f"Research section inventory mismatch: {titles}")
    if text[: matches[0].start()].strip():
        raise ResearchOutputContractError("Research output has content before required sections")
    for i, match in enumerate(matches):
        body = text[match.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        if not body.strip():
            raise ResearchOutputContractError(f"Research output empty section: {titles[i]}")
