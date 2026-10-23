"""Publish the closed 18-member MELI H1 or seven-flow FY statement source population.

Dry-run is default. This does not bind canonical metrics, promote valuation
inputs, or declare complete filing extraction. Missing members remain rejected.
"""

from __future__ import annotations

if not __package__:  # direct script invocation
    from publish_meli_reported_tables import main as publish_source_cli
else:  # package import must reuse the same clock and DB boundary
    from execution.publish_meli_reported_tables import main as publish_source_cli
from provenance.meli_reported_statements import publish_meli_reported_statements


def main(argv: list[str] | None = None) -> int:
    return publish_source_cli(argv, publisher=publish_meli_reported_statements, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
