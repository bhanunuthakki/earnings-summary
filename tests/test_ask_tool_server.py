"""Read-only research tools for the orchestrator agent (src/ask/tool_server.py)."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import cast

import pytest

import ask.tool_server as tool_server
from ask.grounding import EvidenceItem, EvidenceNeed
from ask.tool_server import ResearchToolSession, call_tool, handle_message, serve
from sources.report_financials import FinancialEvidenceReference

ROOT = Path(__file__).resolve().parents[1]


def _item(
    n: int, kind: str, label: str, text: str, reference: FinancialEvidenceReference | None = None
) -> EvidenceItem:
    return EvidenceItem(
        n=n,
        kind=kind,
        label=label,
        text=text,
        doc_id=None,
        href=None,
        canonical_reference=reference,
    )


def _rows(value: object) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", value)


def _payload(result: dict[str, object]) -> dict[str, object]:
    content = _rows(result["content"])
    return cast("dict[str, object]", json.loads(str(content[0]["text"])))


def _reply(message: dict[str, object] | None) -> dict[str, object]:
    assert message is not None
    return cast("dict[str, object]", message.get("result") or message.get("error"))


@pytest.fixture
def db(migrated_db: Callable[..., Path], tmp_path: Path) -> Path:
    path = migrated_db(tmp_path / "tools.db")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?, ?, ?, ?)",
            [
                ("NU", "Nu Holdings", "portfolio", None),
                ("MELI", "MercadoLibre", "evaluation", None),
                ("OLD", "Archived Co", "portfolio", "2026-01-01"),
                ("SPY", "Index ETF", "etf", None),
            ],
        )
        conn.executemany(
            "INSERT INTO expected_earnings (ticker, expected_date, detected_source) VALUES (?, ?, ?)",
            [("NU", "2026-11-12", "fmp"), ("NU", "2026-08-13", "fmp")],
        )
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    holdings = tmp_path / "repo" / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"ticker": "NU", "break_rules": [{"kpi_name": "NIM", "threshold": 17.5}]}),
        encoding="utf-8",
    )
    return tmp_path / "repo"


def test_coverage_lists_active_research_names_with_dates(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo, today=date(2026, 10, 20))
    names = {n["ticker"]: n for n in _rows(session.coverage()["names"])}

    assert set(names) == {"NU", "MELI"}
    assert names["NU"]["next_earnings"] == "2026-11-12"
    assert names["NU"]["next_earnings_status"] == "estimated"
    assert names["NU"]["last_report_estimate"] == "2026-08-13"
    assert names["NU"]["has_thesis"] is True
    assert names["MELI"]["next_earnings_status"] == "unannounced"


def test_coverage_never_writes_the_database(db: Path, repo: Path) -> None:
    before = db.read_bytes()
    ResearchToolSession(db_path=db, repo_root=repo).coverage()
    assert db.read_bytes() == before


def test_thesis_joins_ledger_once_as_owner_record(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    first = _payload(call_tool(session, "thesis", {"ticker": "nu"}))
    again = _payload(call_tool(session, "thesis", {"ticker": "NU"}))

    assert first["item"] == again["item"] == 1
    assert len(session.ledger) == 1
    assert session.manifest()["status_counts"] == {"owner_record": 1}
    assert _payload(call_tool(session, "thesis", {"ticker": "TSM"}))["available"] is False


@pytest.mark.parametrize("ticker", ["../secrets", "nu/../x", "", 7])
def test_thesis_rejects_non_symbol_tickers(db: Path, repo: Path, ticker: object) -> None:
    result = call_tool(
        ResearchToolSession(db_path=db, repo_root=repo), "thesis", {"ticker": ticker}
    )
    assert result["isError"] is True


def test_evidence_numbers_dedupes_and_labels_items(
    db: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = FinancialEvidenceReference.model_construct(ticker="NU", concept="revenue")
    returned = [
        _item(9, "fact", "NU · Revenue", "Q2'26 4.2B", reference),
        _item(9, "fact", "NU · Customers", "Q2'26 127.0M"),
        _item(9, "transcript", "NU Q2 call", "We expect NIM to normalize."),
        _item(9, "dcf", "NU DCF", "fair value 14.10"),
    ]
    seen: list[list[EvidenceNeed]] = []

    def fake(needs: list[EvidenceNeed], **_kwargs: object) -> list[EvidenceItem]:
        seen.append(needs)
        return returned

    monkeypatch.setattr(tool_server, "gather_requested_evidence", fake)
    session = ResearchToolSession(db_path=db, repo_root=repo)
    args = {
        "question": "What changed?",
        "needs": [{"kind": "fact", "ticker": "nu", "query": "revenue"}],
    }
    added = _rows(_payload(call_tool(session, "evidence", args))["added"])
    repeat = _payload(call_tool(session, "evidence", args))["added"]

    assert [i["n"] for i in added] == [1, 2, 3, 4]
    assert [i["status"] for i in added] == [
        "admitted",
        "unadmitted_fact",
        "source_passage",
        "owner_record",
    ]
    assert repeat == []
    assert seen[0][0] == EvidenceNeed(kind="fact", ticker="NU", period=None, query="revenue")


def test_evidence_rejects_unknown_channel(db: Path, repo: Path) -> None:
    args = {"question": "q", "needs": [{"kind": "web", "ticker": "NU"}]}
    assert call_tool(ResearchToolSession(db_path=db, repo_root=repo), "evidence", args)["isError"]


def _verified(
    session: ResearchToolSession, claims: list[dict[str, object]]
) -> list[dict[str, object]]:
    return _rows(_payload(call_tool(session, "verify_draft", {"claims": claims}))["claims"])


def test_verify_draft_binds_numbers_and_quotes_to_cited_items(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.extend(
        [
            _item(1, "fact", "NU · Revenue", "Q1'26 3.5B; Q2'26 4.2B"),
            _item(
                2,
                "transcript",
                "NU Q2 call",
                "Management said deposits grew faster than loans this quarter.",
            ),
        ]
    )
    results = _verified(
        session,
        [
            {"text": "Revenue reached 4.2B in Q2 2026 [1].", "cites": [1]},
            {"text": "Revenue reached 4.5B [1].", "cites": [1]},
            {"text": 'Management said "deposits grew faster than loans" [2].', "cites": [2]},
            {"text": 'They said "loans grew faster than deposits".', "cites": [2]},
            {"text": "Revenue was 4.2B.", "cites": [7]},
            {
                "text": "Revenue grew 20% quarter on quarter [1].",
                "cites": [1],
                "derived": [
                    {
                        "expression": "(b - a) / a * 100",
                        "operands": {"a": "3.5", "b": "4.2"},
                        "displayed_value": "20",
                        "decimals": 0,
                    }
                ],
            },
        ],
    )

    assert [r["status"] for r in results] == ["pass", "fail", "pass", "fail", "fail", "pass"]
    assert results[1]["reason"] == "memo_analysis_number_unbound"
    assert results[3]["reason"] == "memo_quoted_span_not_in_excerpt"
    assert results[4]["reason"] == "unknown_citation"
    assert results[0]["cited_status"] == ["unadmitted_fact"]


def test_verify_draft_reports_bad_arithmetic_as_failure(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.append(_item(1, "fact", "x", "0 and 5"))
    derived = [
        {
            "expression": "b / a",
            "operands": {"a": "0", "b": "5"},
            "displayed_value": "1",
            "decimals": 0,
        }
    ]
    results = _verified(session, [{"text": "Ratio 1 [1].", "cites": [1], "derived": derived}])
    assert results[0] == {"claim": 0, "status": "fail", "reason": "memo_derived_expression_invalid"}


def test_verify_draft_rejects_figures_supported_only_by_formula_constants(
    db: Path, repo: Path
) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.append(_item(1, "fact", "NU · Revenue", "Revenue 5"))
    inert = [
        {"expression": "a + 0 * 999", "operands": {"a": "5"}, "displayed_value": "5", "decimals": 0}
    ]
    unbound = [
        {"expression": "a * 2", "operands": {"a": "7"}, "displayed_value": "14", "decimals": 0}
    ]
    results = _verified(
        session,
        [
            {"text": "Revenue was 999 [1].", "cites": [1], "derived": inert},
            {"text": "Twice revenue is 14 [1].", "cites": [1], "derived": unbound},
        ],
    )
    assert [r["reason"] for r in results] == [
        "memo_analysis_number_unbound",
        "memo_derived_operand_unbound",
    ]


def test_verify_draft_binds_operands_numerically_and_chains_results(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.append(_item(1, "fact", "NU · Revenue", "Revenue 5"))
    year_operand = [
        {
            "expression": "a * 2",
            "operands": {"a": "2025"},
            "displayed_value": "4,050",
            "decimals": 0,
        }
    ]
    chained = [
        {"expression": "a * 2", "operands": {"a": "5"}, "displayed_value": "10", "decimals": 0},
        {"expression": "b * 2", "operands": {"b": "10"}, "displayed_value": "20", "decimals": 0},
    ]
    results = _verified(
        session,
        [
            {"text": "Revenue was 4,050 [1].", "cites": [1], "derived": year_operand},
            {"text": "Doubled 10, then 20 [1].", "cites": [1], "derived": chained},
        ],
    )
    assert results[0]["reason"] == "memo_derived_operand_unbound"
    assert results[1]["status"] == "pass"


def test_operand_binding_uses_readable_text_and_exact_values(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.extend(
        [_item(1, "fact", "x", "Revenue &#53;"), _item(2, "fact", "y", "Revenue 5.0")]
    )
    entity = [
        {"expression": "a * 2", "operands": {"a": "53"}, "displayed_value": "106", "decimals": 0}
    ]
    near = "5.0000000000000000000000000001"
    rounding = [
        {
            "expression": "(a - 5) * 10000000000000000000000000000",
            "operands": {"a": near},
            "displayed_value": "1",
            "decimals": 0,
        }
    ]
    equal = [
        {"expression": "a * 2", "operands": {"a": "5"}, "displayed_value": "10", "decimals": 0}
    ]
    results = _verified(
        session,
        [
            {"text": "Revenue was 106 [1].", "cites": [1], "derived": entity},
            {"text": "Gap 1 [2].", "cites": [2], "derived": rounding},
            {"text": "Twice revenue is 10 [2].", "cites": [2], "derived": equal},
        ],
    )
    assert [r.get("reason", r["status"]) for r in results] == [
        "memo_derived_operand_unbound",
        "memo_derived_operand_unbound",
        "pass",
    ]


def test_verify_draft_checks_inline_marks_and_short_quotes(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.extend(
        [
            _item(1, "fact", "NU · Customers", "Customers 127.0M"),
            _item(2, "fact", "NU · Revenue", "Revenue 4.2B, a record"),
        ]
    )
    results = _verified(
        session,
        [
            {"text": "Revenue was 4.2B [1].", "cites": [2]},
            {"text": 'Management said "record".', "cites": []},
            {"text": 'Management called it "a record" [2].', "cites": [2]},
        ],
    )
    assert [r.get("reason", r["status"]) for r in results] == [
        "inline_citation_not_in_cites",
        "memo_quoted_span_not_in_excerpt",
        "pass",
    ]


def test_malformed_tool_input_fails_the_call_but_keeps_the_session(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    session.ledger.append(_item(1, "fact", "x", "5"))
    malformed: dict[str, object] = {"question": "q", "needs": [{"kind": []}]}
    bad = call_tool(session, "evidence", malformed)
    assert bad["isError"] is True
    assert _payload(call_tool(session, "manifest", {}))["status_counts"] == {"unadmitted_fact": 1}


def _rpc(
    request_id: int, method: str, params: dict[str, object] | None = None
) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_json_rpc_handshake_listing_and_errors(db: Path, repo: Path) -> None:
    session = ResearchToolSession(db_path=db, repo_root=repo)
    init = _reply(handle_message(session, _rpc(1, "initialize", {"protocolVersion": "2024-11-05"})))
    assert init["protocolVersion"] == "2024-11-05"
    assert (
        handle_message(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    )
    tools = _rows(_reply(handle_message(session, _rpc(2, "tools/list")))["tools"])
    assert [t["name"] for t in tools] == [
        "coverage",
        "thesis",
        "evidence",
        "verify_draft",
        "manifest",
    ]
    assert _reply(handle_message(session, _rpc(3, "resources/list")))["code"] == -32601

    out = io.StringIO()
    serve(session, io.StringIO("not json\n\n"), out)
    assert json.loads(out.getvalue())["error"]["code"] == -32700

    out = io.StringIO()
    batch = [
        _rpc(4, "ping"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc(5, "ping"),
    ]
    serve(session, io.StringIO(json.dumps(batch) + "\n" + "[]\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies[0]] == [4, 5]
    assert replies[1]["error"]["code"] == -32600

    out = io.StringIO()
    serve(session, io.StringIO("[{}]\n{}\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0][0]["error"]["code"] == -32600
    assert replies[1]["error"]["code"] == -32600


def test_stdio_entrypoint_serves_coverage(db: Path) -> None:
    requests: list[dict[str, object]] = [
        _rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc(2, "tools/call", {"name": "coverage", "arguments": {}}),
    ]
    completed = subprocess.run(
        [sys.executable, str(ROOT / "execution" / "serve_research_tools.py"), "--db-path", str(db)],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    coverage = json.loads(replies[1]["result"]["content"][0]["text"])
    assert {n["ticker"] for n in coverage["names"]} == {"NU", "MELI"}
