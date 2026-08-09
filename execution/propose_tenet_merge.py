"""One-off: propose a merge of two duplicate current Tenets (B5).

Built for the prod 20/31 duplicate this PR closes — tenets 20
(``tenet:retirement-account-hold-discipline``) and 31
(``tenet:tax-account-holding-discipline``) are the SAME underlying belief
under two different scope_key slugs (the semantic-tension gap
``src/synthesis/semantic_tension.py`` now closes going forward for NEW
landings). Generic for any future pair the owner spots or the accountability
ledger flags: given a KEEP id and a MERGE id (both current Tenets), stage a
``proposed`` tenet whose body/scope_key are the KEEPER's, ``tension_with`` =
the merge id, and whose ``source_note_ids`` are the union of both rows'
citations. The owner then approves it from the Worldview panel
(``synthesis.tenets.approve_tenet``), which supersedes the keeper cleanly via
the normal proposed -> current promotion path.

Deliberately does NOT auto-supersede — an owner-tapped approval is required
(2026-07-19 program review: merges are owner-tapped, never automatic). This
script only stages the candidate; zero LLM cost, pure deterministic
bookkeeping.

Usage:
    python execution/propose_tenet_merge.py --keep 20 --merge 31          # dry run
    python execution/propose_tenet_merge.py --keep 20 --merge 31 --apply  # stages it
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_paths import configured_db_path  # noqa: E402
from db_paths import resolve_db_path as canonical_db_path  # noqa: E402
from synthesis.insights import InsightRow, get_insight  # noqa: E402
from synthesis.tenets import TENET_KIND, record_tenet  # noqa: E402


class TenetMergeError(ValueError):
    """A --keep/--merge pair that fails validation: missing id, wrong kind,
    not status='current', or the same id passed twice."""


def _resolve_db_path(repo_root: Path, override: Path | None) -> Path | None:
    candidate = (
        canonical_db_path(override) if override is not None else configured_db_path(repo_root)
    )
    return candidate if candidate is not None and candidate.exists() else None


def _validated_tenet(tenet_id: int, *, label: str, db_path: Path | str | None) -> InsightRow:
    """Fetch and validate one side of the merge pair. Raises TenetMergeError
    naming the exact problem — missing / wrong kind / not current — rather
    than letting a bad id silently no-op."""
    row = get_insight(tenet_id, db_path=db_path)
    if row is None:
        raise TenetMergeError(f"--{label} {tenet_id}: no such insight")
    if row.kind != TENET_KIND:
        raise TenetMergeError(f"--{label} {tenet_id}: kind={row.kind!r}, expected {TENET_KIND!r}")
    if row.status != "current":
        raise TenetMergeError(f"--{label} {tenet_id}: status={row.status!r}, expected 'current'")
    return row


def stage_merge_proposal(
    keep_id: int, merge_id: int, *, db_path: Path | str | None = None
) -> InsightRow:
    """Validate both ids and stage the merge candidate. Raises
    TenetMergeError on any validation failure; never mutates on failure —
    the DB is untouched unless both ids resolve to two distinct, current
    Tenets."""
    if keep_id == merge_id:
        raise TenetMergeError("--keep and --merge must be different ids")
    keep = _validated_tenet(keep_id, label="keep", db_path=db_path)
    merge = _validated_tenet(merge_id, label="merge", db_path=db_path)
    union_note_ids = sorted(set(keep.source_note_ids) | set(merge.source_note_ids))
    return record_tenet(
        body_md=keep.body_md,
        scope_key=keep.scope_key,
        source_note_ids=union_note_ids,
        status="proposed",
        provenance="owner_merge_candidate",
        tension_with=merge.id,
        db_path=db_path,
    )


def _describe(keep: InsightRow, merge: InsightRow) -> str:
    union_note_ids = sorted(set(keep.source_note_ids) | set(merge.source_note_ids))
    return (
        f"KEEP  T{keep.id} ({keep.scope_key}): {keep.body_md[:120]}\n"
        f"MERGE T{merge.id} ({merge.scope_key}): {merge.body_md[:120]}\n"
        f"-> would stage a PROPOSED tenet on scope_key={keep.scope_key!r}, "
        f"body=keeper's body, tension_with=T{merge.id}, "
        f"source_note_ids={union_note_ids}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--keep", type=int, required=True, help="Insight id of the tenet to keep (the survivor)."
    )
    parser.add_argument(
        "--merge",
        type=int,
        required=True,
        help="Insight id of the duplicate tenet being merged away.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db. Defaults to the worktree root.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the DB path (else uses data/portfolio.db under --repo-root).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Stage the proposal (default: dry-run — print what would happen and exit).",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.repo_root, args.db_path)

    try:
        if args.keep == args.merge:
            raise TenetMergeError("--keep and --merge must be different ids")
        keep = _validated_tenet(args.keep, label="keep", db_path=db_path)
        merge = _validated_tenet(args.merge, label="merge", db_path=db_path)
    except TenetMergeError as exc:
        print(f"propose_tenet_merge: error: {exc}", file=sys.stderr)
        return 1

    if not args.apply:
        print(_describe(keep, merge))
        print("(dry run — pass --apply to stage the proposal)")
        return 0

    proposal = stage_merge_proposal(keep.id, merge.id, db_path=db_path)
    print(
        f"Staged proposed tenet id={proposal.id} on scope_key={proposal.scope_key!r} "
        f"(tension_with=T{merge.id}). Approve it from the Worldview panel to supersede T{keep.id}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
