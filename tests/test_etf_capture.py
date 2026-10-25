"""Synthetic contracts for ETF source capture; no live acquisition."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

import execution.fetch_etf_data as etf
from etf_sources.profile_evidence import admitted_profile_fields, capture_profile_fields
from instrument_store import get_etf_profile
from models.instruments import EtfFieldEvidence, EtfProfile
from net.client import HTTP_CLIENT, HttpJsonResponse, HttpResponseCapture, JsonValue
from pipeline.fmp_doc_index import bind_etf_capture, register_fmp_snapshot


def test_partial_capture_linkage_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EtfFieldEvidence(value="Energy", source="fmp", captured_at=datetime.now(UTC), document_id=1)


def test_unlinked_fmp_receipt_cannot_admit_current_label() -> None:
    now = datetime.now(UTC)
    profile = capture_profile_fields(
        EtfProfile(ticker="ZZZ", sector_label="Energy", profile_fetched_at=now)
    )
    assert admitted_profile_fields(profile, now=now) == {}


def test_issuer_receipt_policy_is_preserved() -> None:
    now = datetime.now(UTC)
    profile = capture_profile_fields(
        EtfProfile(ticker="ZZZ", source="issuer", sector_label="Energy", profile_fetched_at=now)
    )
    assert set(admitted_profile_fields(profile, now=now)) == {"sector_label"}


@pytest.fixture()
def capture_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, Path]]:
    root = tmp_path / "state"
    conn = sqlite3.connect(migrated_db(root / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        yield conn, root
    finally:
        conn.rollback()
        conn.close()


def _reply(
    payload: JsonValue, path: str, *, body: bytes | None = None, clock: datetime | None = None
) -> HttpJsonResponse:
    capture = HttpResponseCapture(
        body=json.dumps(payload).encode() if body is None else body,
        received_at=clock or datetime.now(UTC) - timedelta(minutes=1),
        capture_id=str(uuid4()),
        origin="https://financialmodelingprep.com",
        path=f"/stable/{path}",
    )
    return HttpJsonResponse(200, payload, capture)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    info: HttpJsonResponse,
    holdings: HttpJsonResponse | None = None,
) -> None:
    monkeypatch.setenv("FMP_API_KEY", "synthetic-test-key")

    def fetch(_key: str, _ticker: str, path: str) -> HttpJsonResponse:
        return info if path == "etf/info" else holdings or _reply([], "etf/holdings")

    monkeypatch.setattr(etf, "_fmp_get", fetch)


def _counts(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "documents",
            "evidence_source_observations",
            "evidence_document_versions",
            "etf_profile",
            "etf_holdings",
        )
    )


def test_exact_capture_roundtrip_and_separate_clocks(
    capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    info_time = datetime.now(UTC) - timedelta(minutes=2)
    held_time = info_time + timedelta(seconds=20)
    body = b'[\n {"symbol":"ZZZ", "sector":"Energy", "yield":0, "dividendYield":5, "expenseRatio":"0.15%", "assetClass":"Equity", "etfIndex":"", "benchmark":"Index"}\n]\n'
    info = _reply(cast(JsonValue, json.loads(body)), "etf/info", body=body, clock=info_time)
    holdings = _reply([{"symbol": "OTHER", "weightPercentage": 7}], "etf/holdings", clock=held_time)
    _wire(monkeypatch, info, holdings)
    profile, count = etf.ingest_live(conn, "ZZZ", root)
    assert conn.in_transaction
    assert count == 1 and profile.profile_fetched_at == info_time
    assert (
        conn.execute("SELECT fetched_at FROM etf_holdings").fetchone()[0] == held_time.isoformat()
    )
    stored = get_etf_profile(conn, "ZZZ")
    assert stored is not None and stored == profile
    assert set(admitted_profile_fields(stored)) == {
        "asset_class",
        "benchmark_index",
        "sector_label",
        "expense_ratio",
        "distribution_yield",
    }
    expected = {
        "asset_class": "/0/assetClass",
        "benchmark_index": "/0/benchmark",
        "sector_label": "/0/sector",
        "expense_ratio": "/0/expenseRatio",
        "distribution_yield": "/0/yield",
    }
    assert {
        key: evidence.json_pointer for key, evidence in profile.field_evidence.items()
    } == expected
    evidence = profile.field_evidence["distribution_yield"]
    assert evidence.value == 0.0 and evidence.source_as_of is None
    assert evidence.content_sha256 == hashlib.sha256(body).hexdigest()
    assert evidence.source_url == "https://financialmodelingprep.com/stable/etf/info?symbol=ZZZ"
    for row in conn.execute("SELECT file_path,sha256,source_url FROM documents"):
        retained = (root / row[0]).read_bytes()
        assert hashlib.sha256(retained).hexdigest() == row[1]
        assert "apikey" not in row[2]
    assert (
        root
        / conn.execute(
            "SELECT file_path FROM documents WHERE id=?", (evidence.document_id,)
        ).fetchone()[0]
    ).read_bytes() == body
    assert not (root / "data/historical/fmp").exists()
    clocks = conn.execute(
        "SELECT observed_at,retrieved_at,source_published_at FROM evidence_source_observations WHERE observation_id=?",
        (evidence.observation_id,),
    ).fetchone()
    assert datetime.fromisoformat(clocks[0]) == info_time
    assert datetime.fromisoformat(clocks[1]) == info_time and clocks[2] is None
    # The caller owns durability; a fresh connection cannot see this unit yet.
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(database) as other:
        assert other.execute("SELECT COUNT(*) FROM etf_profile").fetchone()[0] == 0
    conn.commit()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [{"symbol": "ZZZ"}, {"symbol": "ZZZ"}],
        [1],
        {"sector": "Energy"},
        {"symbol": "WRONG"},
        {"symbol": True},
        {"symbol": "ZZZ", "error": "denied"},
    ],
)
def test_info_identity_rejected_before_writes(
    payload: JsonValue, capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    before = _counts(conn)
    _wire(monkeypatch, _reply(payload, "etf/info"))
    with pytest.raises(ValueError):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and not conn.in_transaction
    assert not (root / "data/historical/fmp_snapshots").exists()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "naive",
        "future",
        "wrong_origin",
        "wrong_path",
        "uuid",
        "mismatch",
        "numeric_type",
        "duplicate",
        "nan",
        "overflow",
        "bad_json",
    ],
)
def test_invalid_capture_rejected_before_writes(
    case: str, capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    response = _reply({"symbol": "ZZZ", "yield": 0}, "etf/info")
    capture = response.capture
    assert capture is not None
    if case == "missing":
        response = HttpJsonResponse(200, response.payload)
    elif case == "naive":
        response = replace(response, capture=replace(capture, received_at=datetime.now()))
    elif case == "future":
        response = replace(
            response, capture=replace(capture, received_at=datetime.now(UTC) + timedelta(days=1))
        )
    elif case == "wrong_origin":
        response = replace(response, capture=replace(capture, origin="https://example.test"))
    elif case == "wrong_path":
        response = replace(response, capture=replace(capture, path="/stable/profile"))
    elif case == "uuid":
        response = replace(response, capture=replace(capture, capture_id="invalid"))
    elif case == "mismatch":
        response = replace(response, payload={"symbol": "ZZZ", "yield": 2})
    elif case == "numeric_type":
        response = replace(response, payload={"symbol": "ZZZ", "yield": False})
    else:
        bodies = {
            "duplicate": b'{"symbol":"WRONG","symbol":"ZZZ"}',
            "nan": b'{"symbol":"ZZZ","yield":NaN}',
            "overflow": b'{"symbol":"ZZZ","yield":1e999}',
            "bad_json": b'{"symbol":',
        }
        response = replace(response, capture=replace(capture, body=bodies[case]))
    _wire(monkeypatch, response)
    before = _counts(conn)
    with pytest.raises(ValueError):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before
    assert not (root / "data/historical/fmp_snapshots").exists()


@pytest.mark.parametrize("payload", [{}, [1], [{"fundSymbol": "OTHER"}], [{"etfSymbol": "OTHER"}]])
def test_holdings_shape_and_explicit_fund_identity(
    payload: JsonValue, capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    _wire(monkeypatch, _reply({"symbol": "ZZZ"}, "etf/info"), _reply(payload, "etf/holdings"))
    before = _counts(conn)
    with pytest.raises(ValueError):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and not (root / "data/historical/fmp_snapshots").exists()


@pytest.mark.parametrize(
    "stage", ["second_fetch", "registration", "native_link", "profile", "holdings"]
)
@pytest.mark.parametrize("outer", [False, True])
def test_atomic_failure_preserves_prior_and_outer_writes(
    stage: str,
    outer: bool,
    capture_db: tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, root = capture_db
    _wire(monkeypatch, _reply({"symbol": "ZZZ", "sector": "Energy"}, "etf/info"))
    etf.ingest_live(conn, "ZZZ", root)
    conn.commit()
    prior = get_etf_profile(conn, "ZZZ")
    before = _counts(conn)
    conn.execute("CREATE TABLE pending_owner(value TEXT)")
    conn.commit()
    if outer:
        conn.execute("INSERT INTO pending_owner VALUES ('keep')")
    _wire(monkeypatch, _reply({"symbol": "ZZZ", "sector": "Utilities"}, "etf/info"))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("injected failure")

    if stage == "second_fetch":

        def second(key: str, ticker: str, path: str) -> HttpJsonResponse:
            if path == "etf/holdings":
                raise OSError("injected transport failure")
            return _reply({"symbol": ticker, "sector": "Utilities"}, "etf/info")

        monkeypatch.setattr(etf, "_fmp_get", second)
    else:
        target = {
            "registration": "register_fmp_snapshot",
            "native_link": "bind_etf_capture",
            "profile": "upsert_etf_profile",
            "holdings": "upsert_etf_holdings",
        }[stage]
        monkeypatch.setattr(etf, target, fail)
    with pytest.raises((sqlite3.Error, OSError)):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and get_etf_profile(conn, "ZZZ") == prior
    assert conn.in_transaction is outer
    assert conn.execute("SELECT COUNT(*) FROM pending_owner").fetchone()[0] == int(outer)
    conn.commit()


def test_provider_disabled_does_not_fetch_or_write(
    capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    policy = root / "fmp-policy.json"
    policy.write_text('{"schema_version":1,"enabled":false}')
    monkeypatch.setenv("FMP_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("EARNINGS_SUMMARY_FMP_POLICY_FILE", str(policy))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("HTTP must remain disabled")

    monkeypatch.setattr(HTTP_CLIENT, "request_json", forbidden)
    before = _counts(conn)
    with pytest.raises(RuntimeError, match="provider_disabled_by_owner"):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and not (root / "data/historical/fmp_snapshots").exists()


def _register_capture(
    conn: sqlite3.Connection,
    root: Path,
    response: HttpJsonResponse,
    *,
    source_url: str | None = None,
) -> tuple[int, str, str]:
    capture = response.capture
    assert capture is not None
    document_id, _ = register_fmp_snapshot(
        conn,
        ticker="ZZZ",
        source_filename="ZZZ_etf_info.json",
        content=capture.body,
        project_root=root,
        captured_at=capture.received_at,
        period_end=None,
        source_url=source_url,
    )
    receipt = bind_etf_capture(
        conn,
        document_id=document_id,
        ticker="ZZZ",
        source_filename="ZZZ_etf_info.json",
        capture=capture,
    )
    return document_id, receipt.document_version_id, receipt.observation_id


def test_equal_bytes_recapture_replay_and_conflict(
    capture_db: tuple[sqlite3.Connection, Path],
) -> None:
    conn, root = capture_db
    first = _reply(
        {"symbol": "ZZZ", "sector": "Energy"}, "etf/info", clock=datetime(2024, 1, 1, tzinfo=UTC)
    )
    conn.execute("BEGIN")
    document_id, version_id, first_obs = _register_capture(
        conn, root, first, source_url="https://financialmodelingprep.com/legacy"
    )
    original_document = tuple(
        conn.execute(
            "SELECT fetched_at,sha256,source_url FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    )
    original_version = tuple(
        conn.execute(
            "SELECT recorded_at,observation_id FROM evidence_document_versions WHERE document_version_id=?",
            (version_id,),
        ).fetchone()
    )
    assert first.capture is not None
    fresh = replace(
        first,
        capture=replace(
            first.capture,
            capture_id=str(uuid4()),
            received_at=first.capture.received_at + timedelta(seconds=10),
        ),
    )
    second_id, second_version, second_obs = _register_capture(
        conn, root, fresh, source_url="https://example.test/new"
    )
    assert (second_id, second_version) == (document_id, version_id) and second_obs != first_obs
    assert _register_capture(conn, root, fresh) == (second_id, second_version, second_obs)
    assert (
        tuple(
            conn.execute(
                "SELECT fetched_at,sha256,source_url FROM documents WHERE id=?", (document_id,)
            ).fetchone()
        )
        == original_document
    )
    assert (
        tuple(
            conn.execute(
                "SELECT recorded_at,observation_id FROM evidence_document_versions WHERE document_version_id=?",
                (version_id,),
            ).fetchone()
        )
        == original_version
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_document_observation_links WHERE link_kind='retrieval'"
        ).fetchone()[0]
        == 2
    )
    assert fresh.capture is not None
    conflict = replace(fresh.capture, received_at=fresh.capture.received_at + timedelta(seconds=1))
    with pytest.raises(ValueError):
        bind_etf_capture(
            conn,
            document_id=document_id,
            ticker="ZZZ",
            source_filename="ZZZ_etf_info.json",
            capture=conflict,
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_source_observations WHERE observation_id LIKE 'etf-fmp-http:%'"
        ).fetchone()[0]
        == 2
    )


def test_a_b_a_keeps_history_and_reuses_original_identity(
    capture_db: tuple[sqlite3.Connection, Path],
) -> None:
    conn, root = capture_db
    conn.execute("BEGIN")
    identities = [
        _register_capture(conn, root, _reply({"symbol": "ZZZ", "sector": value}, "etf/info"))
        for value in ("Energy", "Utilities", "Energy")
    ]
    assert identities[0][:2] == identities[2][:2] and identities[0][0] != identities[1][0]
    assert len({identity[2] for identity in identities}) == 3
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_document_observation_links WHERE link_kind='retrieval'"
        ).fetchone()[0]
        == 3
    )


def test_global_hash_conflict_preserves_committed_etf(
    capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    _wire(monkeypatch, _reply({"symbol": "ZZZ", "sector": "Energy"}, "etf/info"))
    etf.ingest_live(conn, "ZZZ", root)
    conn.commit()
    before = _counts(conn)
    _wire(monkeypatch, _reply({"symbol": "ZZY", "sector": "Utilities"}, "etf/info"))
    with pytest.raises(ValueError, match="conflicts"):
        etf.ingest_live(conn, "ZZY", root)
    assert _counts(conn) == before and get_etf_profile(conn, "ZZY") is None


def test_missing_native_binding_rolls_back(
    capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.fmp_doc_index as fmp_index

    conn, root = capture_db
    _wire(monkeypatch, _reply({"symbol": "ZZZ", "sector": "Energy"}, "etf/info"))

    def no_binding(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fmp_index, "ensure_legacy_document_evidence", no_binding)
    before = _counts(conn)
    with pytest.raises(ValueError):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and not conn.in_transaction


def test_no_clobber_failure_never_overwrites_or_admits(
    capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from provenance.immutable_artifact import ImmutableArtifactConflictError

    conn, root = capture_db
    info = _reply({"symbol": "ZZZ", "sector": "Energy"}, "etf/info")
    assert info.capture is not None
    digest = hashlib.sha256(info.capture.body).hexdigest()
    retained = root / "data/historical/fmp_snapshots" / digest[:2] / digest / "ZZZ_etf_info.json"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"existing corrupt bytes")
    _wire(monkeypatch, info)
    before = _counts(conn)
    with pytest.raises(ImmutableArtifactConflictError):
        etf.ingest_live(conn, "ZZZ", root)
    assert _counts(conn) == before and retained.read_bytes() == b"existing corrupt bytes"


@pytest.mark.parametrize(
    "script", ["execution.fetch_etf_data", "execution.fetch_etf_published_data"]
)
@pytest.mark.parametrize("authority", ["missing", "mismatched"])
def test_cli_requires_bound_database_and_state_root(
    script: str,
    authority: str,
    capture_db: tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    conn, root = capture_db
    module = importlib.import_module(script)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)

    def missing_root(_root: Path) -> Path:
        raise ValueError("missing configured authority")

    monkeypatch.setattr(module, "configured_product_state_root", missing_root)
    argv = [script, "--ticker", "ZZZ"]
    if authority == "mismatched":
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        argv += ["--db", database, "--repo-root", str(root / "wrong")]
    monkeypatch.setattr(sys, "argv", argv)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must fail before connecting")

    monkeypatch.setattr(module, "connect_sqlite", forbidden)
    with pytest.raises(ValueError):
        module.main()


@pytest.mark.parametrize("unexpected", [False, True])
def test_optional_fmp_failure_preserves_committed_published_lane(
    unexpected: bool,
    capture_db: tuple[sqlite3.Connection, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    import fetch_etf_data as cli_etf

    import execution.fetch_etf_published_data as published
    from etf_sources.ingest import PublishedDataResult

    conn, root = capture_db
    database = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.execute("CREATE TABLE lane_proof(value TEXT)")
    conn.commit()

    def base(
        base_conn: sqlite3.Connection, ticker: str, state_root: Path, **_kwargs: object
    ) -> PublishedDataResult:
        assert state_root == root
        base_conn.execute("INSERT INTO lane_proof VALUES ('published')")
        return PublishedDataResult(
            ticker, "already_done", None, 0, "unavailable", 0, None, False, "skipped", 0
        )

    def optional(optional_conn: sqlite3.Connection, _ticker: str, state_root: Path) -> None:
        assert state_root == root
        optional_conn.execute("INSERT INTO lane_proof VALUES ('failed fmp')")
        if unexpected:
            raise KeyError("programming error")
        raise ValueError("expected optional failure?apikey=synthetic-secret")

    monkeypatch.setattr(published, "refresh_published_data", base)
    monkeypatch.setattr(cli_etf, "ingest_live", optional)
    monkeypatch.setattr(
        sys,
        "argv",
        ["published", "--ticker", "ZZZ", "--db", str(database), "--repo-root", str(root), "--fmp"],
    )
    if unexpected:
        with pytest.raises(KeyError):
            published.main()
    else:
        assert published.main() == 0
        output = capsys.readouterr().out
        assert "unavailable" in output and "synthetic-secret" not in output
    assert [row[0] for row in conn.execute("SELECT value FROM lane_proof")] == ["published"]


@pytest.mark.parametrize(
    "script", ["execution.fetch_etf_data", "execution.fetch_etf_published_data"]
)
def test_explicit_cli_root_still_loads_approved_environment(
    script: str, capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import os
    import sys

    from etf_sources.ingest import PublishedDataResult

    conn, root = capture_db
    module = importlib.import_module(script)
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    loaded: list[Path] = []

    def load(code_root: Path) -> None:
        loaded.append(code_root)
        monkeypatch.setenv("FMP_API_KEY", "synthetic-loaded-key")

    def live(
        _conn: sqlite3.Connection, ticker: str, state_root: Path, **_kwargs: object
    ) -> tuple[EtfProfile, int]:
        assert os.environ["FMP_API_KEY"] == "synthetic-loaded-key" and state_root == root
        return EtfProfile(ticker=ticker, profile_fetched_at=datetime.now(UTC)), 0

    def base(
        _conn: sqlite3.Connection, ticker: str, state_root: Path, **_kwargs: object
    ) -> PublishedDataResult:
        assert loaded == [module.PROJECT_ROOT] and state_root == root
        return PublishedDataResult(
            ticker, "already_done", None, 0, "unavailable", 0, None, False, "skipped", 0
        )

    monkeypatch.setattr(module, "load_project_env", load)
    argv = [script, "--ticker", "ZZZ", "--db", database, "--repo-root", str(root)]
    if script.endswith("published_data"):
        import fetch_etf_data as cli_etf

        monkeypatch.setattr(module, "refresh_published_data", base)
        monkeypatch.setattr(cli_etf, "ingest_live", live)
        argv.append("--fmp")
    else:
        monkeypatch.setattr(module, "ingest_live", live)
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0 and loaded == [module.PROJECT_ROOT]


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("legacy_document_evidence_binding_revisions", "scope_content_sha256"),
        ("evidence_extraction_runs", "input_sha256"),
        ("evidence_nodes", "node_kind"),
        ("evidence_document_versions", "blob_sha256"),
    ],
)
def test_capture_rejects_unavailable_binding_identity(
    table: str, column: str, capture_db: tuple[sqlite3.Connection, Path]
) -> None:
    conn, root = capture_db
    response = _reply({"symbol": "ZZZ", "sector": "Energy"}, "etf/info")
    conn.execute("BEGIN")
    document_id, _, _ = _register_capture(conn, root, response)
    assert response.capture is not None
    fresh = replace(response.capture, capture_id=str(uuid4()))
    before = _counts(conn)

    def unavailable(
        action: int,
        first: str | None,
        second: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        return (
            sqlite3.SQLITE_IGNORE
            if action == sqlite3.SQLITE_READ and (first, second) == (table, column)
            else sqlite3.SQLITE_OK
        )

    conn.set_authorizer(unavailable)
    try:
        with pytest.raises(ValueError):
            bind_etf_capture(
                conn,
                document_id=document_id,
                ticker="ZZZ",
                source_filename="ZZZ_etf_info.json",
                capture=fresh,
            )
    finally:
        conn.set_authorizer(None)
    assert _counts(conn) == before


@pytest.mark.parametrize("outer", [False, True])
def test_savepoint_creation_failure_releases_only_owned_transaction(
    outer: bool, capture_db: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = capture_db
    _wire(monkeypatch, _reply({"symbol": "ZZZ"}, "etf/info"))
    conn.execute("CREATE TABLE pending_owner(value TEXT)")
    conn.commit()
    if outer:
        conn.execute("INSERT INTO pending_owner VALUES ('keep')")
    before = _counts(conn)

    def deny_savepoint(
        action: int,
        first: str | None,
        _second: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_SAVEPOINT and first == "BEGIN"
            else sqlite3.SQLITE_OK
        )

    conn.set_authorizer(deny_savepoint)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            etf.ingest_live(conn, "ZZZ", root)
    finally:
        conn.set_authorizer(None)
    assert conn.in_transaction is outer and _counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM pending_owner").fetchone()[0] == int(outer)
    assert not (root / "data/historical/fmp_snapshots").exists()
