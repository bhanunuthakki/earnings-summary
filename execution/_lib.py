"""Small shared substrate for deterministic ``execution`` entrypoints.

New CLIs use this module instead of growing another project-root calculation,
database-path argument helper, or ad-hoc JSON logger. Existing scripts can migrate
mechanically without changing their business logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from db_paths import resolve_db_path  # noqa: E402
from log_redact import redact  # noqa: E402


def _safe_log_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {redact(str(key)): _safe_log_value(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        return [_safe_log_value(item) for item in sequence]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


def log_event(event: str, **fields: object) -> None:
    """Emit one redacted structured event to stderr."""
    payload: dict[str, object] = {"event": redact(event)}
    payload.update({redact(key): _safe_log_value(value) for key, value in fields.items()})
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)


def command_parser(description: str | None) -> argparse.ArgumentParser:
    """Create the shared execution CLI parser shell."""
    return argparse.ArgumentParser(description=description)


def add_database_argument(
    parser: argparse.ArgumentParser,
    *,
    flag: str = "--db-path",
    default: Path | None = None,
) -> None:
    """Attach a typed database path while leaving path policy with the command.

    The default is deliberately ``None``. Legacy commands that historically
    used a checkout-relative database must opt into that default explicitly;
    new commands should resolve an authorized database at their own boundary.
    """
    default_hint = f" (default: {default})" if default is not None else ""
    parser.add_argument(
        flag,
        type=Path,
        default=default,
        metavar="PATH",
        help=f"Path to the SQLite database{default_hint}",
    )


__all__ = [
    "PROJECT_ROOT",
    "SRC_ROOT",
    "add_database_argument",
    "command_parser",
    "log_event",
    "resolve_db_path",
]
