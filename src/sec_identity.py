"""The one place this project decides who it says it is to the SEC.

SEC fair access asks every automated requester to declare a real contact.
``filings.edgar_fetch`` classifies an HTTP 401/403 as a ``HardStopError``:
the same request must not be retried unchanged, although the status alone
does not establish that the declared contact caused it.

This module exists because the project had drifted to NINE different User-Agent
strings across ten modules, five of them declaring an address that does not
exist — three ``@example.com`` placeholders and one typo of the owner's own
address. Every one of those was live code hitting sec.gov. A shared helper is
the only shape that keeps the declaration honest: a constant copied per module
is a constant that goes stale per module.

A nonempty process ``EDGAR_USER_AGENT`` takes precedence over that single key
in the configured project env file, read afresh per call without importing
other keys. Only an absent or blank contact uses the public default.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from runtime.secrets import project_env_file

#: Public fallback identity. Operators should set ``EDGAR_USER_AGENT`` to a
#: monitored contact address in their private runtime configuration.
DEFAULT_USER_AGENT = "earnings-summary/1.0 (+https://github.com/bhanunuthakki/earnings-summary)"

#: Environment override, already honoured by several execution/ CLIs.
USER_AGENT_ENV = "EDGAR_USER_AGENT"


class SecContactConfigurationError(ValueError):
    """The selected SEC contact cannot be represented as an HTTP header."""

    def __init__(self) -> None:
        super().__init__("SEC contact configuration is invalid")


def _valid_contact(value: str) -> str:
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or ord(char) > 255 for char in value):
        raise SecContactConfigurationError
    return value.strip()


def sec_user_agent() -> str:
    """The User-Agent to declare on any request to sec.gov or data.sec.gov."""
    override = _valid_contact(os.environ.get(USER_AGENT_ENV, ""))
    if override:
        return override
    configured = dotenv_values(
        project_env_file(Path(__file__).resolve().parents[1]), interpolate=False
    ).get(USER_AGENT_ENV)
    return _valid_contact(configured or "") or DEFAULT_USER_AGENT
