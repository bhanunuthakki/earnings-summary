"""Redact credentials, PII, paths, and URLs at operational boundaries.

``requests`` exceptions often embed request URLs, including query strings.
Route untrusted exception and receipt text through :func:`redact` before it is
logged, or through :func:`sanitize_operational_text` before it is persisted or
presented.
"""

from __future__ import annotations

import re
from typing import Literal

_CREDENTIAL_NAME = (
    r"x[ _-]*api[ _-]*key|apikey|api[ _-]*key|access[ _-]*token|auth[ _-]*token|"
    r"refresh[ _-]*token|id[ _-]*token|client[ _-]*secret|private[ _-]*token|"
    r"session(?:[ _-]*(?:id|token))?|password|passphrase|secret|token|credential|key|"
    r"x[ _-]*amz[ _-]*(?:credential|signature|security[ _-]*token)|"
    r"x[ _-]*goog[ _-]*(?:credential|signature|security[ _-]*token)|"
    r"sig|signature|se|sp|sv|srt|ss|skoid|sktid|skt|ske|skv"
)
_QUERY_CRED_RE = re.compile(
    rf"(?P<prefix>[?&])(?P<param>{_CREDENTIAL_NAME})(?P<sep>=)(?P<val>[^&\s;]+)",
    re.IGNORECASE,
)
_CRED_RE = re.compile(
    rf"(?<![A-Za-z0-9_?&-])(?P<param>{_CREDENTIAL_NAME})"
    r"(?P<sep>[ ]*(?:=|:)[ ]*)(?P<val>"
    r'"(?:\\.|[^"\r\n])*"|'
    r"'(?:\\.|[^'\r\n])*'|"
    r"[^&\r\n;,]+)",
    re.IGNORECASE,
)
_AUTH_BASIC_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:proxy-)?authorization[ \t]*:[ \t]*basic[ \t]+)"
    r"(?P<val>[^\s]+)"
)
_AUTH_BEARER_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:proxy-)?authorization[ \t]*:[ \t]*bearer[ \t]+)"
    r"(?P<val>[^\s]+)"
)
_X_API_KEY_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9_-])x[ _-]*api[ _-]*key[ \t]*:[ \t]*)"
    r"(?P<val>[^\r\n;,]+)"
)
_BEARER_RE = re.compile(
    r"(?i)(\bbearer[ \t]+)[A-Za-z0-9._~+/\-]{8,}=*"
    r"(?=$|[^A-Za-z0-9._~+/\-=])"
)
_BASIC_RE = re.compile(r"(?i)(\bbasic\s+)(?=[A-Za-z0-9+/=._\-]*[0-9+/=])[A-Za-z0-9+/=._\-]{8,}")
_BASIC_URL_RE = re.compile(r"(?i)(\bhttps?://)[^/\s:@]+:[^/\s@]+@")
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:apikey|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|'
    r"id[_-]?token|client[_-]?secret|private[_-]?token|session(?:[_-]?(?:id|token))?|"
    r'token|secret|password|passphrase|credential|key|signature)"'
    r'\s*:\s*")[^"]*(")'
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://[^\s<>\"'\u00b7;]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^<>\r\n\"'\u00b7;]*")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?!/)[^<>\r\n\"'\u00b7;]*")
_MAX_OPERATIONAL_TEXT = 240

OperationalTextMode = Literal["persisted", "presentation"]


def redact(text: object) -> str:
    """Mask credentials and email local-parts in ``text``."""

    sanitized = str(text)
    sanitized = _BASIC_URL_RE.sub(lambda match: f"{match.group(1)}***:***@", sanitized)
    sanitized = _AUTH_BASIC_HEADER_RE.sub(lambda match: f"{match.group('prefix')}***", sanitized)
    sanitized = _AUTH_BEARER_HEADER_RE.sub(lambda match: f"{match.group('prefix')}***", sanitized)
    sanitized = _X_API_KEY_HEADER_RE.sub(lambda match: f"{match.group('prefix')}***", sanitized)
    sanitized = _QUERY_CRED_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('param')}{match.group('sep')}***",
        sanitized,
    )
    sanitized = _CRED_RE.sub(
        lambda match: f"{match.group('param')}{match.group('sep')}***", sanitized
    )
    sanitized = _BEARER_RE.sub(lambda match: f"{match.group(1)}***", sanitized)
    sanitized = _BASIC_RE.sub(lambda match: f"{match.group(1)}***", sanitized)
    sanitized = _JSON_SECRET_RE.sub(lambda match: f"{match.group(1)}***{match.group(2)}", sanitized)
    return _EMAIL_RE.sub(lambda match: f"***{match.group(1)}", sanitized)


def sanitize_operational_text(text: object, *, mode: OperationalTextMode) -> str:
    """Redact and bound untrusted operational text for storage or display.

    Persisted receipts retain only path/URL placeholders. Presentation may
    retain credential-redacted public HTTP(S) URLs, while absolute paths and
    file URLs remain opaque. Both modes return at most 240 characters.
    """

    sanitized = redact(text)
    protected: list[str] = []
    if mode == "persisted":
        sanitized = _URL_RE.sub(
            lambda match: "[path]" if match.group(0).casefold().startswith("file://") else "[url]",
            sanitized,
        )
    elif mode == "presentation":

        def protect_url(match: re.Match[str]) -> str:
            url = match.group(0)
            protected.append("file://[path]" if url.casefold().startswith("file://") else url)
            return f"\x00URL{len(protected) - 1}\x00"

        sanitized = _URL_RE.sub(protect_url, sanitized)
    else:  # pragma: no cover - Literal callers are statically closed
        raise ValueError(f"unsupported operational text mode: {mode}")
    sanitized = _WINDOWS_PATH_RE.sub("[path]", sanitized)
    sanitized = _POSIX_PATH_RE.sub("[path]", sanitized)
    if mode == "presentation":
        for index, url in enumerate(protected):
            sanitized = sanitized.replace(f"\x00URL{index}\x00", url)
    return sanitized[:_MAX_OPERATIONAL_TEXT]
