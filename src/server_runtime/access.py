"""Network and static-report capability policy for the cockpit server."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import subprocess
from collections.abc import Callable, Collection
from pathlib import Path
from urllib.parse import urlparse

from runtime.secrets import create_secret_text, secret_read_path, secret_write_path

REPORT_CAPABILITY_HEADER = "X-Report-Capability"
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PRIVATE_MOBILE_BASE_URL_ENV = "EARNINGS_SUMMARY_PRIVATE_BASE_URL"


def tailscale_access_enabled() -> bool:
    return os.environ.get("COMMENTS_SERVER_ALLOW_TAILSCALE", "").lower() in _TRUE_VALUES


def _parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def is_tailscale_address(value: str) -> bool:
    address = _parse_address(value)
    return address is not None and (address in _TAILSCALE_V4 or address in _TAILSCALE_V6)


def is_allowed_client_address(value: str, *, allow_tailscale: bool) -> bool:
    """Accept only loopback, plus Tailnet addresses when explicitly enabled."""
    address = _parse_address(value)
    if address is None:
        return False
    return address.is_loopback or (allow_tailscale and is_tailscale_address(value))


def is_allowed_origin(
    origin: str,
    *,
    allow_tailscale: bool,
    whitelist: Collection[str],
) -> str | None:
    """Return the origin to echo when it belongs to an approved browser surface."""
    if not origin:
        return None
    if origin == "null":
        return origin
    try:
        parsed = urlparse(origin)
        hostname = parsed.hostname or ""
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    if origin in whitelist:
        return origin
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return origin
    if not allow_tailscale:
        return None
    if is_tailscale_address(hostname):
        return origin
    return None


def private_mobile_origin(
    *,
    explicit: str | None = None,
    config_path: Path | None = None,
) -> str | None:
    """Return one validated origin for the private mobile surface.

    The value must be origin-only. HTTPS is mandatory except for an explicit
    loopback development origin. A checked-in caller may provide the ignored
    service-config path; the environment remains the first production source.
    """
    if explicit is not None:
        raw = explicit
    else:
        raw = os.environ.get(_PRIVATE_MOBILE_BASE_URL_ENV, "")
        if not raw.strip() and config_path is not None:
            try:
                raw = config_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
    value = raw.strip().rstrip("/")
    if not value:
        return None
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
    except ValueError:
        return None
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    if parsed.scheme == "https":
        return f"https://{parsed.netloc.lower()}"
    if parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}:
        return f"http://{parsed.netloc.lower()}"
    return None


class ReportCapabilityStore:
    """Stable bearer capability used only by static ``file://`` reports."""

    def __init__(self, repo_root: Path) -> None:
        self._read_path = secret_read_path("report_capability", repo_root=repo_root)
        self._write_path = secret_write_path("report_capability")

    def load(self) -> str | None:
        configured = os.environ.get("COMMENTS_SERVER_REPORT_CAPABILITY", "").strip()
        if configured:
            return configured
        try:
            value = self._read_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def load_or_create(self) -> str:
        existing = self.load()
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        created = create_secret_text(self._write_path, token)
        self._read_path = self._write_path
        if not created:
            raced = self.load()
            if raced:
                return raced
            raise RuntimeError("report capability file exists but is empty") from None
        return token

    def matches(self, candidate: str) -> bool:
        expected = self.load()
        return bool(expected and candidate and hmac.compare_digest(expected, candidate))


def _tailscale_cli_output() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Tailscale CLI could not provide a Tailnet IPv4 address") from exc
    return result.stdout


def resolve_tailscale_ipv4(output_provider: Callable[[], str] | None = None) -> str:
    provider = output_provider or _tailscale_cli_output
    for line in provider().splitlines():
        candidate = line.strip()
        if candidate and is_tailscale_address(candidate):
            address = _parse_address(candidate)
            if isinstance(address, ipaddress.IPv4Address):
                return candidate
    raise RuntimeError("Tailscale did not report a valid Tailnet IPv4 address")


def validate_bind_host(host: str, *, allow_tailscale: bool) -> str:
    """Reject wildcard/LAN exposure; the server may bind only loopback or its Tailnet IP."""
    if is_allowed_client_address(host, allow_tailscale=allow_tailscale):
        return host
    raise ValueError("host must be loopback or an explicit Tailscale address")
