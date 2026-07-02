"""Auto-reconciliation — derive verdicts; never queue what software can decide.

The owner's 2026-07-02 correction, verbatim: "Determination for so much of this
can be gleaned from my holdings and other information I share with you. Stop
turning this into a shit program that requires a lot of rote inputs from me."
The first Reconcile queue put ~28 one-tap chores in front of him; 27 were
derivable. This pass encodes the derivations, deterministically:

- a seed **decision note** is HISTORY — the action already happened; it needs
  no verdict. Auto ``done`` (reconciled_by ``auto:history``).
- a seed **musing** is ambient working memory — it ages through synthesis, not
  through review. Auto ``live`` (``auto:ambient``).
- a **theme** is durable by construction (it was distilled, not captured).
  Auto ``live`` (``auto:durable``); contradiction handling belongs to the
  distill-time overlap check, not a review queue.
- an **'(inferred)' falsifier on a name no longer portfolio-held** is moot —
  there is no position for it to guard. Auto ``drop`` (``auto:moot-unheld``).

What survives is the irreducible owner-only residue: inferred falsifiers on
HELD positions (words the coach would quote — only the owner can ratify
those), and held positions with NO falsifier text at all (surfaced by
``reconcile.list_missing_falsifiers`` — a live position with no tripwire must
ask, never silently pass). Everything auto-resolved is stamped with its
derivation so the panel can show one summary line instead of a backlog.
Idempotent; zero LLM.
"""

from __future__ import annotations

from pathlib import Path

from user_state._db import now_iso, open_conn


def auto_reconcile(db_path: Path | str | None = None) -> dict[str, int]:
    """Run every derivation; returns a tally of what was auto-resolved."""
    stamp = now_iso()
    tally = {
        "decisions_done": 0,
        "musings_live": 0,
        "themes_live": 0,
        "already_closed": 0,
        "falsifiers_dropped_unheld": 0,
    }
    conn = open_conn(db_path)
    try:
        # Any seed note that is already resolved/superseded/archived (e.g. an
        # intent closed with provenance) needs no verdict — it HAS one.
        cur = conn.execute(
            """
            UPDATE analyst_notes
            SET context_json = json_set(coalesce(context_json,'{}'),
                                        '$.reconcile', 'done',
                                        '$.reconciled_at', ?,
                                        '$.reconciled_by', 'auto:already-closed')
            WHERE source_ref LIKE 'seed:%' AND status != 'open'
              AND json_extract(coalesce(context_json,'{}'), '$.reconcile') IS NULL
            """,
            (stamp,),
        )
        tally["already_closed"] = cur.rowcount
        # Seed decision notes → done (history; the resolved status keeps them
        # out of the open-notes priors while the record itself is untouched).
        cur = conn.execute(
            """
            UPDATE analyst_notes
            SET context_json = json_set(coalesce(context_json,'{}'),
                                        '$.reconcile', 'done',
                                        '$.reconciled_at', ?,
                                        '$.reconciled_by', 'auto:history'),
                status = 'resolved', updated_at = ?
            WHERE source_ref LIKE 'seed:%' AND kind = 'decision'
              AND json_extract(coalesce(context_json,'{}'), '$.reconcile') IS NULL
            """,
            (stamp, stamp),
        )
        tally["decisions_done"] = cur.rowcount

        # Seed musings → live ambient context (status stays open; synthesis
        # and supersede chains age them out, not a review queue).
        cur = conn.execute(
            """
            UPDATE analyst_notes
            SET context_json = json_set(coalesce(context_json,'{}'),
                                        '$.reconcile', 'live',
                                        '$.reconciled_at', ?,
                                        '$.reconciled_by', 'auto:ambient'),
                updated_at = ?
            WHERE source_ref LIKE 'seed:%' AND kind = 'musing'
              AND json_extract(coalesce(context_json,'{}'), '$.reconcile') IS NULL
            """,
            (stamp, stamp),
        )
        tally["musings_live"] = cur.rowcount

        # Themes → live (durable by construction).
        cur = conn.execute(
            """
            UPDATE insight_notes
            SET meta_json = json_set(coalesce(meta_json,'{}'),
                                     '$.reconcile', 'live',
                                     '$.reconciled_at', ?,
                                     '$.reconciled_by', 'auto:durable'),
                updated_at = ?
            WHERE kind = 'theme' AND status = 'current'
              AND json_extract(coalesce(meta_json,'{}'), '$.reconcile') IS NULL
            """,
            (stamp, stamp),
        )
        tally["themes_live"] = cur.rowcount

        # Inferred falsifiers on unheld names → moot; drop them so they never
        # queue for ratification and can never be quoted or extracted.
        cur = conn.execute(
            """
            UPDATE decisions
            SET falsifier = NULL,
                user_notes = coalesce(user_notes,'') || ' · falsifier auto-dropped '
                             || '(moot: not portfolio-held) ' || ?
            WHERE decided_by = 'owner' AND falsifier LIKE '%(inferred)%'
              AND (ticker IS NULL OR NOT EXISTS (
                    SELECT 1 FROM tracked_companies t
                    WHERE t.ticker = decisions.ticker AND t.list_type = 'portfolio'))
            """,
            (stamp,),
        )
        tally["falsifiers_dropped_unheld"] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return tally


def auto_reconciled_summary(db_path: Path | str | None = None) -> dict[str, int]:
    """Counts for the panel's one-line summary ("N auto-resolved")."""
    out = {"auto_done": 0, "auto_live": 0, "auto_dropped": 0}
    conn = open_conn(db_path)
    try:
        out["auto_done"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM analyst_notes WHERE "
                "json_extract(coalesce(context_json,'{}'), '$.reconciled_by') = 'auto:history'"
            ).fetchone()[0]
        )
        out["auto_live"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM analyst_notes WHERE "
                "json_extract(coalesce(context_json,'{}'), '$.reconciled_by') = 'auto:ambient'"
            ).fetchone()[0]
        ) + int(
            conn.execute(
                "SELECT COUNT(*) FROM insight_notes WHERE "
                "json_extract(coalesce(meta_json,'{}'), '$.reconciled_by') = 'auto:durable'"
            ).fetchone()[0]
        )
        out["auto_dropped"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE "
                "user_notes LIKE '%falsifier auto-dropped (moot%'"
            ).fetchone()[0]
        )
    except Exception:
        pass
    finally:
        conn.close()
    return out
