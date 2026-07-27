"""Redact credentials and PII from strings before logging.

`requests.HTTPError` (and most connection errors) embed the full URL —
including the query string — in their message. Any code path that does
``print(f"... {exc}")`` or ``log.error(error=str(exc))`` will therefore write
the API key to stderr / disk logs. Route exception strings (and any other
untrusted text that may contain a URL, an auth header, or a JSON body) through
`redact()` before logging.

What gets masked:
  - credential-bearing URL query params (``?apikey=…``, ``&access_token=…``);
  - ``Authorization: Bearer <token>`` / bare ``Bearer <token>``;
  - JSON-body secrets (``"api_key": "…"``, ``"token"``, ``"secret"``,
    ``"password"``, ``"access_token"``, ``"auth_token"``);
  - email addresses — the identifying local part is masked, the domain kept
    for triage (``bhanu@gmail.com`` → ``***@gmail.com``).

Add new credential param/key names below when integrating a new provider; the
redactor is the single source of truth.
"""

from __future__ import annotations

import re

# ?apikey=… / &access_token=… in a URL query string.
_CRED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<param>"
    r"apikey|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?secret|private[_-]?token|session(?:[_-]?(?:id|token))?|"
    r"password|passphrase|secret|token|credential|key|"
    r"x-amz-(?:credential|signature|security-token)|"
    r"x-goog-(?:credential|signature|security-token)|"
    r"sig|signature|se|sp|sv|srt|ss|skoid|sktid|skt|ske|skv"
    r")=(?P<val>[^&\s]+)",
    re.IGNORECASE,
)

# Authorization: Bearer <token> (or a bare "Bearer <token>"). The {8,} length
# floor avoids masking the English word "bearer" followed by a short word.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")

# JSON / dict body: "api_key": "…", "token": "…", etc. Masks the quoted value.
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:apikey|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|'
    r"id[_-]?token|client[_-]?secret|private[_-]?token|session(?:[_-]?(?:id|token))?|"
    r'token|secret|password|passphrase|credential|key|signature)"'
    r'\s*:\s*")[^"]*(")'
)

# Email address — mask the identifying local part, keep the domain for triage.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def redact(text: object) -> str:
    """Mask credentials and PII in ``text`` before it is logged.

    Coerces ``text`` via ``str()`` then masks credential query params, Bearer
    tokens, JSON-body secrets, and email local-parts. Safe to call on strings,
    exceptions, or anything else — non-strings go through ``str()`` first.
    """
    s = str(text)
    s = _CRED_RE.sub(lambda m: f"{m.group('param')}=***", s)
    s = _BEARER_RE.sub(lambda m: f"{m.group(1)}***", s)
    s = _JSON_SECRET_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", s)
    return _EMAIL_RE.sub(lambda m: f"***{m.group(1)}", s)
