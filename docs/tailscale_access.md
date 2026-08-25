# Tailscale access

The cockpit has no application login. Tailnet device membership and Tailscale
ACLs/grants are the access boundary.

## Preferred long-running service topology

Keep Flask bound to loopback and let Tailscale Serve terminate private HTTPS:

```powershell
tailscale serve --bg http://127.0.0.1:7421
tailscale serve status
```

Treat the HTTPS URL printed by live `tailscale serve status` as canonical. Do
not derive it from the Windows computer name, a cached Tailscale device name or
IP, or the DNS name shown by `tailscale status`. If the host was renamed and
Serve still prints the old name, repair the mapping before updating clients:

```powershell
tailscale serve reset
tailscale serve --bg 7421
tailscale serve status
```

Copy the resulting HTTPS origin exactly into
`COMMENTS_SERVER_CORS_WHITELIST`, restart `es-dashboard`, and verify a local
`127.0.0.1:7421` request plus a cross-machine request to that HTTPS URL.

The dashboard service itself continues to start without `--tailscale`, so it is
not reachable through the LAN or the machine's Tailnet IP directly. Tailscale
Serve exposes the exact `https://<machine>.<tailnet>.ts.net` origin only inside
the Tailnet and proxies it to loopback. Add that exact origin to
`COMMENTS_SERVER_CORS_WHITELIST`; do not whitelist `*.ts.net`.

## Supported access boundary

Direct Tailnet-IP binding is not a supported Mac-to-Windows topology. Do not
start the persistent dashboard with `--tailscale` or direct another device to a
raw `100.64.0.0/10` address. Keep the application on loopback and use the live
Tailscale Serve HTTPS origin described above.

Safety properties:

- The persistent dashboard binds to `127.0.0.1`.
- Wildcard, ordinary LAN, and direct Tailnet-IP listeners are not part of the
  supported deployment.
- Browser origins are limited to loopback and the exact configured private
  HTTPS origin. An unrelated `.ts.net` hostname is not trusted merely because
  it shares the suffix.
- Any device permitted by the Tailnet policy to reach the Serve origin can read
  and mutate the single-user workspace; use Tailscale ACLs or grants to scope
  access.
- Static `file://` reports use a local bearer capability for writes. The
  capability is stored under the gitignored `data/secrets/` directory.

This topology is intended for the owner's private Tailnet, not for public
exposure, Funnel, ordinary reverse proxies, subnet-router exposure, or
shared-user hosting.
