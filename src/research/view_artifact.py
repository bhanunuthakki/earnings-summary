"""Phase-1 Wave-2 saved-view artifact.

Reuses the EXISTING governed NL->ViewSpec compiler (purpose ``viewspec_compile``,
gemini-flash, golden-backed, ``ViewSpec.from_dict``-validated) rather than minting
a redundant ``artifact_draft_view`` purpose (house rule: reuse seams over new
frameworks). The view artifact is NON-mutating in the higher-bar sense -
``from_dict`` validation *is* the oracle - so approve writes the real saved view.

Flow (Phase-1 plan §3 Wave 2):

  draft   -> compile the wondering into a ViewSpec; persist an INERT ``kind='view'``
             ``research_proposal`` carrying the spec in ``artifact_json``.
  preview -> LLM-free via ``viewspec.engine.execute_view`` (render-time; W2-2b).
  apply   -> the SEPARATE explicit write step (the verb action core stays
             write-free by design - a one-tap approve never writes): re-validate
             the spec (oracle) and call the real
             ``user_state.saved_views.save_view``.

Dependency-injected (``compile_fn`` / ``create_fn`` / ``save_fn`` / ``get_fn``) in
the ``run.py`` idiom so the primitives are unit-testable without an LLM or a DB.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from research.proposals import create_proposal, get_proposal
from viewspec.spec import ViewSpec


@dataclass(frozen=True)
class ViewDraftResult:
    proposal_id: int | None
    status: str  # "drafted" | "degraded"
    message: str = ""


def _preview_md(spec_dict: dict[str, object]) -> str:
    tickers_raw = spec_dict.get("tickers")
    tickers = (
        ", ".join(str(t) for t in cast("list[object]", tickers_raw))
        if isinstance(tickers_raw, list)
        else ""
    )
    metrics_raw = spec_dict.get("metrics")
    labels: list[str] = []
    if isinstance(metrics_raw, list):
        for m in cast("list[object]", metrics_raw):
            if isinstance(m, dict):
                md = cast("dict[str, object]", m)
                labels.append(f"{md.get('domain')}:{md.get('key')}")
            else:
                labels.append(str(m))
    metrics = ", ".join(labels)
    periods = spec_dict.get("periods", "")
    tail = f", {periods} periods" if periods else ""
    return (
        f"**Proposed saved view** - {tickers or '-'} x [{metrics or '-'}]{tail}. "
        "Preview runs LLM-free (execute_view); approve saves it."
    )


def _view_name(claim: str, ticker: str | None) -> str:
    first = (claim or "").strip().splitlines()[0].strip()[:60] if claim and claim.strip() else ""
    return first or (ticker.upper() if ticker else "Saved view")


def draft_view_proposal(
    *,
    claim: str,
    ticker: str | None = None,
    note_id: int | None = None,
    task_id: int | None = None,
    db_path: Path | None = None,
    compile_fn: Callable[..., Any] | None = None,
    create_fn: Callable[..., int] = create_proposal,
) -> ViewDraftResult:
    """Compile the wondering into a ViewSpec and persist an inert ``kind='view'`` proposal.

    Never raises on a bad compile - a degraded compile returns a ``degraded``
    result and persists nothing (words already landed as the musing upstream).
    """
    if compile_fn is None:
        from viewspec.nl_compile import compile_nl_to_viewspec  # lazy: pulls llm.cli

        compile_fn = compile_nl_to_viewspec
    result = compile_fn(
        claim,
        db_path=db_path,
        context_tickers=[ticker.upper()] if ticker else None,
    )
    spec = getattr(result, "spec", None)
    status = getattr(result, "status", "error")
    if status != "ok" or spec is None:
        return ViewDraftResult(None, "degraded", getattr(result, "message", None) or str(status))

    spec_dict = spec.to_dict()
    proposal_id = create_fn(
        task_id=task_id,
        kind="view",
        ticker=ticker,
        title=_view_name(claim, ticker),
        body_md=_preview_md(spec_dict),
        evidence_json=json.dumps([{"note_id": note_id}] if note_id is not None else []),
        source_note_ids=json.dumps([note_id] if note_id is not None else []),
        artifact_json=json.dumps(spec_dict),
        provenance="derived",
        db_path=db_path,
    )
    return ViewDraftResult(int(proposal_id), "drafted")


def apply_view_proposal(
    proposal_id: int,
    *,
    name: str | None = None,
    db_path: Path | None = None,
    get_fn: Callable[..., Any] = get_proposal,
    save_fn: Callable[..., Any] | None = None,
) -> Any:
    """The SEPARATE explicit write step: re-validate the drafted spec then save it.

    Kept out of the write-free verb action core by design - a one-tap approve
    only flips status; this deliberate later step performs the live write. Raises
    ``ViewSpecError`` if the stored spec no longer validates (the oracle), or
    ``ValueError`` for a non-view / rejected / spec-less proposal.
    """
    prop = get_fn(proposal_id, db_path=db_path)
    if prop is None or getattr(prop, "kind", None) != "view":
        raise ValueError(f"proposal {proposal_id} is not a view proposal")
    if getattr(prop, "status", None) in ("rejected", "superseded"):
        raise ValueError(f"cannot apply a {getattr(prop, 'status', '?')} proposal")
    artifact = getattr(prop, "artifact_json", None)
    if not artifact:
        raise ValueError(f"view proposal {proposal_id} carries no ViewSpec (artifact_json empty)")

    spec = ViewSpec.from_dict(json.loads(artifact))  # ORACLE - re-validate before the live write
    if save_fn is None:
        from user_state.saved_views import save_view  # lazy

        save_fn = save_view
    return save_fn(
        name=name or getattr(prop, "title", None) or "Saved view",
        spec=spec.to_dict(),
        db_path=db_path,
    )
