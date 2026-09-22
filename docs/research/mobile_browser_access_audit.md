# Mobile-browser access: historical proposal and remaining validation (BHA-28)

**Document ID:** `RESEARCH-2026-BHA-28-MOBILE-ACCESS`
**Original inspection:** 2026-08-15
**Evidence correction:** 2026-09-19
**Status:** Code-inspection proposal; phone/network acceptance pending

## Evidence boundary

The original document inspected `execution/comments_server.py`,
`src/server_runtime/access.py`, and `src/ui/`. It did not retain a phone connection
receipt, observed mobile task results, or a deployment security review. Its earlier
Security, CSRF, Touch Target, and Mobile Viewport PASS labels were unsupported and
are withdrawn. Source code can establish available mechanisms; it cannot establish
that a particular device, network, or deployed configuration works securely.

This document does not authorize listener changes, new exposure, service startup,
production state access, or activation. It is not the live-host runbook.

## Current authority and URL contract

Resolve the live host through the shared `machine-operations` procedure available
in the agent runtime, and follow [agent host operations](../../directives/agent_host_operations.md).
Those owners supersede the original direct-Tailnet-bind instructions.

- Canonical Windows `es-dashboard` owns `127.0.0.1:7421`; the tracker owns
  `127.0.0.1:8000` on that host. Both remain loopback-only.
- Only the dashboard is exposed through the configured private Tailscale Serve
  HTTPS origin. The required URL is the **exact origin reported by live
  `tailscale serve status` on Windows**, shaped as `https://<Serve authority>`.
  A remembered hostname, raw Tailnet IP, or guessed URL is not a substitute.
- A phone's `127.0.0.1` refers to the phone, so the desktop loopback URL cannot
  reach the Windows application. The phone needs authorized access to the private
  origin and its network/identity boundary.
- Production database authority comes from approved external runtime
  configuration. Checkout-default `data/portfolio.db` is not a live, fallback,
  replica, or roster authority and must not be used for this investigation.
- Do not bind this application to `0.0.0.0` or directly to a Tailnet address as a
  workaround. Do not expose the tracker separately or use Funnel. Existing
  `--tailscale` compatibility code does not override current hosting policy.

## Connectivity options

| Option | Assessment | Prerequisites and limits |
|---|---|---|
| Direct same-LAN HTTP listener | Ruled out for the current deployment | Would require changing the loopback boundary and would expose mutation/LLM endpoints without the existing private HTTPS ingress. Being on the same Wi-Fi is not authorization. |
| Configured private mesh plus Tailscale Serve HTTPS | Recommended path to validate, on the same LAN and remotely | Requires the phone's authorized mesh identity, access policy, working HTTPS origin, and healthy canonical backend. Access-policy mistakes, mesh/DNS/certificate availability, device loss and backend outage remain failure modes. |
| Separate authenticated HTTPS reverse proxy | Alternative proposal only | Could support a different device-access model, but adds identity/session enforcement, trusted-proxy/origin handling, certificates, updates and recovery ownership. No implementation or security approval is established here. |

The existing private ingress is the smallest candidate because it preserves the
canonical listener ownership. Current account costs, device/access-policy limits,
and any additional proxy operating cost have not been verified; do not infer that
an alternative is free or maintenance-free.

## Security checks still required

The code contains client-address/origin checks and a report capability store. That
is not evidence that every mobile read/write route is authorized correctly.
Before calling mobile access ready, retain evidence for the actual configuration:

- Phone identity and least-privilege network access; unauthorized-device denial.
- Valid HTTPS origin/certificate, trusted proxy behavior, and no direct backend
  ingress or separately exposed tracker.
- Exact-origin configuration, cookies/session behavior where used, capability
  handling, CSRF rejection and absence of credential-bearing URLs/logs.
- Authorization for mutation and LLM actions, applicable rate/budget limits, and
  readable failure states when a dependency is unavailable.

No security PASS is assigned until these applicable controls are observed on the
approved deployment. The original generic Basic Auth/Cloudflare suggestions are
not an approved replacement for this review.

## Phone and research-task validation still required

Responsive CSS, viewport metadata and a 44px touch token exist in the codebase.
Their presence does not prove that every rendered control meets the target or that
the virtual keyboard leaves the application usable.

Record device, browser/version, viewport, exact deployed source identity and
network path for each observation. Test the authorized private HTTPS route once
on the same LAN and once from a remote network. Then exercise:

- Portfolio → Company Desk navigation, company switching and context identity.
- Earnings calendar and available/unavailable brief doorways.
- Full Brief open/Back/Close, source peeks and competing overlay focus behavior.
- Copilot open/minimize/restore, draft/scroll retention and virtual-keyboard fit.
- Governed Playground ticker/catalog changes, table scrolling and provenance.
- Touch targets, zoom/readability, safe areas, keyboard focus and explicit
  loading/error/unavailable states at supported widths.

No phone or remote-network result is recorded in this artifact. Split any
observed connectivity or interaction defect into its owning implementation issue;
do not treat proposed PWA metadata or CSS snippets as completed mobile support.

## Startup, health and recovery evidence

Use the canonical host's existing service ownership and approved runbook; do not
start another dashboard on a development computer. Read-only acceptance should
first establish the deployed identity, Windows-local dashboard `/healthz`, tracker
health, the exact Serve origin, and real dashboard hydration through that origin.
A healthy port alone is insufficient.

For any separately authorized ingress/configuration change, retain the prior
Serve and origin configuration, named service owner, exact change, health probes,
and the owner's approved recovery plan before execution. Recovery must be able to
disable the new ingress or restore the prior approved configuration while leaving
the canonical loopback services and database authority intact. The exact live
configuration and rollback commands have not been inspected here and are not
invented in this document.

## Decision

**NO-GO for declaring BHA-28 mobile readiness complete or activating a new access
configuration from this document.** Validate the existing canonical private HTTPS
route on the authorized phone and both network paths, complete the applicable
security/task observations, and record concrete health/recovery evidence first.
This pending decision does not claim that the existing desktop private origin is
broken, and does not authorize disabling an existing service.
