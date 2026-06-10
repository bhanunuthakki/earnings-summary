# sec-appsec — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `execution/comments_server.py` (all routes) | No authentication/authorization on any route, including state-changing `/actions/refresh`, `/actions/maintenance`, `/actions/dcf-import`, `/chat/<ticker>/apply`, `/comments`. Defense is only loopback-bind + CORS — both void once multi-tenant. | Add a mandatory authN gate + per-request identity; gate all mutations; don't bind off-loopback until done. |
| critical | `execution/comments_server.py:256,301,360,373`; `src/identity.py:16` | `user_id` read from unauthenticated `?user_id=` query param, falling back to hardcoded `DEFAULT_USER_ID="bhanu"` → IDOR over financial PII. | Derive `user_id` from the authenticated principal; never from client input. |
| high | `requirements.txt:1-34` | Zero version pinning, no lockfile; `pydantic` listed twice. Non-reproducible builds, wide supply-chain exposure. | Pin all runtime deps + commit a lockfile (pip-compile/uv lock); dedupe pydantic. |
| high | `.github/workflows/ci.yml`, `.pre-commit-config.yaml` | No SAST (bandit/semgrep), no dependency/CVE scan (pip-audit/Dependabot), no secret scanning (gitleaks/detect-secrets). | Add pip-audit + bandit/semgrep CI jobs and a detect-secrets/gitleaks pre-commit hook. |
| medium | `src/ir_pipeline/transcript.py:186`; `src/ir_pipeline/discover/generic.py:290` | SSRF guard `ensure_safe_public_url()` exists and is used elsewhere but not at these two fetch sites. | Route both through `ensure_safe_public_url()` before the request. |
| medium | `execution/fetch_etf_data.py:233-239` (+ review sibling FMP fetchers) | FMP key in URL query param; raw `requests` exception (which embeds `?apikey=`) interpolated into a raised `RuntimeError` without the redactor → key leak on network error. | Route through `redact()` like the canonical fetcher; prefer moving the key to a header. **(Flagged as a standalone task.)** |
| medium/adv | `src/db.py:65-75` (SQLite) | No encryption at rest; acceptable at L1, but at L2 the same file holds multi-tenant financial PII in plaintext. | Encrypt at rest (SQLCipher / volume encryption) + document data classification for L2. |
| low/adv | `execution/comments_server.py:1055` et al. | Raw exception text (`f"...{e}"`) returned to client; info-disclosure risk for untrusted callers at L2. | Return generic errors to clients; log detailed (redacted) errors server-side only. |

## Credited / OK
No hardcoded secrets (entropy + provider-prefix scans clean; one `AIza` hit was prose, dismissed without printing). SQL parameterized throughout; the only f-string SQL is DDL on internal constants (not injectable). `.env`/`credentials.json`/`token.json`/`data/` gitignored. Flask defaults to `127.0.0.1`, `debug=False`, no wildcard CORS. `send_file` ticker routes constrained by Flask `<ticker>` converter.

## Out of scope
AuthN/AuthZ depth → `sec-authz`. Cross-tenant access → `sec-tenant-isolation`. Prompt-injection → `sec-llm`. GDPR/PCI/SOC2 → `legal-compliance`.
