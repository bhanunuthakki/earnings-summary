from __future__ import annotations

from pathlib import Path

import pytest

from server_runtime.access import (
    REPORT_CAPABILITY_HEADER,
    ReportCapabilityStore,
    is_allowed_client_address,
    is_allowed_origin,
    resolve_tailscale_ipv4,
)


def test_network_policy_allows_only_loopback_by_default() -> None:
    assert is_allowed_client_address("127.0.0.1", allow_tailscale=False)
    assert is_allowed_client_address("::1", allow_tailscale=False)
    assert not is_allowed_client_address("100.100.1.2", allow_tailscale=False)
    assert not is_allowed_client_address("192.168.1.20", allow_tailscale=False)


def test_network_policy_allows_tailnet_but_not_lan_when_enabled() -> None:
    assert is_allowed_client_address("100.64.0.1", allow_tailscale=True)
    assert is_allowed_client_address("100.127.255.254", allow_tailscale=True)
    assert is_allowed_client_address("fd7a:115c:a1e0::1", allow_tailscale=True)
    assert not is_allowed_client_address("100.128.0.1", allow_tailscale=True)
    assert not is_allowed_client_address("10.0.0.7", allow_tailscale=True)


def test_origin_policy_accepts_tailnet_origins_only_in_tailnet_mode() -> None:
    origin = "http://100.100.1.2:7421"
    assert is_allowed_origin(origin, allow_tailscale=True, whitelist=frozenset()) == origin
    assert is_allowed_origin(origin, allow_tailscale=False, whitelist=frozenset()) is None
    assert (
        is_allowed_origin(
            "https://laptop.example-tailnet.ts.net",
            allow_tailscale=True,
            whitelist=frozenset(),
        )
        == "https://laptop.example-tailnet.ts.net"
    )
    assert (
        is_allowed_origin(
            "https://attacker.example",
            allow_tailscale=True,
            whitelist=frozenset(),
        )
        is None
    )


def test_report_capability_is_stable_and_never_empty(tmp_path: Path) -> None:
    store = ReportCapabilityStore(tmp_path)
    first = store.load_or_create()
    second = store.load_or_create()
    assert first
    assert first == second
    assert store.matches(first)
    assert not store.matches("")
    assert not store.matches(first + "x")
    assert REPORT_CAPABILITY_HEADER == "X-Report-Capability"


def test_resolve_tailscale_ipv4_rejects_non_tailnet_output() -> None:
    with pytest.raises(RuntimeError, match="Tailscale"):
        resolve_tailscale_ipv4(lambda: "192.168.1.5\n")


def test_resolve_tailscale_ipv4_accepts_first_tailnet_address() -> None:
    assert resolve_tailscale_ipv4(lambda: "100.90.2.3\n") == "100.90.2.3"
