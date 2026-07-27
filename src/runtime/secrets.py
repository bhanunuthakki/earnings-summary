"""Typed path policy for credentials and local environment configuration.

Secret bytes live outside the repository under the user's application-data
directory.  Legacy in-repository paths remain a read-only migration fallback:
once an external file exists it always wins, and new files are created only in
the external directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

SECRETS_DIR_ENV = "EARNINGS_SUMMARY_SECRETS_DIR"  # pragma: allowlist secret
ENV_FILE_ENV = "EARNINGS_SUMMARY_ENV_FILE"


def secrets_dir() -> Path:
    """Return the external credential directory without creating it."""
    configured = os.environ.get(SECRETS_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return (Path(appdata) / "earnings-summary" / "secrets").resolve()
    return (Path.home() / ".config" / "earnings-summary" / "secrets").resolve()


def _validate_name(name: str) -> None:
    if not name or Path(name).name != name:
        raise ValueError("secret name must be one basename")


def _exists_with_json_sibling(path: Path) -> bool:
    return path.exists() or (not path.suffix and path.with_suffix(".json").exists())


def secret_write_path(name: str) -> Path:
    """Return the external-only destination for new or refreshed secret bytes."""
    _validate_name(name)
    return secrets_dir() / name


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def write_private_text(path: Path, value: str) -> None:
    """Atomically replace an explicitly authorized private file."""
    destination = path.resolve()
    root = destination.parent
    _secure_directory(root)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=root)
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(destination)
        if os.name != "nt":
            destination.chmod(0o600)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_secret_text(path: Path, value: str) -> None:
    """Atomically replace a secret in the canonical external directory."""
    destination = path.resolve()
    if destination.parent != secrets_dir():
        raise ValueError("secret writes must target the external secrets directory")
    write_private_text(destination, value)


def create_secret_text(path: Path, value: str) -> bool:
    """Create an external secret once; return False if another process won."""
    destination = path.resolve()
    root = secrets_dir()
    if destination.parent != root:
        raise ValueError("secret writes must target the external secrets directory")
    _secure_directory(root)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=root)
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                os.rename(tmp_path, destination)
            else:
                os.link(tmp_path, destination)
                tmp_path.unlink()
        except FileExistsError:
            return False
        if os.name != "nt":
            destination.chmod(0o600)
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def secret_read_path(name: str, *, repo_root: Path | None = None) -> Path:
    """Resolve a secret for reading, with a read-only legacy migration fallback."""
    _validate_name(name)
    external = secret_write_path(name)
    if _exists_with_json_sibling(external) or repo_root is None:
        return external
    legacy = repo_root.resolve() / "data" / "secrets" / name
    return legacy if _exists_with_json_sibling(legacy) else external


def secret_file(name: str, *, repo_root: Path | None = None) -> Path:
    """Backward-compatible alias for read-only secret resolution."""
    return secret_read_path(name, repo_root=repo_root)


def project_env_file(repo_root: Path) -> Path:
    """Resolve the external project env file, with a legacy migration fallback."""
    configured = os.environ.get(ENV_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    external = secrets_dir() / ".env"
    if external.exists():
        return external
    legacy = repo_root.resolve() / ".env"
    return legacy if legacy.exists() else external


def load_project_env(repo_root: Path, *, override: bool = False) -> bool:
    """Load the resolved env file using python-dotenv's non-leaking API."""
    from dotenv import load_dotenv

    return bool(load_dotenv(project_env_file(repo_root), override=override))
