"""Explicit ONON input authority controls dispatch without checkout-local defaults."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from dcf.onon_inputs import RECIPE
from execution import refresh_dcf


def package(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "input_evidence": {
                    "recipe": RECIPE,
                    "ticker": "ONON",
                    "research_snapshot_id": "synthetic-snapshot",
                    "financial_period_end": "2026-06-30",
                    "facts": {},
                    "assumptions": {},
                }
            }
        )
    )
    return path


@pytest.mark.parametrize("via_environment", [False, True])
def test_clean_checkout_routes_explicit_onon_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_environment: bool,
) -> None:
    artifact = package(tmp_path / "reviewed.json")
    if via_environment:
        monkeypatch.setenv("DCF_ONON_ASSUMPTIONS_PATH", str(artifact))
    seen: list[str] = []

    def child(
        args: list[str], *, env: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(args[-1])
        assert args[-1].endswith("build_onon_dcf.py")
        assert env["DCF_ONON_ASSUMPTIONS_PATH"] == str(artifact.resolve())
        assert (
            env["DCF_ONON_ASSUMPTIONS_SHA256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="synthetic child boundary")

    monkeypatch.setattr(refresh_dcf.subprocess, "run", child)
    result = refresh_dcf.refresh_one(
        "ONON",
        tmp_path,
        tmp_path / "db",
        valuation_year=2026,
        onon_assumptions_path=None if via_environment else artifact,
    )
    assert result["format"] == "onon_economic_fcff"
    assert len(seen) == 1
    assert not (tmp_path / "dcf" / "ONON.xlsx").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_ticker",
        "wrong_recipe",
        "malformed",
        "missing",
        "owner_conflict",
        "malformed_hint",
        "generic_conflict",
    ],
)
def test_invalid_explicit_authority_refuses_before_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    artifact = package(tmp_path / "reviewed.json")
    if failure == "missing":
        artifact.unlink()
    elif failure == "malformed":
        artifact.write_text("{")
    elif failure in {"wrong_ticker", "wrong_recipe"}:
        data = json.loads(artifact.read_text())
        data["input_evidence"]["ticker" if failure == "wrong_ticker" else "recipe"] = (
            "NU" if failure == "wrong_ticker" else "other/v1"
        )
        artifact.write_text(json.dumps(data))
    else:
        hint = tmp_path / (
            "data/dcf_assumptions/ONON.json"
            if failure == "generic_conflict"
            else "micro_thesis/holdings/ONON.json"
        )
        hint.parent.mkdir(parents=True)
        hint.write_text(
            "{"
            if failure == "malformed_hint"
            else json.dumps(
                {"redesign": {"valuation_model": "fcff_dcf"}}
                if failure == "generic_conflict"
                else {"valuation_model": "bank_excess_return"}
            )
        )
    dest = tmp_path / "dcf" / "ONON.xlsx"
    dest.parent.mkdir()
    dest.write_bytes(b"keep")
    staged = dest.with_name("ONON.rebuild.xlsx")
    staged.write_bytes(b"keep staged")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("authority refusal must precede child execution")

    monkeypatch.setattr(refresh_dcf.subprocess, "run", forbidden)
    result = refresh_dcf.refresh_one(
        "ONON", tmp_path, tmp_path / "db", valuation_year=2026, onon_assumptions_path=artifact
    )
    assert result["status"] == "error"
    assert dest.read_bytes() == b"keep"
    assert staged.read_bytes() == b"keep staged"
    assert not (tmp_path / "db").exists()


def test_non_onon_bulk_routing_ignores_onon_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def route(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ticker": "META", "status": "synthetic-route"}

    monkeypatch.setattr(refresh_dcf, "_refresh_redesign", route)
    result = refresh_dcf.refresh_one(
        "META",
        tmp_path,
        tmp_path / "db",
        valuation_year=2026,
        onon_assumptions_path=tmp_path / "missing.json",
    )
    assert result["status"] == "synthetic-route"


@pytest.mark.parametrize("exit_code,result_ticker", [(1, "ONON"), (0, "META")])
def test_onon_child_failure_or_wrong_result_cannot_claim_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    result_ticker: str,
) -> None:
    artifact = package(tmp_path / "reviewed.json")
    live = tmp_path / "dcf" / "ONON.xlsx"
    live.parent.mkdir()
    live.write_bytes(b"previous workbook must survive")

    def child(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            exit_code,
            stdout=f"RESULT\t{result_ticker}\tvalue/sh=$34.47\tdcf_runs=ok\n",
            stderr="synthetic failed completion" if exit_code else "",
        )

    monkeypatch.setattr(refresh_dcf.subprocess, "run", child)
    result = refresh_dcf.refresh_one(
        "ONON",
        tmp_path,
        tmp_path / "synthetic.db",
        valuation_year=2026,
        onon_assumptions_path=artifact,
    )
    assert result["status"] == "failed"
    assert live.read_bytes() == b"previous workbook must survive"
