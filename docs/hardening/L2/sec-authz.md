# sec-authz — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `execution/comments_server.py` (no `before_request`; only `@after_request` at :168) | No authentication on any route — no auth hook, decorator, session, token, or password check anywhere. Every route incl. state-changing ones executes for any caller reaching the port. | Mandatory `@before_request` authN gate (allowlist only `/healthz`); default-deny → 401. Don't bind off-loopback until present. |
| critical | `src/identity.py:16`; consumed at `comments_server.py:256,301,360,373` | IDOR / no server-side identity — `user_id` is attacker-controlled via `?user_id=`, fallback `DEFAULT_USER_ID="bhanu"`. Any caller reads/exports another tenant's data. | Derive `user_id` solely from the authenticated principal; delete the `request.args.get("user_id")` reads. |
| critical | `comments_server.py:729-765` (`/actions/maintenance`), `:546-596` (`/actions/refresh`), `:646-727` (dcf) | Unauthenticated state change + subprocess dispatch (`sys.executable <script>`) → vertical privilege escalation; `force_budget_bypass` lets an anon caller bypass cost controls. | Gate all `/actions/*` + `apply=true` behind an operator/admin role (RBAC, default-deny); log each dispatch with principal. |
| high | `comments_server.py:1016-1024` → `src/chat_session.py:355-445` | Unauthenticated arbitrary write within a broad scope (`data/`,`micro_thesis/`,`.tmp/`,`directives/`); `.resolve()` guard doesn't stop symlink escape (TOCTOU). | Require authN + per-tenant path authorization; drop `directives/`; use realpath containment + symlink check. |
| high | `comments_server.py:788-856` (`/comments`), `:954-1007` (`/chat`), `:480-514` (`/reports`,`/dcf`) | Object-level authZ missing — comments/chat/reports keyed only by ticker+date with no ownership check; any caller reads/deletes another tenant's threads. | Resolve owning tenant and compare to principal before any read/mutate; 403 on mismatch; centralize the check. |
| medium | `comments_server.py:412-462` (`/api/llm-budgets`, `/api/ticker-settings`) | Unauthenticated mutation of cost/operational controls (set caps, `bypass_budget=true`) → cost-exhaustion. | Gate behind authN + operator role; audit-log config writes. |
| medium | `comments_server.py` (mutating routes) | No authN/authZ audit trail — privileged dispatches/mutations emit no per-principal event. | Structured audit log (principal, action, target tenant, time, outcome); never log credentials. |
| info/adv | CORS `:94-118,168-185`; secrets `fetch_etf_data.py`, `src/integrations/gsheets.py` | Credited: CORS echo (never `*`) is sound CSRF mitigation (not authN); secrets handling good — FMP key redacted via `log_redact`+`from None`, Google creds in gitignored `data/secrets/`, no JWT/`alg:none` issues (no tokens exist). | Keep CORS but don't treat as access control; adopt a secrets manager + rotation before L3. |

## Out of scope
Row-level cross-tenant enforcement → `sec-tenant-isolation` (findings #2/#5 are its prerequisite). Generic appsec / rate-limiting / gitignore verification → `sec-appsec`. Legal access rights → `legal-compliance`.
