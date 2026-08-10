# Tailscale access

The cockpit has no application login. Tailnet device membership and Tailscale
ACLs/grants are the access boundary.

## Preferred long-running service topology

Keep Flask bound to loopback and let Tailscale Serve terminate private HTTPS:

```powershell
tailscale serve --bg http://127.0.0.1:7421
tailscale serve status
```

The dashboard service itself continues to start without `--tailscale`, so it is
not reachable through the LAN or the machine's Tailnet IP directly. Tailscale
Serve exposes the exact `https://<machine>.<tailnet>.ts.net` origin only inside
the Tailnet and proxies it to loopback. Add that exact origin to
`COMMENTS_SERVER_CORS_WHITELIST`; do not whitelist `*.ts.net`.

## Direct Tailnet-IP mode

For an interactive process without Tailscale Serve, start the server from the
repository root:

```powershell
python execution/sqlite_bootstrap.py execution/comments_server.py --tailscale
```

The server asks the local Tailscale client for its IPv4 address and binds only
to that explicit `100.64.0.0/10` address. Open `http://<tailscale-ip>:7421` from
another device that your Tailnet policy permits.

Safety properties:

- Without `--tailscale`, the server binds to `127.0.0.1`.
- Wildcard and ordinary LAN binds are rejected.
- Requests must come from loopback or a Tailscale address.
- Browser origins are limited to loopback, Tailscale addresses, and the exact
  configured private HTTPS origin. An unrelated `.ts.net` hostname is not
  trusted merely because it shares the suffix.
- Any permitted Tailnet device can read and mutate the single-user workspace;
  use Tailscale ACLs or grants to restrict which devices can reach port 7421.
- Static `file://` reports use a local bearer capability for writes. The
  capability is stored under the gitignored `data/secrets/` directory.

Both modes are intended for the owner's private Tailnet, not for public
exposure, Funnel, ordinary reverse proxies, subnet-router exposure, or
shared-user hosting.
