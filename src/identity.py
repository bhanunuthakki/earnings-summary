"""Single source of truth for the operating user's identity.

This is a single-user tool; every user-scoped table (alerts, thesis ledger, KPI
registry, sizing intent, …) is keyed by ``user_id``. The default resolves from
the ``CIO_USER_ID`` environment variable, falling back to ``"bhanu"`` so rows
already written under that id keep resolving. Set ``CIO_USER_ID`` to run the
same code under a different identity without touching the schema.

Resolved once at import; export the env var before launching the process.
"""

from __future__ import annotations

import os

DEFAULT_USER_ID: str = os.environ.get("CIO_USER_ID", "bhanu")
