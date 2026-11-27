"""Run detect-secrets over every tracked file, skipping exact files proven clean.

The detect-secrets hook result for one file is a pure function of the file
path, its bytes, the hook arguments below, the filter module, and the
detect-secrets version. A clean result for an unchanged (path, blob) pair
therefore needs no rescan. The cache stores only those clean pairs, salted
with every input above; a salt change discards the whole cache.

Every tracked file is either already in the trusted clean set or scanned now.
Any finding, scanner error, or unreadable cache fails closed. Only trusted
`main` push runs save the cache (see the security job in ci.yml), so a pull
request cannot add entries that another run trusts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

SCHEMA = "secret-scan-clean-blobs/v3"
FILTER_FILE = Path(".github/scripts/quality_hash_filter.py")
# Canonical evidence hash fields are generated metadata. Keep scanning every
# other field and file with the unchanged detector set.
EXCLUDE_FILES = (
    r"^(?:data[\\/]fmp_canary_manifest\.json|evals[\\/]receipts[\\/].*|docs[\\/]hardening[\\/].*"
    r"|config[\\/]filing_xbrl_processor_(?:approval|bundle)\.json"
    r"|docs[\\/]design[\\/]deletion_catalog_2026_08\.json"
    r"|evals[\\/]golden[\\/]metric_lifecycle_triage\.json"
    r"|tests[\\/](?:test_etf_instrument_mvp|test_gemini_backend|test_peer_selection"
    r"|test_validate_environment)\.py)$"
)
HOOK_ARGUMENTS = (
    "--filter",
    f"file://{FILTER_FILE.as_posix()}::is_quality_evidence_hash",
    "--exclude-files",
    EXCLUDE_FILES,
)
BATCH_SIZE = 32


def tracked_blobs(root: Path) -> list[tuple[str, str]]:
    """Return (path, blob id) for every tracked file in the index."""
    output = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    entries: list[tuple[str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        _mode, blob, _stage = meta.split(b" ")
        entries.append((path.decode("utf-8", "surrogateescape"), blob.decode("ascii")))
    return entries


def entry_key(path: str, blob: str) -> str:
    return hashlib.sha256(f"{path}\0{blob}".encode("utf-8", "surrogateescape")).hexdigest()


def cache_salt(root: Path, detector_version: str) -> str:
    digest = hashlib.sha256()
    for part in (
        SCHEMA,
        detector_version,
        json.dumps(HOOK_ARGUMENTS),
        Path(__file__).read_bytes().hex(),
        (root / FILTER_FILE).read_bytes().hex(),
    ):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def load_clean(cache: Path, salt: str) -> set[str]:
    """Return trusted clean keys; a missing or differently salted cache is empty."""
    if not cache.is_file():
        return set()
    payload: object = json.loads(cache.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return set()
    fields = cast("dict[str, object]", payload)
    if fields.get("salt") != salt:
        return set()
    keys = fields.get("clean")
    if not isinstance(keys, list):
        raise ValueError(f"{cache}: clean must be a list of strings")
    items = cast("list[object]", keys)
    clean = [key for key in items if isinstance(key, str)]
    if len(clean) != len(items):
        raise ValueError(f"{cache}: clean must be a list of strings")
    return set(clean)


def _batches(root: Path, paths: Sequence[str]) -> list[list[str]]:
    # Largest files first, dealt round-robin, so one huge file never queues
    # behind a full batch of others on the same worker.
    def size(path: str) -> int:
        try:
            return (root / path).lstat().st_size
        except OSError:
            return 0

    ordered = sorted(paths, key=lambda path: (-size(path), path))
    count = max(1, -(-len(ordered) // BATCH_SIZE))
    return [ordered[index::count] for index in range(count)]


def scan(
    root: Path,
    cache: Path,
    *,
    detector_version: str,
    jobs: int,
    write_cache: bool,
) -> int:
    salt = cache_salt(root, detector_version)
    clean = load_clean(cache, salt)
    entries = tracked_blobs(root)
    keys = {path: entry_key(path, blob) for path, blob in entries}
    pending = [path for path, _blob in entries if keys[path] not in clean]
    print(f"secret-scan: {len(entries)} tracked files, {len(entries) - len(pending)} cached clean")

    def run(batch: list[str]) -> tuple[list[str], subprocess.CompletedProcess[str]]:
        result = subprocess.run(
            ["detect-secrets-hook", *HOOK_ARGUMENTS, "--", *batch],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        return batch, result

    failed = False
    newly_clean: set[str] = set()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for batch, result in pool.map(run, _batches(root, pending)):
            if result.returncode == 0:
                newly_clean.update(keys[path] for path in batch)
                continue
            failed = True
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
    print(f"secret-scan: scanned {len(pending)} files, failed={failed}")
    if write_cache and not failed:
        current = set(keys.values())
        trusted = sorted((clean & current) | newly_clean)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"salt": salt, "clean": trusted}) + "\n", encoding="utf-8")
    return 1 if failed else 0


def _detector_version() -> str:
    return importlib.metadata.version("detect-secrets")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--write-cache", action="store_true")
    args = parser.parse_args(argv)
    root = Path.cwd()
    return scan(
        root,
        args.cache,
        detector_version=_detector_version(),
        jobs=args.jobs,
        write_cache=args.write_cache,
    )


if __name__ == "__main__":
    raise SystemExit(main())
