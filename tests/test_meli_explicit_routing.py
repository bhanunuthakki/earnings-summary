"""Explicit MELI input authority controls dispatch without checkout-local defaults."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from dcf.input_evidence import InputEvidenceError
from dcf.meli_inputs import RECIPE
from execution import build_meli_platform_dcf as meli
from execution import refresh_dcf


def package(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "input_evidence": {
                    "recipe": RECIPE,
                    "ticker": "MELI",
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
def test_clean_checkout_routes_explicit_meli_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_environment: bool,
) -> None:
    artifact = package(tmp_path / "reviewed.json")
    if via_environment:
        monkeypatch.setenv("DCF_MELI_ASSUMPTIONS_PATH", str(artifact))
    seen: list[str] = []

    def child(
        args: list[str], *, env: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(args[-1])
        assert args[-1].endswith("build_meli_platform_dcf.py")
        assert env["DCF_MELI_ASSUMPTIONS_PATH"] == str(artifact.resolve())
        assert (
            env["DCF_MELI_ASSUMPTIONS_SHA256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="synthetic child boundary")

    monkeypatch.setattr(refresh_dcf.subprocess, "run", child)
    result = refresh_dcf.refresh_one(
        "MELI",
        tmp_path,
        tmp_path / "db",
        valuation_year=2026,
        meli_assumptions_path=None if via_environment else artifact,
    )
    assert result["format"] == "meli_platform_sotp"
    assert len(seen) == 1
    assert not (tmp_path / "dcf" / "MELI.xlsx").exists()


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
            "data/dcf_assumptions/MELI.json"
            if failure == "generic_conflict"
            else "micro_thesis/holdings/MELI.json"
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
    dest = tmp_path / "dcf" / "MELI.xlsx"
    dest.parent.mkdir()
    dest.write_bytes(b"keep")
    staged = dest.with_name("MELI.rebuild.xlsx")
    staged.write_bytes(b"keep staged")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("authority refusal must precede child execution")

    monkeypatch.setattr(refresh_dcf.subprocess, "run", forbidden)
    result = refresh_dcf.refresh_one(
        "MELI", tmp_path, tmp_path / "db", valuation_year=2026, meli_assumptions_path=artifact
    )
    assert result["status"] == "error"
    assert dest.read_bytes() == b"keep"
    assert staged.read_bytes() == b"keep staged"
    assert not (tmp_path / "db").exists()


def test_changed_parent_bytes_fail_before_child_database_access(tmp_path: Path) -> None:
    artifact = package(tmp_path / "reviewed.json")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.write_text(artifact.read_text() + " ")
    with pytest.raises(InputEvidenceError, match="assumptions_authority_changed_after_dispatch"):
        meli.load_verified_assumptions(
            "MELI",
            db_path=tmp_path / "missing.db",
            assumptions_path=artifact,
            expected_sha256=digest,
        )
    assert not (tmp_path / "missing.db").exists()


def test_non_meli_bulk_routing_ignores_meli_package(
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
        meli_assumptions_path=tmp_path / "missing.json",
    )
    assert result["status"] == "synthetic-route"


@pytest.mark.parametrize("field,value", [("ticker", "NU"), ("recipe", "other/v1")])
def test_child_rejects_wrong_identity_before_database_access(
    tmp_path: Path, field: str, value: str
) -> None:
    artifact = package(tmp_path / "reviewed.json")
    data = json.loads(artifact.read_text())
    data["input_evidence"][field] = value
    artifact.write_text(json.dumps(data))
    with pytest.raises(InputEvidenceError):
        meli.load_verified_assumptions(
            "MELI",
            db_path=tmp_path / "missing.db",
            assumptions_path=artifact,
            expected_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )
    assert not (tmp_path / "missing.db").exists()
