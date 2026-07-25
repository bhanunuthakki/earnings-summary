"""Prompts as first-class, versioned templates with separated variables
(LLM Quality Program P0 — directives/llm_quality_program_2026_07.md).

The step-back finding this implements: prompts on this platform were inline
f-strings, so the improvement loop had to reverse-engineer the instruction
scaffold from captured renders — capping coverage at whatever the harvest
happened to contain (9 of 59 costed purposes on 2026-07-25) and making every
proposed edit anchor-fragile. A registered template dissolves all of that:
the template IS the scaffold, the variables ARE the data region, and both are
knowable at the call site with zero archaeology.

Design contract:

* ``PromptTemplate`` — an id, a ``{slot}``-style body, and the EXACT set of
  slot names. Registration validates that the declared variables and the
  body's real slots match exactly (a drifted declaration is a lie that would
  poison every downstream consumer).
* ``version`` auto-derives as ``sha256(body)[:12]``: any edit is a new
  version with zero bump discipline required. ``llm.prompt_versions`` stays
  the HUMAN A/B dimension; this is the mechanical identity. Both get logged.
* ``render()`` returns a ``RenderedPrompt`` — a ``str`` SUBCLASS carrying
  (template_id, template_version, vars_sha256). It flows through the entire
  existing transport untouched (sha256/len/subprocess stdin all see a plain
  string) while the ledger writer lifts the metadata off it via
  ``template_meta``. This is deliberate: threading three new parameters
  through every call_llm/record path would churn ~a dozen signatures for the
  same effect.
* Rendering is STRICT both ways: a missing variable and an unexpected
  variable both raise — a silently-empty slot is exactly the class of
  plausible-success defect this platform's review history keeps finding.

Registration is module-level and greppable (the ``llm_calls.md`` "prompts are
greppable constants" rule, upgraded to structured). Migrated call sites build
their prompt via ``TEMPLATE.render(...)``; unmigrated ones keep passing raw
strings and log NULL template fields — visibly unmigrated, never faked.
"""

from __future__ import annotations

import hashlib
import json
import string
from dataclasses import dataclass, field

# template_id -> PromptTemplate. Module-level so `grep template_id` finds every
# registered prompt; populated at import of the defining module via register().
REGISTRY: dict[str, PromptTemplate] = {}


class RenderedPrompt(str):
    """A rendered prompt that KNOWS which template produced it.

    ``str`` subclass: every existing consumer (sha256, len, subprocess stdin,
    capture, fallback) treats it as a plain string; the ledger lifts the
    template identity off it with ``template_meta`` at record time."""

    template_id: str
    template_version: str
    vars_sha256: str

    def __new__(
        cls, text: str, *, template_id: str, template_version: str, vars_sha256: str
    ) -> RenderedPrompt:
        obj = super().__new__(cls, text)
        obj.template_id = template_id
        obj.template_version = template_version
        obj.vars_sha256 = vars_sha256
        return obj


def _body_slots(body: str) -> set[str]:
    """The real ``{slot}`` names in a template body (``{{``/``}}`` escapes and
    positional ``{}`` excluded — positional slots are rejected at registration)."""
    slots: set[str] = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(body):
        if field_name:  # None = no slot; "" = positional (rejected by caller)
            slots.add(field_name)
    return slots


@dataclass(frozen=True)
class PromptTemplate:
    """One versioned prompt template. Immutable; edits create a new version
    identity automatically (``version`` hashes the body)."""

    template_id: str
    body: str
    variables: tuple[str, ...]
    # Optional human note: what this prompt is for / who consumes the output.
    description: str = ""
    version: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version", hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:12]
        )
        real = _body_slots(self.body)
        declared = set(self.variables)
        if any(
            field_name == ""
            for _l, field_name, _s, _c in string.Formatter().parse(self.body)
            if field_name is not None
        ):
            raise ValueError(f"{self.template_id}: positional {{}} slots are not allowed")
        if real != declared:
            missing = declared - real
            undeclared = real - declared
            raise ValueError(
                f"{self.template_id}: declared variables and body slots differ "
                f"(declared-but-absent: {sorted(missing)}; present-but-undeclared: "
                f"{sorted(undeclared)}) — a drifted declaration lies to every consumer"
            )

    def render(self, **variables: str) -> RenderedPrompt:
        """Strict render: the provided variable set must EXACTLY equal the
        declared set. Missing → the slot would silently vanish; extra → the
        caller thinks something is in the prompt that isn't. Both raise."""
        provided = set(variables)
        declared = set(self.variables)
        if provided != declared:
            raise ValueError(
                f"{self.template_id}: render variables != declared "
                f"(missing: {sorted(declared - provided)}; unexpected: "
                f"{sorted(provided - declared)})"
            )
        text = self.body.format(**variables)
        vars_sha = hashlib.sha256(
            json.dumps(variables, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return RenderedPrompt(
            text,
            template_id=self.template_id,
            template_version=self.version,
            vars_sha256=vars_sha,
        )


def register(template: PromptTemplate) -> PromptTemplate:
    """Add to the registry. A duplicate id with a DIFFERENT body is a hard
    error (two truths); re-registering the identical template is a no-op so
    module reloads (tests) stay safe."""
    existing = REGISTRY.get(template.template_id)
    if existing is not None:
        if existing.version == template.version:
            return existing
        raise ValueError(
            f"template id {template.template_id!r} already registered with a "
            f"different body (existing {existing.version}, new {template.version})"
        )
    REGISTRY[template.template_id] = template
    return template


def template_meta(prompt: object) -> tuple[str | None, str | None, str | None]:
    """(template_id, template_version, vars_sha256) if ``prompt`` is a
    RenderedPrompt, else (None, None, None) — the ledger's lift point.
    Plain-string prompts are simply unmigrated call sites; NULLs in the ledger
    are the honest representation, never an error."""
    if isinstance(prompt, RenderedPrompt):
        return prompt.template_id, prompt.template_version, prompt.vars_sha256
    return None, None, None
