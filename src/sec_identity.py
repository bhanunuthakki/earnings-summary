"""The one place this project decides who it says it is to the SEC.

SEC fair access asks every automated requester to declare a real contact, and
enforces it: an undeclared or bogus User-Agent earns a 403, which
``filings.edgar_fetch`` classifies as a ``HardStopError`` precisely because
retrying without fixing the header cannot succeed.

This module exists because the project had drifted to NINE different User-Agent
strings across ten modules, five of them declaring an address that does not
exist — three ``@example.com`` placeholders and one typo of the owner's own
address. Every one of those was live code hitting sec.gov. A shared helper is
the only shape that keeps the declaration honest: a constant copied per module
is a constant that goes stale per module.

``EDGAR_USER_AGENT`` overrides the default, and is read per call rather than
captured at import so a long-running process picks up a change.
"""

from __future__ import annotations

import os

#: Public fallback identity. Operators should set ``EDGAR_USER_AGENT`` to a
#: monitored contact address in their private runtime configuration.
DEFAULT_USER_AGENT = "earnings-summary/1.0 (+https://github.com/bhanunuthakki/earnings-summary)"

#: Environment override, already honoured by several execution/ CLIs.
USER_AGENT_ENV = "EDGAR_USER_AGENT"


def sec_user_agent() -> str:
    """The User-Agent to declare on any request to sec.gov or data.sec.gov."""
    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT
