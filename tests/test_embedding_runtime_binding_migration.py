from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR = "0248_native_processing_closure_adapters"
HEAD = "0249_embedding_runtime_artifact_binding"
SHA = "a" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_runtime_binding_preserves_history_and_guards_new_rows(tmp_path: Path) -> None:
    path = tmp_path / "runtime-binding.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL
        );
        CREATE TABLE search_embedding_artifacts (
            embedding_artifact_id TEXT PRIMARY KEY,
            index_run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE search_projection_seals (
            projection_seal_id TEXT PRIMARY KEY,
            index_run_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            dimensions INTEGER
        );
        INSERT INTO search_embedding_model_promotions
        VALUES ('legacy-promotion','fastembed','model',2);
        INSERT INTO search_embedding_artifacts
        VALUES ('legacy-artifact','legacy-run','fastembed','model',2,'succeeded');
        INSERT INTO search_projection_seals
        VALUES ('legacy-seal','legacy-run','vector','fastembed','model',2);
        """
    )
    conn.commit()
    conn.close()

    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT runtime_artifact_sha256 FROM search_embedding_model_promotions "
            "WHERE promotion_id='legacy-promotion'"
        ).fetchone() == (None,)
        with pytest.raises(sqlite3.IntegrityError, match="runtime artifact"):
            conn.execute(
                "INSERT INTO search_embedding_model_promotions "
                "(promotion_id,provider,model,dimensions) "
                "VALUES ('unbound','fastembed','model',2)"
            )
        conn.execute(
            "INSERT INTO search_embedding_model_promotions "
            "(promotion_id,provider,model,dimensions,runtime_artifact_json,"
            "runtime_artifact_sha256) VALUES ('bound','fastembed','model',2,'{}',?)",
            (SHA,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="promoted runtime"):
            conn.execute(
                "INSERT INTO search_embedding_artifacts "
                "(embedding_artifact_id,index_run_id,provider,model,dimensions,outcome,"
                "runtime_artifact_sha256) VALUES "
                "('wrong','run','fastembed','model',2,'succeeded',?)",
                ("b" * 64,),
            )
        conn.execute(
            "INSERT INTO search_embedding_artifacts "
            "(embedding_artifact_id,index_run_id,provider,model,dimensions,outcome,"
            "runtime_artifact_sha256) VALUES "
            "('bound-artifact','run','fastembed','model',2,'succeeded',?)",
            (SHA,),
        )
        conn.execute(
            "INSERT INTO search_embedding_artifacts "
            "(embedding_artifact_id,index_run_id,provider,model,dimensions,outcome,"
            "runtime_artifact_sha256) VALUES "
            "('bound-with-legacy','legacy-run','fastembed','model',2,'succeeded',?)",
            (SHA,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="one promoted"):
            conn.execute(
                "INSERT INTO search_projection_seals "
                "(projection_seal_id,index_run_id,index_kind,provider,model,dimensions,"
                "runtime_artifact_sha256) VALUES "
                "('mixed-seal','legacy-run','vector','fastembed','model',2,?)",
                (SHA,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE search_embedding_artifacts SET runtime_artifact_sha256=? "
                "WHERE embedding_artifact_id='bound-artifact'",
                ("b" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="one promoted"):
            conn.execute(
                "INSERT INTO search_projection_seals "
                "(projection_seal_id,index_run_id,index_kind,provider,model,dimensions,"
                "runtime_artifact_sha256) VALUES "
                "('empty-seal','empty-run','vector','fastembed','model',2,?)",
                (SHA,),
            )
        conn.execute(
            "INSERT INTO search_projection_seals "
            "(projection_seal_id,index_run_id,index_kind,provider,model,dimensions,"
            "runtime_artifact_sha256) VALUES "
            "('bound-seal','run','vector','fastembed','model',2,?)",
            (SHA,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE search_projection_seals SET runtime_artifact_sha256=? "
                "WHERE projection_seal_id='bound-seal'",
                ("b" * 64,),
            )
    finally:
        conn.close()

    command.downgrade(config, PRIOR)
    conn = sqlite3.connect(path)
    try:
        assert "runtime_artifact_sha256" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(search_projection_seals)")
        }
    finally:
        conn.close()
