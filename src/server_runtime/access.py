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

REPORT_CAPABILITY_HEADER = "X-Report-Capability"
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


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
    if origin in whitelist:
        return origin
    try:
        parsed = urlparse(origin)
        hostname = parsed.hostname or ""
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return origin
    if not allow_tailscale:
        return None
    if is_tailscale_address(hostname) or hostname.lower().endswith(".ts.net"):
        return origin
    return None


class ReportCapabilityStore:
    """Stable bearer capability used only by static ``file://`` reports."""

    def __init__(self, repo_root: Path) -> None:
        self._path = repo_root / "data" / "secrets" / "report_capability"

    def load(self) -> str | None:
        configured = os.environ.get("COMMENTS_SERVER_REPORT_CAPABILITY", "").strip()
        if configured:
            return configured
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def load_or_create(self) -> str:
        existing = self.load()
        if existing:
            return existing
        self._path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        try:
            with self._path.open("x", encoding="utf-8") as handle:
                handle.write(token)
        except FileExistsError:
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
