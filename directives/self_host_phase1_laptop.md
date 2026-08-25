# Self-host Phase 1 — the laptop as an always-on, phone-reachable server

**Goal:** close the lid and still reach the dashboard from your phone — **$0, no new hardware, no Linux port.** This is "option 3" (stay Windows) made permanent: the laptop becomes a closed-lid, plugged-in, never-sleeping server on your private Tailscale mesh. It's also the exact validation step before any dedicated N100/VPS (see [`self_host_scoping.md`](self_host_scoping.md)).

**Effort:** ~1 hour. Steps marked **[You]** are hands-on (installs, phone enrollment, web consoles); the rest are copy-paste PowerShell (Admin).

The long-running service executes code from the clean runtime checkout at
`C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary` and points
`--repo-root` at the canonical data checkout
`C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary`. Never launch a
service from a task worktree or resolve an arbitrary `python` from `PATH`.

---

## Step 1 — Never sleep on AC; lid-close does nothing

```powershell
# never sleep / hibernate while plugged in
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10   # screen can still turn off
# lid close on AC = do nothing
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

Leave it plugged in. (On battery it will still sleep — that's fine; it's a desk server now.)

## Step 2 — Supervise the two services with NSSM (auto-start at boot, auto-restart on ANY exit)

NSSM fixes the two supervision gaps: the dashboard has no supervisor today, and the poller's Task Scheduler trigger is blind to a clean exit (the documented death mode).

**[You]** Download NSSM (`nssm.exe`) from nssm.cc and put it on PATH (e.g. `C:\Windows\System32`).

```powershell
$appRoot  = "C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary"
$dataRoot = "C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary"
$py       = "$appRoot\venv\Scripts\python.exe"
$bootstrap = "$appRoot\execution\sqlite_bootstrap.py"

if (-not (Test-Path -LiteralPath $py)) {
  $py = "$appRoot\.venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $py)) {
  throw "Managed runtime Python was not found under $appRoot"
}
& $py $bootstrap --check

# --- dashboard: stays bound to 127.0.0.1; Tailscale proxies to it (Step 3) ---
nssm install es-dashboard "$py"
nssm set es-dashboard AppParameters "-u `"$bootstrap`" `"$appRoot\execution\comments_server.py`" --port 7421 --repo-root `"$dataRoot`""
nssm set es-dashboard AppDirectory "$appRoot"
nssm set es-dashboard AppExit Default Restart
nssm set es-dashboard Start SERVICE_AUTO_START

# --- Telegram poller: single instance, restart on any exit ---
nssm install es-poller "$py"
nssm set es-poller AppParameters "-u `"$bootstrap`" `"$appRoot\execution\capture_poller.py`" --repo-root `"$dataRoot`""
nssm set es-poller AppDirectory "$appRoot"
nssm set es-poller AppEnvironmentExtra CAPTURE_WHISPER_MODEL=small.en
nssm set es-poller AppExit Default Restart
nssm set es-poller Start SERVICE_AUTO_START
```

**Before starting the poller service, disable the old Task Scheduler poller** so you don't run two `getUpdates` loops (Telegram 409s the second):

```powershell
schtasks /Change /TN "capture_poller" /DISABLE   # exact name via: schtasks /query | findstr /i poller
nssm start es-dashboard
nssm start es-poller
```

## Step 3 — Tailscale (private mesh, HTTPS, no public port)

**[You]** Install Tailscale on the **laptop** and the **phone** (App Store / Play Store); sign both into the **same** Tailscale account.

```powershell
tailscale up
tailscale serve --bg 7421        # HTTPS via MagicDNS → proxies to 127.0.0.1:7421
tailscale serve status           # prints your https://<host>.<tailnet>.ts.net URL
```

The URL printed by live `tailscale serve status` is the only client-facing
hostname authority. Never synthesize it from the Windows computer name, a
cached device alias/IP, or `tailscale status`. If the serving host has been
renamed and Serve reports a stale hostname, run `tailscale serve reset`, repeat
`tailscale serve --bg 7421`, and use the newly printed URL everywhere below.

`tailscale serve` terminates TLS and forwards to localhost, so **the app never binds a non-loopback port** — there is no public listener to attack. The dashboard is reachable *only* from devices on your tailnet.

**[You]** Keep the tailnet private — don't share nodes and don't enable Funnel (that would expose it publicly). If a second person/device ever joins, add a Tailscale ACL scoping port `7421` to your own device tag.

## Step 4 — Let the browser origin through CORS

Accessed via the MagicDNS name, the browser's `Origin` is the Tailscale FQDN; the server's CORS guard allows only loopback by default and will 403 every AJAX call until you whitelist it.

```powershell
# use the exact https URL from `tailscale serve status`
nssm set es-dashboard AppEnvironmentExtra COMMENTS_SERVER_CORS_WHITELIST=https://<host>.<tailnet>.ts.net
nssm restart es-dashboard
```

## Step 5 — Verify

**[You]** Close the laptop lid. On your phone (Tailscale on), open `https://<host>.<tailnet>.ts.net`. Confirm the dashboard loads **and** the chat/ask panel streams (SSE works — Tailscale passes it through unbuffered, no Cloudflare 100s timeout). Send a Telegram capture and confirm it lands.

If it works: you're done — the laptop can stay closed.

---

## Cheap code-hardening follow-ups (small separate PRs, not laptop steps)

Even behind the mesh, a compromised enrolled device shouldn't trivially pillage the box. From [`self_host_scoping.md`](self_host_scoping.md) §5:

- **Ticker allowlist** — `re.fullmatch(r'[A-Z0-9.\-]{1,10}', ticker.upper())` before the path join on `/dcf/<ticker>` and `/reports/<ticker>` (attacker-controlled filesystem input).
- **`/healthz`** — stop leaking the absolute `repo_root`; return `{'status':'ok'}`.
- **Security headers** `after_request` — `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- **Move secrets** to a `0600` file outside the repo tree and **rotate** the Telegram token + FMP + Gemini keys (they were read during the scoping audit).
- **Confirm the poller is `chat_id`-allowlisted to you** — the one ingress the mesh does not cover.

## When to graduate to a dedicated box

Only if: you want the laptop free to travel, or you want DR/uptime independent of the laptop. Then follow `self_host_scoping.md` Phases 2–6 (N100 mini-PC or Hetzner CX32 + Linux port). Nothing here is wasted — the same Tailscale + services model carries over.
