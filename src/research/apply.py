"""Phase-1 Wave-2 apply dispatch: the SEPARATE explicit write step per artifact kind.

``proposals.act_on_proposal`` is write-free by construction -- a one-tap approve
only flips status. This module is the deliberate later write both surfaces (the
web ``/api/research/proposal/<id>/act`` route and the Telegram callback) invoke
AFTER an approve, dispatching by proposal kind:

  - view  -> the real saved view (``view_artifact.apply_view_proposal`` ->
             ``user_state.saved_views.save_view``)
  - memo  -> nothing (a memo IS the artifact; approve just files it)
  - dcf / thesis / code -> Wave 3/4 (not wired yet; a benign note)

It performs NO web fetch, so approve-then-apply never forms a lethal trifecta.
"""

from __future__ import annotations

from pathlib import Path

from research.proposals import get_proposal


def apply_approved_proposal(proposal_id: int, *, db_path: Path | str | None = None) -> str:
    """Perform the kind-specific live write for a just-approved proposal.

    Returns a short human note for the surface to echo (empty when there is
    nothing to write). Propagates only what the kind-specific applier raises
    (e.g. the view oracle rejecting a now-invalid spec) so the surface can show
    it rather than silently swallow a failed write.
    """
    prop = get_proposal(proposal_id, db_path=db_path)
    if prop is None:
        return ""
    if prop.kind == "view":
        from research.view_artifact import apply_view_proposal

        row = apply_view_proposal(proposal_id, db_path=db_path)
        return f"saved view: {getattr(row, 'name', None) or prop.title}"
    if prop.kind == "memo":
        return ""
    return f"{prop.kind}: apply not yet wired"
