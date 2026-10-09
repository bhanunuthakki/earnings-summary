"""Module-level helpers, constants, and the redacting Flask boundary.

The route modules and the app factory share one definition of these helpers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from flask import Flask, has_request_context, request

from ask.exchange_store import SessionContextV1
from dcf import persist as dcf_persist
from dcf import redesign as dcf_redesign
from log_redact import redact
from runtime.secrets import secret_read_path
from server_runtime.access import (
    is_allowed_origin,
    private_mobile_origin,
    tailscale_access_enabled,
)
from user_state.notes import AnalystNoteRow

if TYPE_CHECKING:
    from discovery.store import CandidateRow
    from user_state.saved_views import SavedViewRow


SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"

# Repo-wide maintenance chores exposed on the dashboard, each dispatched as a
# single-flight job running an existing CLI under execution/. (Onboarding a
# specific ticker is handled separately — it needs a ticker argument.)
MAINTENANCE_ACTIONS: dict[str, list[str]] = {
    "seed_kpis": ["seed_kpi_definitions.py", "--all"],
    "process_inbox": ["register_dropped_documents.py", "--all"],
    "sweep_history": ["sweep_output_history.py"],
    "onboard_pending": ["onboard_pending_tickers.py"],
}

MAX_REQUEST_BYTES = 262_144
MAX_USER_INPUT_CHARS = 8_000
STREAM_QUEUE_MAXSIZE = 64
MAX_ADMITTED_CHATS = 8
CORRELATION_ID_RX = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")

# The work-os hydration GETs join the panel response cache: same 30s TTL,
# same single-flight, same mutation-clear. Server-side only — the client keeps
# its strict no-store contract on every one of these routes. Evaluation
# Dialogues (M2 F9) joins them: it re-hashed and re-validated every session
# context per request, work the mutation registry already schedules for the
# session/note write families.
_WORK_OS_CACHE_ROUTE_PATHS = frozenset(
    {
        "/api/work-os/portfolio",
        "/api/work-os/evaluation",
        "/api/work-os/briefs",
        "/api/work-os/evaluation-dialogues",
    }
)


def is_work_os_cached_route(path: str) -> bool:
    """Match exactly the cached work-os surfaces (never the brief-body subroute)."""
    if path in _WORK_OS_CACHE_ROUTE_PATHS:
        return True
    return path.startswith("/api/work-os/companies/") and path.endswith("/desk")


# The cached surfaces whose response bodies render memoized cockpit rows
# (M2 F5). A mutation whose registry family set contains ANY of these prefixes
# must drop the data memo as well as the response cache, so the memo's
# invalidation set is never narrower than the response cache's for the routes
# that share the data (validation contract VAL-SERVE-019). Unknown/CLEAR_ALL
# mutations fail safe to a full memo clear, mirroring the response cache.
COCKPIT_DATA_MEMO_FAMILIES = frozenset(
    {
        "/api/panel/overview",
        "/api/work-os/portfolio",
        "/api/work-os/evaluation",
    }
)


_BROWSER_USER_AGENT_RX = re.compile(r"(?:mozilla|chrome|chromium|safari|firefox|edg)/", re.I)
README_RUN_ID_RX = re.compile(r"[0-9a-f]{32}\Z")
LOGGER = logging.getLogger(__name__)


def drain_durable_events(
    events: Iterator[dict[str, object]],
    chunks: queue.Queue[dict[str, object] | None],
    stop: threading.Event,
) -> None:
    """Consume persistence-owning streams to completion after a disconnect."""

    def put_if_connected(item: dict[str, object] | None) -> None:
        while not stop.is_set():
            try:
                chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    try:
        for item in events:
            if stop.is_set():
                continue
            put_if_connected(item)
    except Exception as exc:
        LOGGER.error("durable Ask stream failed: %s", redact(exc))
        put_if_connected({"type": "error", "error": "ask failed; retry the request"})
    finally:
        put_if_connected(None)


def cors_allow_origin(origin: str, *, repo_root: Path = PROJECT_ROOT) -> str | None:
    """Return the ``Access-Control-Allow-Origin`` value to echo for ``origin``, or None.

    Allows authenticated file:// reports (Origin ``"null"``), same-port
    loopback aliases, and exact configured private origins. The request guard
    separately requires the capability for opaque-origin access. An explicit
    comma-separated
    ``COMMENTS_SERVER_CORS_WHITELIST`` of allowed origins is honored.
    """
    whitelist = {
        o.strip()
        for o in os.environ.get("COMMENTS_SERVER_CORS_WHITELIST", "").split(",")
        if o.strip()
    }
    configured_private_origin = private_mobile_origin(
        config_path=secret_read_path("private_mobile_base_url", repo_root=repo_root)
    )
    if configured_private_origin:
        whitelist.add(configured_private_origin)
    return is_allowed_origin(
        origin,
        allow_tailscale=tailscale_access_enabled(),
        whitelist=frozenset(whitelist),
        server_origin=request.host_url.rstrip("/")
        if has_request_context()
        else "http://localhost:7421",
    )


def referer_back_path(referer: str) -> str | None:
    """The relative ``path?query`` an ``/approve`` click bounces back to,
    derived from its Referer — or None when the Referer is absent,
    unparseable, or cross-site (judged by the same loopback/whitelist rule
    as CORS, via ``_cors_allow_origin``). Scheme and host never survive into
    the redirect target (and ``//host``-style paths are rejected), so a
    crafted Referer can't turn ``/approve`` into an open redirect."""
    if not referer:
        return None
    try:
        parsed = urllib.parse.urlparse(referer)
    except ValueError:
        return None
    if (parsed.scheme or parsed.netloc) and cors_allow_origin(
        f"{parsed.scheme}://{parsed.netloc}"
    ) is None:
        return None
    path = parsed.path or "/"
    if not path.startswith("/") or path[1:2] in ("/", "\\"):
        return None
    return path + (f"?{parsed.query}" if parsed.query else "")


def is_browser_user_agent(user_agent: str) -> bool:
    """Identify browser requests only for the no-fetch-metadata CSRF fallback.

    Browsers normally send ``Origin`` and/or ``Sec-Fetch-Site`` on mutations.
    Privacy-hardened clients can omit both, so a browser-shaped request with no
    trustworthy metadata must present the existing report capability. Local
    non-browser callers (curl, the Python CLI, Flask's test client) remain
    usable without turning a browser's ambient loopback access into a bypass.
    """

    return _BROWSER_USER_AGENT_RX.search(user_agent) is not None


def linked_gsheet(repo_root: Path, ticker: str) -> tuple[str | None, str | None]:
    """The ``(sheet_id, edit_url)`` of the Google Sheet linked to a ticker's DCF,
    or ``(None, None)`` when no ``dcf_defaults.gsheet_id`` is set in the holdings
    JSON. Shared by the ``/dcf/<T>`` redirect and the ``/api/dcf-sheet/<T>``
    endpoint so the two never diverge on how a Sheet link is resolved."""
    from dcf.availability import resolve_dcf_route_artifact

    artifact = resolve_dcf_route_artifact(repo_root, ticker)
    if artifact is None or artifact.kind != "sheet":
        return None, None
    return artifact.sheet_id, artifact.target


def dcf_recompute_payload(inp: dcf_redesign.RedesignInputs) -> dict[str, object]:
    """Run the pure DCF engine over one assumption set → the JSON the in-app
    valuation card consumes.

    The whole in-app modify→recompute loop in one stateless call (resolves the
    DCF round-trip gap): base fair value, the Bull/Base/Bear scenario triplet,
    the live over/under (decimal, the 0076 convention), and the WACC × exit-
    multiple sensitivity grid (computed today but trapped in xlsx cells). No
    xlsx, no persistence — a save / Push-to-Sheets commit is a separate action.

    Raises :class:`dcf_redesign.RedesignError` only for a degenerate BASE (e.g. a
    perpetuity terminal with WACC ≤ g); a degenerate Bull/Bear degrades to
    ``None`` inside ``scenario_values`` rather than raising.
    """
    sv = dcf_redesign.scenario_values(inp)
    grid = dcf_redesign.sensitivity_grid(inp)
    return {
        "fair_value_per_share_usd": sv.base,
        "scenarios": {"base": sv.base, "bull": sv.bull, "bear": sv.bear},
        "over_under_pct": dcf_persist.derive_over_under(inp.current_price, sv.base),
        "wacc": inp.wacc,
        "terminal_method": inp.terminal_method,
        "terminal_basis": inp.terminal_basis,
        "exit_multiple": inp.exit_multiple,
        "current_price": inp.current_price,
        "sensitivity": {
            "wacc_axis": list(grid.wacc_axis),
            "multiple_axis": list(grid.multiple_axis),
            "values": [list(r) for r in grid.values],
            "base_wacc": grid.base_wacc,
            "base_multiple": grid.base_multiple,
            "basis": grid.basis,
            "current_price": grid.current_price,
        },
    }


def note_to_json(note: object) -> dict[str, object]:
    """AnalystNoteRow → JSON-safe dict for the /api/notes responses (P4.5)."""
    if not isinstance(note, AnalystNoteRow):
        raise TypeError("note must be an AnalystNoteRow")
    payload = asdict(note)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in payload.items()}


def opt_int(raw: object) -> int | None:
    """A JSON field as int, or None when absent/empty/non-numeric — the
    note-link routes' tolerant id decode (S15)."""
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def view_to_json(view: SavedViewRow) -> dict[str, object]:
    """SavedViewRow → JSON-safe dict for the /api/views responses (P5.1)."""
    payload = asdict(view)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in payload.items()}


def candidate_to_json(cand: CandidateRow) -> dict[str, object]:
    """CandidateRow → JSON-safe dict for the /api/discovery responses (P5.4)."""
    payload = asdict(cand)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in payload.items()}


# Lifecycle moves the OWNER may make from the queue UI / chat. ``building``
# and ``built`` are written only by the build pathway (discovery_build.py) —
# the queue can't hand-wave a name into "built".
DISCOVERY_OWNER_STATUSES: frozenset[str] = frozenset({"new", "queued", "dismissed"})


def payload_text(value: object) -> str | None:
    """A trimmed non-empty string from a JSON payload field, else None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def canonical_session_context(repo_root: Path, context: SessionContextV1) -> SessionContextV1:
    """Bind company context to the exact canonical thesis bytes, when present."""

    ticker = context.company_ticker
    canonical_ref: str | None = None
    canonical_version: str | None = None
    if ticker is not None:
        relative = Path("micro_thesis") / "holdings" / f"{ticker}.json"
        thesis_path = repo_root / relative
        if thesis_path.is_file():
            canonical_ref = relative.as_posix()
            canonical_version = hashlib.sha256(thesis_path.read_bytes()).hexdigest()
    if context.thesis_ref not in {None, canonical_ref}:
        raise ValueError("session_context thesis_ref conflicts with the canonical thesis")
    if context.thesis_version not in {None, canonical_version}:
        raise ValueError("session_context thesis_version conflicts with the canonical thesis")
    return context.model_copy(
        update={
            "thesis_ref": canonical_ref,
            "thesis_version": canonical_version,
        }
    )


def _parse_bbox_param(raw: str | None) -> tuple[float, float, float, float] | None:
    """``?bbox=x0,y0,x1,y1`` (PDF page coords) → tuple, or None on any
    malformed input — the highlight is enrichment, never a 400."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(p) for p in parts)
    except ValueError:
        return None
    return (x0, y0, x1, y1)


def record_dismiss_pass(
    *,
    ticker: str,
    reason: str,
    revisit_text: str | None,
    source_dismissal_id: int | None,
    db_path: Path,
) -> dict[str, object] | None:
    """Record a pass/avoid decision (L11), JSON-shaped for the response. None
    when the ledger is unavailable — the dismiss/queue move still succeeded, the
    optional decision capture just didn't land."""
    from pass_decisions import (
        LENS_DISCOVERY_DISMISSAL,
        LENS_MANUAL_PASS,
        record_pass_decision,
    )

    result = record_pass_decision(
        ticker=ticker,
        reason=reason,
        revisit_text=revisit_text,
        source_dismissal_id=source_dismissal_id,
        source_lens=(
            LENS_DISCOVERY_DISMISSAL if source_dismissal_id is not None else LENS_MANUAL_PASS
        ),
        db_path=db_path,
    )
    if result is None:
        return None
    return {"decision_id": result.decision_id, "created": result.created, "ticker": ticker}


# The deterministic /discovery chat commands moved to ask.commands (the
# unified ask engine intercepts them from BOTH chat surfaces); the REST
# build routes share its buildable-status set via discovery.store.


class RedactingFlask(Flask):
    """Flask boundary that never writes an exception traceback to logs."""

    def log_exception(
        self,
        exc_info: tuple[type, BaseException, TracebackType] | tuple[None, None, None],
    ) -> None:
        exc = exc_info[1]
        if exc is None:
            return
        self.logger.error(
            "unhandled request exception: %s",
            redact(f"{type(exc).__name__}: {exc}")[:500],
        )
