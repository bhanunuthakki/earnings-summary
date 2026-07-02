# Self-Hosting Scoping Document — earnings-summary

*Prepared for the repo owner · 2026-07-01 · single-user personal equity-research platform*

**Decisions locked (2026-07-01):** Access = **Tailscale private mesh** (no public listener, no in-app login). Immediate host = **the laptop kept on** (closed-lid, plugged-in, never-sleep) via the free Phase-1 below — this is "option 3" made permanent; a dedicated **N100 mini-PC / Hetzner VPS + Linux port** is a *deferred upgrade*, taken only if the laptop needs to travel or DR matters. Concrete laptop setup steps live in [`self_host_phase1_laptop.md`](self_host_phase1_laptop.md).

## 1. Objective & Recommended Architecture

**Goal:** Get the whole platform (dashboard, Telegram capture poller, ~28 crons) off the laptop and running **always-on**, reachable from your **phone or any of your own devices**, without the lid ever needing to stay open — while keeping your portfolio/thesis data off the public internet.

**The recommendation, in one sentence:** Run everything on **one small always-on host** (a **home Intel N100 mini-PC** as the default, or a **Hetzner CX32 Linux VPS** if you want zero hardware) on **Linux**, reach it from your phone over a **private Tailscale mesh** (the app stays bound to `127.0.0.1`, **no public listener**), and rely on **Tailscale's network-level device identity as the auth** — so **no login code has to be written** into the currently-authless Flask app.

The single most important property: **there is no public port and no login page to attack.** For a personal finance dataset, collapsing the attack surface to "compromise one of *my own* enrolled devices" is worth more than any convenience.

---

## 2. Recommended Stack

| Component | Choice | Why | $/mo |
|---|---|---|---|
| **Host** | Home Intel N100 mini-PC (16 GB / 500 GB), *or* Hetzner CX32 (4 vCPU / 8 GB / **80 GB**) | ~20 GB working set (337 MB DB + 9.1 GB fmp cache + 6.1 GB IR docs) fits with headroom; N100 keeps all data in-house, VPS gives DC uptime | ~$2–3 (N100 power) *or* ~$8 (CX32) |
| **OS** | Linux (Ubuntu Server 24.04 LTS) | App Python is already OS-portable (4 guarded `os.name` branches, no pywin32/COM); Linux *fixes* the poller supervision + logon-session defects | $0 |
| **Access** | **Tailscale Personal** (free) + `tailscale serve` for HTTPS | Phone joins the tailnet; zero public exposure; WireGuard-encrypted; MagicDNS gives a stable URL; **device identity = the auth** | $0 |
| **Auth** | **None in-app** — Tailscale network identity + an ACL scoping `:7421` to your devices | App has zero login today; the mesh supplies strong, instantly-revocable, device-scoped identity with no new security-critical code | $0 |
| **Web serving** | Keep Flask app on `127.0.0.1:7421`; add **`waitress`** (1 line) to kill the dev-server banner | Dev server's only real defect for 1 user is the banner + no graceful restart; `threaded=True` already handles the 4-worker SSE pool + your one browser. **Do NOT use gunicorn multi-worker** (overkill) and **NOT `waitress` if you ever front it behind a proxy that buffers SSE** — Tailscale is raw TCP, so SSE just works | $0 |
| **Poller supervision** | **systemd service, `Restart=always`, `Type=simple`** | Restarts on **any** exit (fixes the clean-exit-death bug at the root); single-instance prevents the Telegram `getUpdates` 409 | $0 |
| **DB** | SQLite WAL on **local SSD** (unchanged) | WAL is single-machine; never on NFS/SMB. Migrate via the existing online-backup snapshot, not a raw copy | $0 |
| **Backup** | Keep `cron/backup_db.py`; **repoint `ES_DB_BACKUP_DIR`** to a local path, then add **restic → Backblaze B2** | Google-Drive path disappears on Linux; B2 is ~$0.10/mo for 14×62 MB snapshots and gives off-host DR | ~$0.10–1.00 |
| **LLM transport** | Unchanged — metered `src/llm/cli.py` via `ANTHROPIC_API_KEY`, Gemini fallback | Already headless-safe on Linux (`shutil.which('claude')`, utf-8 forced); the subscription wrapper is **not** used by the app | (metered, see §7) |

---

## 3. The Four Forks — Resolved

**Fork 1 — OS: → Linux (Ubuntu 24.04 LTS).**
The application Python needs essentially no changes; the Windows coupling is confined to the ops shell (Task Scheduler XMLs, `.bat` wrappers, the PowerShell/robocopy backup). Porting is mechanical *and* it structurally repairs the two real defects (poller restart, logged-in-session requirement).
*Runner-up: stay Windows + NSSM.* Pick this only if you want **near-zero effort** — register the existing 33 `schtasks` tasks verbatim and NSSM-wrap just the two always-on services (~2 hours vs 3–5 days). You inherit the fragile Windows ops shell but change almost nothing.

**Fork 2 — Host: → Home N100 mini-PC (~$150 once).**
Cheapest lifetime cost, all financial data stays physically in-house, ample local SSD for the 20 GB set.
*Runner-up: Hetzner CX32 (~$8/mo).* Pick this if you want **no hardware to babysit** and DC-grade uptime, and you're comfortable with the DB + plaintext secrets living on a cloud provider's disk (encrypt + rotate). **Reject the Azure Windows VM (~$35–45/mo)** — 4–5× the cost and it *still* forces the poller/logon fixes.

**Fork 3 — Access: → Tailscale private mesh (no public listener).**
$0, zero public attack surface, no login code, instant device revocation, and SSE works with no proxy-buffering surgery.
*Runner-up: Cloudflare Tunnel + Access (OIDC).* Pick this **only** if "open it on a borrowed browser that can't run Tailscale" becomes a hard requirement. Costs: a domain (~$10/yr), edge-JWT-verification code that must be written correctly, and a **100-second free-tier SSE timeout that breaks chat/ask**. Not worth it for an audience of one.

**Fork 4 — Auth: → None in-app; rely on Tailscale network identity + an ACL.**
For one user there is no privilege hierarchy — "identity" is a single binary check (is-this-my-enrolled-device), which the mesh enforces. Do **not** build RBAC or a login.
*Runner-up: single-user password + signed session cookie (`scaffold-auth`).* Only needed if a **public** path is chosen, or as an optional second factor *behind* Tailscale for extra paranoia. As a sole public-facing gate on the dev server it's the most error-prone option — avoid.

---

## 4. What Must Be Migrated

| Item | Today | Target | Notes |
|---|---|---|---|
| **Web app serve** | `app.run()` Werkzeug dev server, manual `start_comments_server.bat`, no supervision | `waitress` under a **systemd service** (`Restart=on-failure`), bound to `127.0.0.1` | The largest gap between today and phone access — it has **no** supervisor at all |
| **~33 crons** (doc says 28) | UTF-16 `.task.xml` + `.bat` wrappers, `schtasks` | ~30 **systemd timers** (`Persistent=true`) or one crontab; each job is already `python execution/<x>.py` | Run `verify_cron_registration.py` first to get the true set; migrate the real 33 incl. `capture_poller`, `ledger_synthesis`, and the 3 registered-only tasks |
| **Telegram poller** | `LogonTrigger` + `RestartOnFailure` (blind to clean exit → the death mode) | **systemd `Restart=always`, `Type=simple`** | Preserve single-instance (409 guard). Confirm it's **chat_id-allowlisted to you** — the one ingress the mesh does *not* cover |
| **SQLite + backups** | `portfolio.db` (337 MB WAL) in MAIN; backup → Google-Drive Windows path | Consistent **online-backup snapshot** → transfer → `restore_db.py`; `ES_DB_BACKUP_DIR` repointed + restic→B2 | **Trap:** the worktree DB is a 32 KB stub — source from MAIN. `tar` the 9.1 GB/110k-file fmp cache into one archive before transfer |
| **Secrets** | Plaintext `.env` + `data/secrets/*.json` inside repo tree | `/etc/earnings-summary/env` (0600), **outside** repo_root; `CAPTURE_TELEGRAM_TOKEN_FILE` points the token loader out of the tree | **Rotate** the Telegram token, FMP key, and Gemini key after migration — they were read during audit |
| **ffmpeg / whisper** | Hardcoded `C:/ffmpeg/bin` (nt-guarded, no-ops on Linux) | `apt install ffmpeg`; `small.en` (~480 MB) auto-downloads on first voice memo | Near-zero effort |

**Headless subscription-LLM-auth caveat:** The app's own LLM calls use the **metered** path (`ANTHROPIC_API_KEY` set) and run headless on Linux unchanged — **no action needed**. The **subscription** wrapper (`C:/Users/Bhanu/.gemini/snippets/claude_cli.py`) requires an interactive `claude auth login` **and** `ANTHROPIC_API_KEY` *unset* — mutually exclusive with the metered path in one env, and its OAuth can't complete on a headless box. **Do not introduce it onto the server.** No cron script uses it (verified), so it is a red herring for this migration; if you ever need it for ad-hoc scripts, copy `~/.claude` credentials out-of-band and give that script its own env.

---

## 5. Security Posture Before Exposure

The whole security story reduces to **one gate**: never bind off-loopback without an identity layer. Tailscale supplies it at the network level, so the app stays on `127.0.0.1` and **there is no public listener** — the currently-unauthenticated control plane (all ~100 routes, including mutating `/approve`, `/api/capture/text`, chat-apply file writes, job dispatch) is simply unreachable from the internet.

Must-haves before the phone touches it:

1. **Tailscale is the gate.** Keep `--host 127.0.0.1`; reach it via `tailscale serve` (also gives HTTPS). **Never `--host 0.0.0.0` to the public internet.** The CSRF/Origin guard is *not* auth — a `curl` with no Origin is explicitly allowed.
2. **Set a Tailscale ACL** scoping `:7421` to your own device tag. The default tailnet is allow-all; if another device/person ever joins, the authless dashboard would be reachable without it.
3. **Set `COMMENTS_SERVER_CORS_WHITELIST`** to the exact MagicDNS origin (e.g. `https://host.tailXXXX.ts.net`) — never `*`. Without it the HTML loads but every AJAX call silently 403s ("why doesn't it work on my phone").
4. **Move secrets to a 0600 file outside repo_root** (so no `send_file` route can ever resolve to a credential) and **rotate** them.
5. **Ticker allowlist** — add `re.fullmatch(r'[A-Z0-9.\-]{1,10}', ticker.upper())` before the path join on `/dcf/<ticker>` and `/reports/<ticker>` (unvalidated attacker-controlled filesystem input; narrow today, close it on a networked box).
6. **Reduce `/healthz`** to `{'status':'ok'}` — it currently leaks the absolute `repo_root` path.
7. **~6-line security-headers `after_request`** — `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (report URLs contain tickers = positions), + HSTS under TLS.
8. **Confirm the poller is chat_id-allowlisted** to you.

Items 5–7 are cheap defense-in-depth even behind the mesh (a compromised tailnet device shouldn't trivially pillage the box).

---

## 6. Phased Migration Sequence

Each phase is independently verifiable. **The laptop stays fully live as fallback** until Phase 6 cutover — it keeps the repo, DB, and can run `start_comments_server.bat` at any time.

**Phase 0 — Fix two active bugs first (on the laptop, independent of hosting).**
- The **poller clean-exit bug** is best fixed at the supervisor (Phase 3), but the **`run_morning_pipeline.py` `UnicodeEncodeError`** is broken *right now*: it writes UTF-8 child output to a cp1252 stdout and crashes at stage 0c, silently skipping triggers/feed/validate (confirmed in the 2026-07-01 cron log). Add `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` in `main()`.
- *Verify:* run the morning pipeline manually end-to-end; all stages complete.

**Phase 1 — Phone access to an always-on instance (the smallest thing that works; hours, $0, no hardware).**
- Install **Tailscale** on the **current laptop** + phone. Wrap `comments_server` and `capture_poller` as **NSSM services** (auto-start at boot, restart on any exit). Disable lid-close sleep, keep it plugged in.
- Set `COMMENTS_SERVER_CORS_WHITELIST` to the laptop's MagicDNS origin.
- *Verify:* **close the lid**, open the dashboard on your phone over Tailscale, confirm the chat/ask SSE panel streams. This validates the entire Tailscale + phone + supervised-service workflow **before you spend a dollar or port anything.** (This is a bridge, not the destination — one unsupervised box.)

**Phase 2 — Provision the always-on host + add `waitress` + write the two units (verify locally).**
- Buy the N100 (or provision the CX32). Install Ubuntu 24.04, Tailscale, `apt install ffmpeg`, Python + deps + `waitress`.
- Write `earnings-server.service` and `earnings-poller.service`; verify them **on the new box against a throwaway/empty DB** first.
- *Verify:* both services start, restart on kill, and the server answers on the tailnet.

**Phase 3 — Migrate the data consistently.**
- On the laptop: `python cron/backup_db.py` (from **MAIN**, not the stub) → scp the `.gz` → `python cron/restore_db.py --latest --to /opt/earnings-summary/data/portfolio.db` → `PRAGMA integrity_check`.
- `tar czf` the fmp cache and `ir_documents/`, transfer, extract. Move secrets to `/etc/earnings-summary/env` (0600).
- *Verify:* row counts + schema version match; dashboard renders real reports on the new box.

**Phase 4 — Backups + DR on the new host.**
- Set `ES_DB_BACKUP_DIR` to a local path; add restic → Backblaze B2. Run one **real restore drill** (`restore_db.py --latest --to /tmp`) **before trusting the host**.
- *Verify:* a `.gz` lands in B2; the restore passes integrity check.

**Phase 5 — Port the cron fleet (the bulky, low-risk-if-last work).**
- Convert the ~30 jobs to systemd timers (`Persistent=true`). **Keep the `--max-cost-usd 10` and `CLAUDE_WEB_MAX_BUDGET_USD $2` caps intact.** Fire each timer once manually.
- *Verify:* run the full daily chain (03:00→06:45) **unattended for one cycle** with the laptop still live; confirm `ingestion_runs` rows land and the feed rebuilds.

**Phase 6 — Cutover.**
- Rotate all secrets. Decommission the laptop's Task Scheduler tasks (and the Phase-1 NSSM services). Retire/rewrite `verify_cron_registration.py` for `systemctl list-timers` (or drop it — `cron_health_panel` reads `ingestion_runs`, which is scheduler-agnostic).
- *Verify:* one full day on the new host with the laptop powered down.

---

## 7. Total Monthly Cost & Open Decisions

**Recommendation's recurring cost:**

| Line | N100 mini-PC | Hetzner CX32 |
|---|---|---|
| Host | ~$2–3 (power); ~$4–5 amortized hardware over 3 yr | ~$8 |
| Tailscale | $0 | $0 |
| Backblaze B2 | ~$0.10–1.00 | ~$0.10–1.00 |
| **Total (hosting)** | **~$3–8/mo effective** | **~$9/mo** |

**The number that actually matters — LLM spend (unchanged by hosting):**
The metered LLM bill is **~$300–650/mo** (verified from the `llm_calls` ledger: July MTD $58.55, June $655.87 backfill-heavy, May $35.87; steady-state ~$14/day median). **Hosting is a rounding error — 30–70× smaller.** Do not over-optimize the ~$5/mo host spread. An always-on box fires crons unattended, so the existing per-call caps and budget gates **must** survive the port or a runaway inverts the whole cost story.

**Open decisions for you:**

1. **Access model — confirm Tailscale-private.** Is "open it on a borrowed non-Tailscale browser" an actual hard requirement, or does "my own phone" cover it? (If the former, we take the heavier Cloudflare+OIDC path — but it breaks chat SSE on the free tier.)
2. **Host — N100 mini-PC (data in-house, ~$150 once) vs Hetzner CX32 (no hardware, ~$8/mo).** Decide on data-sovereignty vs home-uptime tolerance, **not** the ~$5/mo delta.
3. **OS/effort — full Linux port now (3–5 days, fixes supervision at the root) vs stay-Windows + NSSM (~2 hours, keeps the fragile ops shell).** How much one-time effort is worth it?
4. **Backup — local-only (single point of failure) vs add restic→B2 (~$0.10/mo, survives host loss).** Recommend at least the local repoint before cutover, B2 shortly after.
5. **Secret rotation — confirm you'll rotate the Telegram token + FMP + Gemini keys after migration, and when.**
6. **Confirm the Telegram poller is chat_id-allowlisted to you** — the one ingress the mesh does not protect.

**Bottom line:** one small always-on box, Linux, Tailscale-private, no login code, ~$3–9/mo — and the migration's biggest payoff isn't cost, it's that `systemd Restart=always` permanently kills the poller-death and lid-close problems that started this.