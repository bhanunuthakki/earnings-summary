# Technical Audit: Secure Mobile-Browser Access & URL Connectivity (BHA-28)

**Document ID:** `RESEARCH-2026-BHA-28-MOBILE-ACCESS`  
**Date:** 2026-08-15  
**Author:** AI Agentic Engineering  
**Scope:** `execution/comments_server.py`, `src/server_runtime/access.py`, `src/ui/`  
**Status:** Completed Investigation & Reference Architecture

---

## 1. Executive Summary

The research platform operates a localhost-first Flask server (`execution/comments_server.py` on default port `7421`) backed by `data/portfolio.db` (SQLite in WAL mode). Accessing this platform from a mobile browser (e.g., iPhone / iPad) requires resolving two primary engineering constraints:

1. **Security & Network Boundaries:** The server exposes direct financial state mutations, LLM execution pipelines, and maintenance CLIs without application-level user login forms. Exposing port 7421 to an open LAN or public internet via `0.0.0.0` introduces severe risk of unauthorized mutation or LLM quota exhaustion.
2. **UI & Viewport Ergonomics:** Financial tables, multi-column research screens, and interactive drawers designed for desktop displays must adapt gracefully to mobile touch screens, notch safe-areas, and virtual keyboard viewports.

This audit evaluates network connectivity patterns, CSRF/origin defenses, and mobile layout conformance, establishing a production-grade blueprint for secure mobile research access.

---

## 2. Network Topology & Host Binding Architecture

### 2.1 Threat Model for LAN Binding (`0.0.0.0`)

Binding directly to `0.0.0.0:7421` on a home/office Wi-Fi network exposes:
- **Write Endpoints:** `/comments` (POST/PATCH/DELETE), `/api/journal` note creation, `/api/dcf` scenario overrides.
- **Compute & Execution Hooks:** Maintenance endpoints (`seed_kpis`, `process_inbox`, `sweep_history`) dispatching CLI subprocesses.
- **LLM Quota Consumption:** `/api/ask/stream` generating live SSE responses against the user's shared CLI quota.

> [!WARNING]
> Raw `0.0.0.0` binding without mutual authentication is forbidden. `server_runtime/access.py` strictly validates bind addresses via `validate_bind_host()`.

### 2.2 Recommended Architecture: Encrypted Tailnet Overlay (Tailscale)

The platform already incorporates native Tailscale integration (`--tailscale` CLI flag and `server_runtime/access.py`):

```
┌────────────────────────────────────────────────────────┐
│                   Private Tailnet                      │
│                                                        │
│  ┌───────────────────────┐    WireGuard Encrypted      │
│  │ Mobile Device (iOS)   │◄─────────────────────────┐  │
│  │ 100.x.y.z             │                          │  │
│  └───────────────────────┘                          │  │
│                                                     ▼  │
│                                      ┌────────────────┐│
│                                      │ Host Workstation││
│                                      │ 100.a.b.c:7421 ││
│                                      │ comments_server││
│                                      └────────────────┘│
└────────────────────────────────────────────────────────┘
```

**Key Advantages:**
- **Zero-Trust Encryption:** WireGuard peer-to-peer encapsulation across all network hops.
- **CGNAT IP Validation:** `server_runtime/access.py` verifies client IP addresses against the Tailscale CGNAT range (`100.64.0.0/10` IPv4 and `fd7a:115c:a1e0::/48` IPv6).
- **No Ingress Port Forwarding:** Router firewall remains completely closed; no NAT traversal risks.

### 2.3 Alternative: Reverse Proxy with Mutual TLS (mTLS) or Cloudflare Access

When remote access without the Tailscale client app is required:
- Deploy a lightweight reverse proxy (Caddy / Nginx) on localhost.
- Terminate HTTPS with Let's Encrypt / Tailscale HTTPS certs.
- Enforce HTTP Basic Auth or Cloudflare Access Zero Trust JWT validation before proxying upstream to `127.0.0.1:7421`.

---

## 3. Security, Origin & CSRF Defense Audit

### 3.1 Origin Verification Contract (`is_allowed_origin`)

In `src/server_runtime/access.py`:
- **No Wildcards:** `Access-Control-Allow-Origin: *` is never emitted.
- **Strict Whitelist Echo:** The server validates the `Origin` header against:
  1. `null` (supporting local `file://` static HTML reports).
  2. Loopback hostnames: `localhost`, `127.0.0.1`, `::1`.
  3. Tailscale hostnames/IPs when `COMMENTS_SERVER_ALLOW_TAILSCALE=1`.
  4. Explicit origins declared in `COMMENTS_SERVER_CORS_WHITELIST`.
- **Private Mobile URL Pinning:** The `EARNINGS_SUMMARY_PRIVATE_BASE_URL` environment variable allows pinning an exact HTTPS mobile domain (e.g., `https://research.my-tailnet.ts.net`).

### 3.2 Static Report Capability Bearer Tokens

- Static HTML reports opened on mobile or desktop carry a cryptographic capability token in `X-Report-Capability`.
- Managed by `ReportCapabilityStore` (`src/server_runtime/access.py`) using constant-time HMAC comparison (`hmac.compare_digest`), preventing unauthorized local network probing.

---

## 4. Mobile Viewport & Touch Ergonomics Audit

### 4.1 Viewport Metadata & Scaling

All primary UI surfaces (Workspace Shell, Operations Hub, Research Cockpit) inline standard responsive viewport tags:
```html
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
```

### 4.2 Touch Target Compliance

- Design System Token: `--touch-target-size: 44px;` (defined in `src/ui/tokens.py`).
- All interactive controls (`.k-btn`, `.k-chip-btn`, tab headers, action cards) bind `min-block-size: var(--touch-target-size);`, preventing miss-taps on high-density displays.

### 4.3 Responsive Layout Patterns

1. **Harvey/Legora 3-Layer Sidebar:**
   - Desktop: Persistent 260px navigation rail.
   - Mobile (<768px): Collapses into an off-canvas drawer with sliding gesture / backdrop scrim.
2. **Dense Financial Tables (`.p-table`):**
   - Wrapped in `<div style="overflow-x:auto;">` containers with inertial touch scrolling (`-webkit-overflow-scrolling: touch;`).
   - Sticky ticker/metric labels on horizontal swipe.
3. **Ask Copilot Drawer:**
   - Bottom sheet drawer with virtual keyboard accommodation and auto-scrolling streaming output.

---

## 5. Implementation Runbook for Mobile Access

### 5.1 Starting the Cockpit on Tailscale

1. Verify Tailscale is active on the workstation:
   ```bash
   tailscale status
   ```
2. Start `comments_server.py` with Tailscale binding:
   ```bash
   python execution/comments_server.py --tailscale --port 7421
   ```
3. On the mobile device:
   - Ensure Tailscale is connected on iOS / Android.
   - Navigate to `http://<tailscale-machine-name>:7421` or `http://100.x.y.z:7421`.

### 5.2 Environmental Checklist for iOS WebApp Mode

To install the research platform as a standalone iOS Home Screen Progressive Web App:
- Add `apple-mobile-web-app-capable` meta tag:
  ```html
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  ```
- Incorporate CSS safe-area padding:
  ```css
  padding-top: env(safe-area-inset-top);
  padding-bottom: env(safe-area-inset-bottom);
  ```

---

## 6. Audit Conclusion & Compliance Status

- **Network Security:** PASS (Strict loopback + Tailscale CGNAT enforcement).
- **CSRF & Capability Auth:** PASS (Dynamic origin echo, constant-time bearer token check).
- **Touch Target Ergonomics:** PASS (All buttons/chips conform to >=44px touch targets).
- **Mobile Viewport Conformance:** PASS (Fluid typography, horizontal scroll tables, responsive CSS custom variables).
