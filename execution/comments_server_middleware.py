"""Request lifecycle hooks for the comments-server app.

Owns the before/after/error/teardown hooks and the panel/work-os response cache
integration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import urllib.parse
from typing import TYPE_CHECKING, cast

from comments_server_cockpit_memo import CockpitDataReservation
from comments_server_panel_cache import (
    PanelCacheBusy,
    PanelCacheEntry,
    PanelCacheHit,
    PanelCacheReservation,
    resolve_mutation_families,
)
from comments_server_shared import (
    COCKPIT_DATA_MEMO_FAMILIES,
    CORRELATION_ID_RX,
    cors_allow_origin,
    is_browser_user_agent,
    is_work_os_cached_route,
)
from flask import Flask, Response, g, request

import comments
from log_redact import redact
from logging_config import get_correlation_id, new_correlation_id, set_correlation_id
from runtime.secrets import secret_read_path
from server_runtime.access import (
    REPORT_CAPABILITY_HEADER,
    is_allowed_client_address,
    is_allowed_request_host,
    private_mobile_origin,
    tailscale_access_enabled,
)

if TYPE_CHECKING:
    from comments_server_context import ServerContext


def install_middleware(app: Flask, ctx: ServerContext) -> None:
    """Register the request lifecycle hooks on ``app``."""
    client_error = ctx.client_error
    chat_slots = ctx.chat_slots
    cockpit_memo = ctx.cockpit_memo
    panel_cache = ctx.panel_cache
    repo_root = ctx.repo_root
    report_capability = ctx.report_capability
    server_origin = ctx.server_origin

    @app.before_request
    def enforce_network_boundary():
        configured_origin = private_mobile_origin(
            config_path=secret_read_path("private_mobile_base_url", repo_root=repo_root)
        )
        trusted_origins = tuple(origin for origin in (configured_origin, server_origin) if origin)
        if not is_allowed_request_host(request.host, trusted_origins=trusted_origins):
            return ({"error": "request host is not an application host"}, 403)
        remote_address = request.remote_addr or ""
        if not is_allowed_client_address(
            remote_address,
            allow_tailscale=tailscale_access_enabled(),
        ):
            return ({"error": "client address is outside the allowed network"}, 403)
        return None

    @app.before_request
    def bind_correlation_id() -> None:
        # Fresh correlation id per request so all log lines for one operation
        # stitch together (sre-4). Honor an upstream X-Correlation-ID if present.
        incoming = request.headers.get("X-Correlation-ID", "").strip()
        if CORRELATION_ID_RX.fullmatch(incoming):
            set_correlation_id(incoming)
        else:
            new_correlation_id()

    def invalidate_mutation_cache() -> None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        # A mutation can affect several cached surfaces. Instead of
        # evicting the whole 256-entry panel + work-os response cache, resolve
        # the exact families that route's writes can staleness from the
        # mutation-route registry (W6) and invalidate only those prefixes.
        # Panel timing telemetry (/api/metrics/panel) and the operations
        # attention routes are registered as declared no-ops (the latter
        # invalidates at its own precise success moment inside the route). The
        # registry is total over the non-GET route table — enforced by a test
        # that walks app.url_map — and an unknown route resolves to None,
        # which FAILS SAFE to a full clear() so new mutation routes can never
        # serve a pre-mutation fragment.
        route_rule = cast(object, request.url_rule)
        families = resolve_mutation_families(getattr(route_rule, "rule", None))
        if families is None:
            panel_cache.clear()
            cockpit_memo.clear()
        else:
            for prefix in families:
                panel_cache.invalidate_prefix(prefix)
            # The cockpit-data memo backs every surface in
            # _COCKPIT_DATA_MEMO_FAMILIES, so any mutation the registry maps
            # to one of them drops the memo too — its invalidation set is
            # never narrower than the response cache's for that data
            # (VAL-SERVE-019). Unmapped families (e.g. alert dismissal →
            # journal/diet only) keep the memo's 30s bounded staleness,
            # exactly the bound the response cache already had.
            if any(prefix in COCKPIT_DATA_MEMO_FAMILIES for prefix in families):
                cockpit_memo.clear()

    @app.errorhandler(comments.CommentStoreReadError)
    def unavailable_comment_store(_error: comments.CommentStoreReadError):
        return client_error(
            "comment store unavailable; original preserved; repair before retrying", 503
        )

    @app.before_request
    def start_request_timer() -> None:
        g.request_started_ns = time.perf_counter_ns()
        invalidate_mutation_cache()

    @app.after_request
    def invalidate_completed_mutation_cache(response: Response) -> Response:
        # A concurrent GET may read old state after the initial invalidation.
        # Evict its cached result or active reservation after writes finish,
        # including error responses that may follow a partial mutation.
        invalidate_mutation_cache()
        return response

    @app.errorhandler(413)
    def request_too_large(_error: object):
        return client_error("request body is too large", 413)

    @app.errorhandler(500)
    def internal_server_error(error: object):
        if getattr(error, "original_exception", None) is None:
            app.logger.error(
                "unhandled request failure: %s",
                redact(f"{type(error).__name__}: {error}")[:500],
            )
        return client_error("request failed; retry the request", 500)

    @app.teardown_request
    def close_request_db(exception: BaseException | None = None) -> None:
        if g.pop("chat_admission_reserved", False):
            chat_slots.release()
        for key in ("request_read_db", "request_operations_read_db"):
            db_conn = g.pop(key, None)
            if db_conn is not None:
                with contextlib.suppress(Exception):
                    db_conn.close()
        reservation = g.pop("panel_cache_reservation", None)
        if isinstance(reservation, PanelCacheReservation):
            panel_cache.abandon(reservation)
        memo_reservation = g.pop("cockpit_memo_reservation", None)
        if isinstance(memo_reservation, CockpitDataReservation):
            cockpit_memo.abandon(memo_reservation)

    @app.before_request
    def csrf_origin_guard():
        # CSRF defense-in-depth for the unauthenticated localhost control plane.
        # A site the operator is visiting can drive a cross-origin state-changing
        # request at this server; reject any unsafe-method request whose browser
        # Origin is cross-site (judged by the same loopback / "null" / whitelist
        # rule as CORS, via _cors_allow_origin). Opaque-origin sensitive reads
        # also need the capability; health checks and OPTIONS remain public. An absent Origin remains allowed for loopback
        # non-browser CLI callers, but a browser with no Origin, Referer, or
        # Fetch Metadata must prove possession of the static-report capability.
        # Remote mutations must come from a same-origin browser surface. This
        # complements the
        # CORS-withholding in add_cors_headers, which only stops requests the
        # browser bothers to preflight — the Origin check also covers a simple
        # or forged cross-site request that skips preflight.
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS":
            return None
        if request.method in ("GET", "HEAD"):
            if (
                origin == "null"
                and request.path != "/healthz"
                and not report_capability.matches(request.headers.get(REPORT_CAPABILITY_HEADER, ""))
            ):
                return ({"error": "static report capability required"}, 403)
            return None
        remote_address = request.remote_addr or ""
        capability_matches = report_capability.matches(
            request.headers.get(REPORT_CAPABILITY_HEADER, "")
        )
        if origin == "null" and not capability_matches:
            return ({"error": "static report capability required"}, 403)
        if (
            not origin
            and tailscale_access_enabled()
            and remote_address
            not in (
                "127.0.0.1",
                "::1",
            )
        ):
            return ({"error": "Origin required for remote state-changing request"}, 403)
        if origin and cors_allow_origin(origin, repo_root=repo_root) is None:
            return ({"error": "cross-origin state-changing request refused"}, 403)
        if not origin:
            fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
            if fetch_site == "cross-site":
                return ({"error": "cross-site state-changing request refused"}, 403)
            referer = request.headers.get("Referer", "").strip()
            if referer:
                try:
                    parsed = urllib.parse.urlparse(referer)
                    referer_origin = f"{parsed.scheme}://{parsed.netloc}"
                except ValueError:
                    referer_origin = ""
                if (
                    not referer_origin
                    or cors_allow_origin(referer_origin, repo_root=repo_root) is None
                ):
                    return ({"error": "cross-site state-changing request refused"}, 403)
            elif fetch_site != "same-origin" and not capability_matches:
                if is_browser_user_agent(request.headers.get("User-Agent", "")):
                    return ({"error": "state-changing request capability required"}, 403)
        return None

    @app.before_request
    def reserve_chat_admission():
        if request.method != "POST" or request.path != "/api/ask/stream":
            return None
        if not chat_slots.acquire(blocking=False):
            return ({"error": "chat capacity reached; retry shortly"}, 429, {"Retry-After": "5"})
        # Admission precedes session creation and durable exchange acceptance.
        # The worker takes ownership only after successful submission.
        g.chat_admission_reserved = True
        return None

    @app.before_request
    def serve_fresh_panel_cache() -> Response | None:
        if request.method != "GET":
            return None
        work_os_cached = is_work_os_cached_route(request.path)
        if not work_os_cached and not request.path.startswith("/api/panel/"):
            return None
        if request.path == "/api/panel/cron_health" and request.args.get("fragment") == "live":
            g.panel_cache_bypass = True
            return None
        cache_key = request.full_path.removesuffix("?")
        lookup = panel_cache.get_or_reserve(cache_key)
        if isinstance(lookup, PanelCacheBusy):
            g.panel_cache_bypass = True
            g.panel_cache_busy = True
            response = Response(
                json.dumps({"error": "This view is still loading. Retry shortly."}),
                status=503,
                mimetype="application/json",
            )
            response.headers["Retry-After"] = str(lookup.retry_after_seconds)
            response.headers["X-Panel-Cache"] = "busy"
            return response
        if isinstance(lookup, PanelCacheReservation):
            g.panel_cache_reservation = lookup
            return None
        assert isinstance(lookup, PanelCacheHit)
        body = lookup.entry.body
        content_type = lookup.entry.content_type
        g.panel_cache_hit = True
        if work_os_cached:
            # Work-os hydration is server-side only: the client contract stays
            # Cache-Control no-store (the browser never caches these payloads),
            # so the hit carries no ETag and no 304 — just the observation
            # header and the untouched route semantics.
            response = Response(body, content_type=content_type)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Panel-Cache"] = "hit"
            return response
        etag = lookup.entry.etag
        if request.if_none_match.contains(etag.strip('"')):
            response = Response(status=304)
        else:
            response = Response(body, content_type=content_type)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Panel-Cache"] = "hit"
        return response

    @app.after_request
    def add_cors_headers(response: Response) -> Response:
        # The workspace report HTML opens via file://, so its browser Origin is
        # the literal string "null"; pages served by this server carry a
        # loopback Origin. Echo back ONLY those — never "*". A wildcard let any
        # site the user happened to be visiting drive state-changing POSTs
        # (refresh/onboard jobs, comment writes, the chat-apply file write)
        # against this unauthenticated localhost server: those routes require a
        # JSON content-type, which forces a CORS preflight that "*" answered.
        # Withholding the header makes the preflight fail, so the cross-site
        # request never fires. (See _cors_allow_origin for the whitelist path.)
        allowed = cors_allow_origin(request.headers.get("Origin", ""), repo_root=repo_root)
        if allowed is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            f"Content-Type, {REPORT_CAPABILITY_HEADER}, X-Portfolio-Write-Intent"
        )
        # Security headers — the dashboard is network-reachable over Tailscale.
        # SAMEORIGIN (not DENY) because the command center embeds /reports/<T> in
        # a same-origin iframe. no-referrer so ticker-bearing report URLs (which
        # reveal positions) never leak in a Referer to any external destination.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers["X-Correlation-ID"] = get_correlation_id()
        return response

    @app.after_request
    def add_panel_etag(response: Response) -> Response:
        # Cheap revalidation for the shell's panel fragments (S14): every
        # 200-OK GET under /api/panel/ carries a content ETag, and a matching
        # If-None-Match comes back 304 with an empty body — the client's
        # stale-while-revalidate refresh costs the panel BUILD, not the panel
        # TRANSFER, and an unchanged panel never re-renders. `no-cache` means
        # "store but always revalidate", so the browser's own HTTP cache gives
        # the drawer/notes/peek fetches the same cheap 304 path with zero
        # client changes. (Registered after add_cors_headers — Flask runs
        # after_request hooks in reverse order, so the 304 conversion happens
        # first and the CORS headers still land on the 304.)
        if getattr(g, "panel_cache_bypass", False):
            response.headers["Cache-Control"] = "no-store"
            return response
        if (
            request.method == "GET"
            and response.status_code == 200
            and not response.direct_passthrough
            and is_work_os_cached_route(request.path)
        ):
            # Work-os hydration joins the same 30s single-flight cache. The
            # client contract is unchanged — no-store, no ETag, no 304 — only
            # the X-Panel-Cache observation header is added. Degraded
            # 200-with-degraded tracker payloads are cached under the same TTL
            # so a down tracker keeps serving its honest degraded state instead
            # of re-probing per request. External cron writes are not
            # invalidation events (parity with panel fragments today); HTTP
            # mutations evict only the cache families their route's registry
            # entry declares at mutation start and completion.
            if not getattr(g, "panel_cache_hit", False):
                reservation = g.pop("panel_cache_reservation", None)
                if isinstance(reservation, PanelCacheReservation):
                    body = response.get_data()
                    panel_cache.store(
                        reservation,
                        PanelCacheEntry(
                            body=body,
                            content_type=response.content_type or "application/octet-stream",
                            # Internal freshness validator only; work-os
                            # responses never emit it (no-store).
                            etag=hashlib.sha256(body).hexdigest(),
                            # Memo-fed bodies inherit the memo's data clock so
                            # the entry cannot serve cockpit data past the
                            # memo's own expiry (M2 F5 staleness bound).
                            expires_at=g.pop("cockpit_memo_expires_at", None),
                        ),
                    )
                response.headers["X-Panel-Cache"] = "miss"
            return response
        if (
            request.method == "GET"
            and request.path.startswith("/api/panel/")
            and response.status_code == 200
            and not response.direct_passthrough
        ):
            response.add_etag()
            response.headers["Cache-Control"] = "no-cache"
            if not getattr(g, "panel_cache_hit", False):
                reservation = g.pop("panel_cache_reservation", None)
                if isinstance(reservation, PanelCacheReservation):
                    panel_cache.store(
                        reservation,
                        PanelCacheEntry(
                            body=response.get_data(),
                            content_type=response.content_type or "application/octet-stream",
                            etag=response.headers["ETag"],
                            # Memo-fed bodies inherit the memo's data clock so
                            # the entry cannot serve cockpit data past the
                            # memo's own expiry (M2 F5 staleness bound).
                            expires_at=g.pop("cockpit_memo_expires_at", None),
                        ),
                    )
                response.headers["X-Panel-Cache"] = "miss"
            # make_conditional mutates + returns self; the cast restores the
            # Flask subclass the werkzeug stub erases.
            return cast("Response", response.make_conditional(request))
        return response

    @app.after_request
    def add_server_timing(response: Response) -> Response:
        started_ns = getattr(g, "request_started_ns", None)
        if isinstance(started_ns, int):
            duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
            if duration_ms >= 500:
                app.logger.info(
                    json.dumps(
                        {
                            "event": "slow_http_request",
                            "method": request.method,
                            "path": request.path,
                            "status": response.status_code,
                            "duration_ms": round(duration_ms, 1),
                            "panel_cache": (
                                "busy"
                                if getattr(g, "panel_cache_busy", False)
                                else "hit"
                                if getattr(g, "panel_cache_hit", False)
                                else "miss"
                            ),
                            "correlation_id": get_correlation_id(),
                        },
                        separators=(",", ":"),
                    )
                )
        return response
