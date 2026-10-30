"""Current-chain contract for the post-earnings readout budget."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config
