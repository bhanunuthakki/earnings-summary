# Tailscale access

The cockpit has no application login. In Tailscale mode, Tailnet device
membership and Tailscale ACLs are the access boundary.

Start the server from the repository root:

```powershell
python execution/comments_server.py --tailscale
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

This mode is intended for the owner's private Tailnet, not for public exposure,
reverse proxies, subnet routers, or shared-user hosting.
