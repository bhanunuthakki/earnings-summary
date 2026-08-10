"""Domain-owned thesis/KPI writer registered behind :mod:`research.apply`."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

from compute.kpi_definition_units import resolve_definition_unit
from research.apply import MutationApplyResult, register_mutating_applier
from research.proposals import ResearchProposal
from user_state._db import now_iso


def _sync_kpi_registry(
    connection: sqlite3.Connection,
    *,
    content: object,
    holdings_payload: dict[str, object],
) -> int:
    from research.proposal_approval import KpiProposalContentV1

    if not isinstance(content, KpiProposalContentV1):
        return 0
    del holdings_payload  # full post-change uniqueness was validated before replacement
    stamp = now_iso()
    old_by_name = {item.name.casefold(): item for item in content.old_value}
    new_by_name = {item.name.casefold(): item for item in content.new_value}
    added = [item for key, item in new_by_name.items() if key not in old_by_name]
    removed = [item for key, item in old_by_name.items() if key not in new_by_name]
    thesis_breaker = content.target_path == "/tier_1_kpis"
    threshold_tier = "tier_1_break" if thesis_breaker else "tier_2_monitor"
    follow_up_count = 0
    for item in added:
        unit = resolve_definition_unit(connection, content.ticker, item.name)
        existing_definition = connection.execute(
            "SELECT id FROM kpi_definitions WHERE ticker=? AND name=?",
            (content.ticker, item.name),
        ).fetchone()
        if existing_definition is None:
            connection.execute(
                "INSERT INTO kpi_definitions "
                "(ticker,name,unit,primary_source,threshold_tier,notes,definition_origin) "
                "VALUES (?,?,?,?,?,?, 'analyst')",
                (
                    content.ticker,
                    item.name,
                    unit.value,
                    "ir_doc",
                    threshold_tier,
                    str(item.break_condition or item.notes or item.note or "")[:2000] or None,
                ),
            )
        else:
            connection.execute(
                "UPDATE kpi_definitions SET unit=?,threshold_tier=? WHERE id=?",
                (unit.value, threshold_tier, int(existing_definition["id"])),
            )
        registry = connection.execute(
            "SELECT id,scaffold_source FROM user_kpi_registry "
            "WHERE user_id='bhanu' AND ticker=? AND kpi_name=?",
            (content.ticker, item.name),
        ).fetchone()
        if registry is None:
            connection.execute(
                "INSERT INTO user_kpi_registry "
                "(user_id,ticker,kpi_name,threshold_direction,threshold_value,is_thesis_breaker,"
                "scaffold_source,notes,created_at,updated_at) "
                "VALUES ('bhanu',?,?,NULL,NULL,?,?,?,?,?)",
                (
                    content.ticker,
                    item.name,
                    int(thesis_breaker),
                    "copilot_ask_approval",
                    str(item.break_condition or item.notes or item.note or "")[:2000] or None,
                    stamp,
                    stamp,
                ),
            )
        elif str(registry["scaffold_source"] or "") == "copilot_ask_approval":
            connection.execute(
                "UPDATE user_kpi_registry SET is_thesis_breaker=?,updated_at=? WHERE id=?",
                (int(thesis_breaker), stamp, int(registry["id"])),
            )
        else:
            follow_up_count += 1
    for item in removed:
        connection.execute(
            "UPDATE kpi_definitions SET threshold_tier=NULL "
            "WHERE ticker=? AND name=? AND threshold_tier=?",
            (content.ticker, item.name, threshold_tier),
        )
        registry = connection.execute(
            "SELECT id,scaffold_source FROM user_kpi_registry "
            "WHERE user_id='bhanu' AND ticker=? AND kpi_name=?",
            (content.ticker, item.name),
        ).fetchone()
        if registry is not None:
            if str(registry["scaffold_source"] or "") == "copilot_ask_approval":
                connection.execute(
                    "DELETE FROM user_kpi_registry WHERE id=?", (int(registry["id"]),)
                )
            else:
                follow_up_count += 1
    connection.execute(
        "INSERT INTO thesis_ledger_entries "
        "(user_id,ticker,entry_kind,body,source_alert_id,created_at,accepted_at) "
        "VALUES ('bhanu',?,'kpi_update',?,NULL,?,?)",
        (
            content.ticker,
            json.dumps(
                {
                    "target_path": content.target_path,
                    "added": [item.name for item in added],
                    "removed": [item.name for item in removed],
                    "follow_up_count": follow_up_count,
                },
                sort_keys=True,
            ),
            stamp,
            stamp,
        ),
    )
    return follow_up_count


def apply_ask_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None,
    proposal: ResearchProposal,
    repo_root: object,
    connection: object,
    after_replace: object = None,
) -> MutationApplyResult:
    """Apply one already-gated canonical Ask proposal in the caller's transaction."""

    del proposal_id, db_path
    from research.proposal_approval import apply_canonical_ask_change

    if not isinstance(repo_root, Path) or not isinstance(connection, sqlite3.Connection):
        raise TypeError("Ask proposal applier requires its domain transaction context")
    callback = cast("Callable[[], None]", after_replace) if callable(after_replace) else None
    result = apply_canonical_ask_change(
        proposal,
        repo_root=repo_root,
        connection=connection,
        after_replace=callback,
    )
    follow_up_count = _sync_kpi_registry(
        connection,
        content=result.content,
        holdings_payload=result.holdings_payload,
    )
    return MutationApplyResult(
        message=(
            "Approved and recovered the already-applied canonical change"
            if result.recovered
            else "Approved and applied the canonical change"
        )
        + (
            f"; follow-up required for {follow_up_count} externally owned KPI registry row(s)"
            if follow_up_count
            else ""
        ),
        applied=True,
        target_postcondition_sha256=result.target_postcondition_sha256,
    )


register_mutating_applier("ask_thesis_edit", apply_ask_proposal)
register_mutating_applier("ask_kpi_edit", apply_ask_proposal)
