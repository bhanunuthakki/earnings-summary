"""Earnings generators must use the explicit or configured database authority."""

import sys
from pathlib import Path

import pytest

import earnings_brief
import earnings_readout
from execution import generate_post_earnings_readouts, generate_pre_earnings_briefs


@pytest.mark.parametrize("lane", ["pre", "post"])
@pytest.mark.parametrize("route", ["configured", "explicit", "missing", "checkout", "absent"])
def test_generator_database_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str, route: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        root
        / "execution"
        / (
            "generate_pre_earnings_briefs.py"
            if lane == "pre"
            else "generate_post_earnings_readouts.py"
        )
    )
    configured = tmp_path / "configured-state.db"
    explicit = tmp_path / "explicit-fixture.db"
    # Only path validation runs. These marker files are never opened as databases.
    configured.touch()
    explicit.touch()
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    calls: list[Path] = []

    def capture_run(db_path: Path, repo_root: Path, **kwargs: object) -> dict[str, int]:
        calls.append(db_path)
        return {"generated": 0}

    monkeypatch.setattr(
        earnings_brief if lane == "pre" else earnings_readout, "generate_all", capture_run
    )
    argv = [str(script)]
    if route in {"configured", "explicit"}:
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(configured))
    if route == "explicit":
        argv.extend(["--db-path", str(explicit)])
    elif route == "checkout":
        argv.extend(["--db-path", str(root / "data" / "portfolio.db")])
    elif route == "absent":
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "absent-state.db"))
    monkeypatch.setattr(sys, "argv", argv)
    main = (
        generate_pre_earnings_briefs.main if lane == "pre" else generate_post_earnings_readouts.main
    )

    if route in {"missing", "checkout", "absent"}:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
        assert calls == []
    else:
        assert main() == 0
        assert calls == [(explicit if route == "explicit" else configured).resolve()]
