"""discovery_sources — backfill the verified 13F manager CIKs.

The 0098 seed shipped only Appaloosa's CIK (the one verified at author time);
the rest were configured-yet-dormant. This backfills the managers whose
current 13F-HR filer CIK was verified live 2026-06-13 against
``data.sec.gov/submissions`` (a recent 13F-HR + a clean conformed-name match) —
the safe subset that ``execution/resolve_manager_ciks.py`` would itself
auto-apply (exactly one recent, strong-name candidate). The trap cases stay
NULL deliberately: a manager's old entity that stopped filing (Viking Global
Equities LP — last 13F 2002) or a near-duplicate sleeve (Sands Capital
*Alternatives* / Polen *Credit* vs the growth books) are left for the resolver
+ owner to disposition, not guessed here.

Idempotent: each UPDATE is gated on ``cik IS NULL`` so it never clobbers a CIK
the owner has since set via the Sources panel. Downgrade is a deliberate no-op
(this is a data backfill; the column + table belong to 0096/0098).

Revision ID: 0099_discovery_sources_ciks
Revises: 0098_discovery_sources
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0099_discovery_sources_ciks"
down_revision: str | Sequence[str] | None = "0098_discovery_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (source_key, cik) — verified live 2026-06-13: each filed a 13F-HR in 2026-05
# under a conformed name that uniquely matches the roster entry.
_VERIFIED_CIKS: tuple[tuple[str, str], ...] = (
    ("altimeter", "0001541617"),  # Altimeter Capital Management, LP
    ("coatue", "0001135730"),  # COATUE MANAGEMENT LLC
    ("lone_pine", "0001061165"),  # LONE PINE CAPITAL LLC
    ("tiger_global", "0001167483"),  # TIGER GLOBAL MANAGEMENT LLC
    ("whale_rock", "0001387322"),  # Whale Rock Capital Management LLC
    ("akre", "0001112520"),  # AKRE CAPITAL MANAGEMENT LLC
    ("edgewood", "0000860561"),  # EDGEWOOD MANAGEMENT LLC
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_sources" not in insp.get_table_names():
        return
    for source_key, cik in _VERIFIED_CIKS:
        bind.execute(
            sa.text(
                "UPDATE discovery_sources SET cik = :cik, updated_at = :ts "
                "WHERE source_key = :key AND cik IS NULL"
            ),
            {"cik": cik, "ts": "2026-06-13T00:00:00+00:00", "key": source_key},
        )


def downgrade() -> None:
    # Data backfill — nothing structural to undo.
    pass
