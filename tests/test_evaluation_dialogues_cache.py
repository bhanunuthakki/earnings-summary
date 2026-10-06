"""M2 F9: the Evaluation Dialogues hydration joins the work-os response cache.

``/api/work-os/evaluation-dialogues`` used to re-open a read connection, fetch
every portfolio session context and sha256-hash + Pydantic-parse each one on
every request. It now shares the 30s single-flight response cache its sibling
work-os hydrations already use, so a second identical GET is served without any
of that work — while a request that actually rebuilds still reports a mutated
(unreadable) or missing context exactly as before.

Pins the behavior the validation suite asserts (VAL-SERVE-005/017):

* the first GET is a ``miss`` and a second identical GET inside the window is a
  ``hit`` with a byte-identical body, still ``Cache-Control: no-store`` and
  still without an ETag;
* the hit re-opens no read connection and re-runs no context sha256 validation
  (spies on the module's ``connect_sqlite`` and ``sha256`` both stay flat);
* the served body equals the direct loader projection, and an unreadable or
  missing context is still reported (``relevance_partial``, indeterminate
  ``has_dialogue`` matching) on both the miss and the cached hit;
* the mutation registry refreshes the dialogues key for the session/note/
  discovery writes that feed it, and the fail-safe clear still evicts it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comments_server
from comments_server_panel_cache import resolve_mutation_families

from pipeline import evaluation_dialogues

DIALOGUES_PATH = "/api/work-os/evaluation-dialogues"
DIALOGUES_DEFAULT_URL = f"{DIALOGUES_PATH}?limit=3&sort=relevance&filter=all"
DIALOGUES_VARIANT_URL = f"{DIALOGUES_PATH}?limit=10&sort=ticker_asc&filter=has_dialogue"
DIALOGUES_CACHE_PREFIX = "/api/work-os/evaluation-dialogues"

_DDL = """
CREATE TABLE tracked_companies (
  user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
  instrument_type TEXT, archived_at TEXT
);
CREATE TABLE discovery_candidates (
  id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, status TEXT
);
CREATE TABLE ask_sessions (
  id TEXT PRIMARY KEY, scope TEXT, title TEXT DEFAULT '',
  created_at TEXT DEFAULT '2026-08-20T00:00:00Z', updated_at TEXT, distilled_at TEXT
);
CREATE INDEX ix_ask_sessions_scope_updated ON ask_sessions (scope, updated_at DESC);
CREATE TABLE ask_session_contexts (
  session_id TEXT PRIMARY KEY, schema_version TEXT DEFAULT 'session_context.v1',
  context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL, revision INTEGER DEFAULT 0,
  created_at TEXT DEFAULT '2026-08-20T00:00:00Z', updated_at TEXT DEFAULT '2026-08-20T00:00:00Z'
);
CREATE TABLE analyst_notes (
  id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, kind TEXT DEFAULT 'observation',
  status TEXT, body TEXT DEFAULT '', anchor_type TEXT, anchor_key TEXT,
  source TEXT DEFAULT 'user', source_ref TEXT, supersedes_id INTEGER,
  resolution_note TEXT, context_json TEXT, created_at TEXT,
  updated_at TEXT DEFAULT '2026-08-20T00:00:00Z', resolved_at TEXT,
  decision_id INTEGER, position_entry_id INTEGER, link_auto_resolve INTEGER DEFAULT 0,
  fact_ref TEXT
);
"""


def _context_json(
    ticker: str,
    candidate_id: int | None = None,
    instrument: str = "stock",
) -> tuple[str, str]:
    payload = {
        "schema_version": "session_context.v1",
        "company_ticker": ticker,
        "evaluation_candidate_id": candidate_id,
        "evaluation_instrument_type": instrument,
    }
    raw = json.dumps(payload)
    return raw, sha256(raw.encode("utf-8")).hexdigest()


def _seed(tmp_path: Path, *, break_bbb: str | None = None) -> Path:
    """Build the minimal dialogues schema; ``break_bbb`` corrupts BBB's context.

    ``break_bbb='corrupt'`` writes a context whose committed sha256 no longer
    matches its JSON (unreadable); ``break_bbb='missing'`` drops the context row
    entirely. Everything else is a valid, session-linked evaluation row.
    """
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_DDL)
    conn.executescript(
        """
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'AAA', 'Alpha Corp', 'evaluation', 'equity', NULL),
          ('bhanu', 'BBB', 'Beta Corp', 'evaluation', 'equity', NULL);
        INSERT INTO discovery_candidates (id, user_id, ticker, status) VALUES
          (1, 'bhanu', 'AAA', 'built');
        INSERT INTO ask_sessions (id, scope, updated_at) VALUES
          ('session-aaa', 'portfolio', '2026-08-21T12:00:00Z'),
          ('session-bbb', 'portfolio', '2026-08-20T12:00:00Z');
        INSERT INTO analyst_notes (user_id, ticker, created_at, status) VALUES
          ('bhanu', 'AAA', '2026-08-20T10:00:00Z', 'open');
        """
    )
    aaa_json, aaa_sha = _context_json("AAA", 1, "stock")
    conn.execute(
        "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) "
        "VALUES (?, ?, ?)",
        ("session-aaa", aaa_json, aaa_sha),
    )
    bbb_json, bbb_sha = _context_json("BBB", None, "stock")
    if break_bbb == "corrupt":
        conn.execute(
            "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) "
            "VALUES (?, ?, ?)",
            ("session-bbb", bbb_json, sha256(b"{not the context}").hexdigest()),
        )
    elif break_bbb != "missing":
        conn.execute(
            "INSERT INTO ask_session_contexts (session_id, context_json, context_sha256) "
            "VALUES (?, ?, ?)",
            ("session-bbb", bbb_json, bbb_sha),
        )
    conn.commit()
    conn.close()
    return tmp_path


def _client(tmp_path: Path, *, break_bbb: str | None = None) -> FlaskClient:
    return comments_server.create_app(_seed(tmp_path, break_bbb=break_bbb)).test_client()


def _direct_projection(
    tmp_path: Path,
    *,
    limit: int,
    sort: Literal["relevance", "ticker_asc"],
    filter_state: Literal["all", "has_dialogue", "has_notes", "ready"],
) -> dict[str, Any]:
    projection = evaluation_dialogues.load_evaluation_dialogues(
        tmp_path / "data" / "portfolio.db",
        limit=limit,
        sort=sort,
        filter_state=filter_state,
    )
    return projection.model_dump(mode="json")


# ---------------------------------------------------------------------------
# The route is served by the work-os response cache (VAL-SERVE-017)
# ---------------------------------------------------------------------------


def test_first_get_is_a_miss_and_second_identical_get_is_a_hit(tmp_path: Path) -> None:
    client = _client(tmp_path)

    first = client.get(DIALOGUES_DEFAULT_URL)
    assert first.status_code == 200
    assert first.headers["X-Panel-Cache"] == "miss"
    # The rebuilt (miss) response keeps the pre-change headers byte-for-byte:
    # the route emitted no Cache-Control before this change and still does not
    # (VAL-SERVE-001 compares Cache-Control as real signal), and never an ETag.
    assert "Cache-Control" not in first.headers
    assert "ETag" not in first.headers

    second = client.get(DIALOGUES_DEFAULT_URL)
    assert second.status_code == 200
    assert second.headers["X-Panel-Cache"] == "hit"
    # The cache-served hit carries the work-os client contract: no-store, no
    # ETag, no 304 — exactly like its sibling hydrations.
    assert second.headers["Cache-Control"] == "no-store"
    assert "ETag" not in second.headers
    assert second.get_data() == first.get_data()
    assert second.get_json() == first.get_json()


def test_hit_skips_the_read_connection_and_context_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counters = {"connect": 0, "sha256": 0}
    real_connect = evaluation_dialogues.connect_sqlite
    real_sha256 = evaluation_dialogues.sha256

    def counting_connect(*args: Any, **kwargs: Any) -> Any:
        counters["connect"] += 1
        return real_connect(*args, **kwargs)

    def counting_sha256(*args: Any, **kwargs: Any) -> Any:
        counters["sha256"] += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(evaluation_dialogues, "connect_sqlite", counting_connect)
    monkeypatch.setattr(evaluation_dialogues, "sha256", counting_sha256)

    client = _client(tmp_path)
    first = client.get(DIALOGUES_DEFAULT_URL)
    assert first.status_code == 200
    assert first.headers["X-Panel-Cache"] == "miss"
    # The miss opened exactly one read connection and hashed both contexts.
    assert counters == {"connect": 1, "sha256": 2}

    second = client.get(DIALOGUES_DEFAULT_URL)
    assert second.headers["X-Panel-Cache"] == "hit"
    assert counters == {"connect": 1, "sha256": 2}, "the hit must re-verify nothing"
    assert second.get_data() == first.get_data()


def test_cached_body_equals_the_direct_loader_projection(tmp_path: Path) -> None:
    client = _client(tmp_path)

    missed = client.get(DIALOGUES_VARIANT_URL)
    assert missed.status_code == 200
    assert missed.headers["X-Panel-Cache"] == "miss"
    hit = client.get(DIALOGUES_VARIANT_URL)
    assert hit.headers["X-Panel-Cache"] == "hit"

    expected = _direct_projection(
        tmp_path, limit=10, sort="ticker_asc", filter_state="has_dialogue"
    )
    assert missed.get_json() == expected
    assert hit.get_json() == expected


def test_distinct_query_variants_cache_separately(tmp_path: Path) -> None:
    client = _client(tmp_path)

    default = client.get(DIALOGUES_DEFAULT_URL)
    variant = client.get(DIALOGUES_VARIANT_URL)
    assert default.headers["X-Panel-Cache"] == "miss"
    assert variant.headers["X-Panel-Cache"] == "miss"
    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "hit"
    assert client.get(DIALOGUES_VARIANT_URL).headers["X-Panel-Cache"] == "hit"


# ---------------------------------------------------------------------------
# Caching changes only how often validation runs, never its outcome
# (VAL-SERVE-005)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("break_kind", ["corrupt", "missing"])
def test_unreadable_or_missing_context_is_still_reported_on_miss_and_hit(
    tmp_path: Path, break_kind: str
) -> None:
    client = _client(tmp_path, break_bbb=break_kind)

    first = client.get(DIALOGUES_VARIANT_URL)
    assert first.status_code == 200
    assert first.headers["X-Panel-Cache"] == "miss"
    payload = first.get_json()
    assert payload["matching_state"] == "indeterminate"
    assert payload["total_matching"] is None
    assert "relevance_partial" in payload["reason_codes"]

    second = client.get(DIALOGUES_VARIANT_URL)
    assert second.headers["X-Panel-Cache"] == "hit"
    assert second.get_data() == first.get_data()
    # The cached body is exactly the loader's (rejected) outcome, not a
    # silently promoted state.
    assert second.get_json() == _direct_projection(
        tmp_path, limit=10, sort="ticker_asc", filter_state="has_dialogue"
    )


def test_unreadable_context_keeps_link_state_unknown_on_the_all_view(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, break_bbb="corrupt")

    payload = client.get(DIALOGUES_DEFAULT_URL).get_json()
    by_ticker = {item["ticker"]: item for item in payload["items"]}
    assert set(by_ticker) == {"AAA", "BBB"}
    # AAA still has its own valid linked session; BBB's unreadable context is
    # rejected, so its linkage stays unknown and the row is marked partial.
    assert by_ticker["AAA"]["ask_session_link_state"] == "linked"
    assert by_ticker["BBB"]["ask_session_link_state"] == "unknown"
    assert "relevance_partial" in payload["reason_codes"]


# ---------------------------------------------------------------------------
# Mutation invalidation (a write refreshes the dialogues cache)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "/api/ask/stream",
        "/api/ask/sessions/<session_id>",
        "/api/notes",
        "/api/notes/<int:note_id>/<action>",
        "/api/discovery/candidates/<int:cand_id>/status",
    ],
)
def test_dialogues_input_writes_invalidate_the_cached_key(rule: str) -> None:
    families = resolve_mutation_families(rule)
    assert families is not None, rule
    assert any(DIALOGUES_CACHE_PREFIX.startswith(prefix) for prefix in families), rule


def test_declared_noop_mutation_does_not_evict_the_dialogues_cache(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "miss"
    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "hit"

    assert (
        client.post(
            "/api/metrics/panel",
            json={"panel": "evaluation", "cache": "cold", "total_ms": 1},
        ).status_code
        == 204
    )

    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "hit"


def test_unknown_mutation_clears_the_dialogues_cache_fail_safe(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.application.add_url_rule(
        "/test/unregistered-dialogues-mutation",
        "test_unregistered_dialogues_mutation",
        lambda: ("", 204),
        methods=["POST"],
    )
    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "miss"
    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "hit"

    assert client.post("/test/unregistered-dialogues-mutation").status_code == 204

    assert client.get(DIALOGUES_DEFAULT_URL).headers["X-Panel-Cache"] == "miss"
