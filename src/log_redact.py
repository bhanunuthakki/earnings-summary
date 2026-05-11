"""Redact credentials from strings before logging.

`requests.HTTPError` (and most connection errors) embed the full URL —
including the query string — in their message. Any code path that does
``print(f"... {exc}")`` or ``log.error(error=str(exc))`` will therefore
write the API key to stderr / disk logs. Route exception strings (and
any other untrusted text that may contain a URL) through `redact()`
before logging.

The regex covers the credential-bearing query params we know are in use
across the codebase plus the most common variants seen in third-party
APIs — apikey, api_key, access_token, auth_token, password, secret —
case-insensitive. Add new param names here when integrating a new
provider; the redactor is the single source of truth.
"""

from __future__ import annotations

import re

_CRED_RE = re.compile(
    r"(?P<param>apikey|api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)=(?P<val>[^&\s]+)",
    re.IGNORECASE,
)


def redact(text: object) -> str:
    """Mask credential-bearing query params in ``text``.

    Returns the input (coerced via ``str()``) with any
    ``<param>=<value>`` pair replaced by ``<param>=***`` for known
    credential parameter names. Safe to call on strings, exceptions, or
    anything else — non-strings go through ``str()`` first.
    """
    return _CRED_RE.sub(lambda m: f"{m.group('param')}=***", str(text))
