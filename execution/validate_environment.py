"""Pre-flight environment check for the morning pipeline.

Validates the four conditions the daily jobs depend on:

  1. The configured external .env file exists (legacy PROJECT_ROOT/.env is a
     read-only migration fallback)
  2. FMP_API_KEY is present and non-empty in .env (or already in env)
  3. FMP_TIER is one of: free, basic, starter, premium (or absent — defaults to basic)
  4. playwright is importable (required by the IR-document + IR-KPI crons)

Prints a human-readable table to stdout and exits:

  0  all checks passed
  1  one or more checks failed (hard blockers printed in full)

The intent is "fail loud" — this script is run from the top of
run_morning_pipeline.py so any configuration gap surfaces in the cron log
rather than silently degrading the first stage that needs the missing value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.secrets import project_env_file  # noqa: E402

ENV_FILE = project_env_file(PROJECT_ROOT)
VALID_TIERS = {"free", "basic", "starter", "premium"}


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser — KEY=VALUE lines, strips quotes, ignores comments."""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
    except OSError:
        pass
    return result


def check() -> list[tuple[str, bool, str]]:
    """Run all checks; return list of (check_name, passed, detail) tuples."""
    results: list[tuple[str, bool, str]] = []

    # 1. .env present
    env_exists = ENV_FILE.exists()
    results.append(
        (
            ".env present",
            env_exists,
            str(ENV_FILE) if env_exists else f"not found at {ENV_FILE}",
        )
    )

    # Load .env values (merge with os.environ, env wins over file for overrides).
    dotenv = _load_dotenv(ENV_FILE) if env_exists else {}
    merged = {
        **dotenv,
        **{k: v for k, v in os.environ.items() if k in dotenv or k.startswith("FMP_")},
    }

    # 2. FMP_API_KEY non-empty
    fmp_key = merged.get("FMP_API_KEY", "").strip()
    results.append(
        (
            "FMP_API_KEY set",
            bool(fmp_key),
            "present" if fmp_key else "empty or missing — FMP fetches will fail",
        )
    )

    # 3. FMP_TIER valid (or absent, which defaults to basic)
    fmp_tier = merged.get("FMP_TIER", "").strip().lower()
    if not fmp_tier:
        tier_ok, tier_detail = True, "absent (defaults to basic)"
    elif fmp_tier in VALID_TIERS:
        tier_ok, tier_detail = True, fmp_tier
    else:
        tier_ok, tier_detail = False, f"{fmp_tier!r} not in {sorted(VALID_TIERS)}"
    results.append(("FMP_TIER valid", tier_ok, tier_detail))

    # 4. playwright importable
    try:
        import importlib

        importlib.import_module("playwright.sync_api")
        pw_ok, pw_detail = True, "importable"
    except ImportError as exc:
        pw_ok = False
        pw_detail = f"ImportError: {exc} — IR-KPI + IR-doc crons will fail"
    results.append(("playwright importable", pw_ok, pw_detail))

    return results


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the table; only use the exit code.",
    )
    args = parser.parse_args(argv)

    checks = check()
    failed = [(name, detail) for name, passed, detail in checks if not passed]

    if not args.quiet:
        print("\nEnvironment preflight\n")
        for name, passed, detail in checks:
            icon = "✓" if passed else "✗"
            print(f"  {icon}  {name}: {detail}")
        print()
        if failed:
            print(f"  {len(failed)} check(s) failed — resolve before running the pipeline.\n")
        else:
            print("  All checks passed.\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
