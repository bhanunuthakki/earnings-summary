"""Fail closed when private or generated material enters the public Git tree."""

from __future__ import annotations

import os
import re
import subprocess
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path

FORBIDDEN_PATH_CATEGORIES = (
    ("local-state", re.compile(r"^\.harden/state\.json$")),
    ("self-hosting", re.compile(r"^directives/self_host.*\.md$")),
    (
        "database",
        re.compile(
            r"(?:^|/)(?:portfolio|runtime|local_state|state)\.(?:db|sqlite|sqlite3)$",
            re.IGNORECASE,
        ),
    ),
    (
        "credentials",
        re.compile(r"(?:^|/)(?:\.env|credentials\.json|token\.json)$", re.IGNORECASE),
    ),
    (
        "private-data",
        re.compile(r"^private/", re.IGNORECASE),
    ),
    (
        "private-markdown",
        re.compile(r"(?:^|/)[^/]*(?:private|personal)[^/]*\.md$", re.IGNORECASE),
    ),
)
HOME_PATH = re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)")
OWNER_HOME_PATH = re.compile(r"(?:/Users/bhanu|/home/bhanu|[A-Za-z]:\\Users\\bhanu)", re.IGNORECASE)
PERSONAL_EMAIL = re.compile(
    r"\b(?:bhanu|nuthakki)[^@\s]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)
ACCOUNT_LEVEL_DETAIL = re.compile(
    r"\b(?:cost[\s_-]*basis|account[\s_-]*balance)"
    r"\s*[\"']?\s*[:=]\s*[\"']?[$€£]?[-+]?\d"
    r"|\bposition[\s_-]*(?:value|size)\s*[\"']?\s*[:=]\s*[\"']?"
    r"(?:[$€£]\s*[-+]?\d|[-+]?\d(?:[\d,.]*\d)?\s*(?:USD|EUR|GBP)\b)"
    r"|\b(?:quantity|shares?|share[\s_-]*quantity)\s*[\"']?\s*[:=]"
    r"\s*[\"']?[-+]?\d"
    r"|\baccount[\s_-]*(?:id|number|no\.?)\s*[\"']?\s*[:=]"
    r"\s*[\"']?[A-Za-z0-9*_-]+",
    re.IGNORECASE,
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
HIGH_CONFIDENCE_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)"
    r"\s*[=:]\s*(?:[\"'`](?P<quoted>[^\"'`$<{\s]{12,})[\"'`]"
    r"|(?P<bare>[A-Za-z0-9_./+=-]{12,}))"
)
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
SYNTHETIC_MARKER = re.compile(r"\b(?:dummy|example|fake|fixture|placeholder|redacted|test)\b", re.I)
ACCOUNT_DETAIL_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".md",
    ".ndjson",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
UNSCANNABLE_PRIVATE_SUFFIXES = {".db", ".docx", ".pdf", ".sqlite", ".sqlite3", ".zip"}
CODE_SUFFIXES = {".js", ".py", ".sh", ".ts"}
MAX_WORKBOOK_ENTRIES = 10_000
MAX_WORKBOOK_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _clean_git_env() -> dict[str, str]:
    """Keep callers' hook-scoped Git variables from overriding an explicit repo."""

    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def is_exempt_account_document(relative: str) -> bool:
    """Keep only synthetic test fixtures out of account-data scanning."""
    return relative.startswith(("tests/", "instruction_tests/"))


def tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )
    return [item for item in result.stdout.decode().split("\0") if item]


def forbidden_path_category(relative: str) -> str | None:
    for category, pattern in FORBIDDEN_PATH_CATEGORIES:
        if pattern.search(relative):
            return category
    return None


def _workbook_text(data: bytes) -> tuple[str | None, str | None]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as workbook:
            entries = workbook.infolist()
            total_size = sum(entry.file_size for entry in entries)
            if len(entries) > MAX_WORKBOOK_ENTRIES or total_size > MAX_WORKBOOK_UNCOMPRESSED_BYTES:
                return None, "unscannable-workbook"
            parts: list[str] = []
            for entry in entries:
                if not entry.filename.lower().endswith((".xml", ".rels", ".txt", ".json", ".csv")):
                    continue
                parts.append(workbook.read(entry).decode("utf-8", errors="ignore"))
            return "\n".join(parts), None
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None, "unscannable-workbook"


def _text_for_scan(relative: str, data: bytes) -> tuple[str | None, str | None]:
    suffix = Path(relative).suffix.lower()
    if suffix == ".xlsx":
        return _workbook_text(data)
    if suffix in UNSCANNABLE_PRIVATE_SUFFIXES:
        return None, "unscannable-private-artifact"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, None


def content_violation_categories(relative: str, data: bytes) -> set[str]:
    if relative == "execution/verify_public_tree.py":
        return set()
    text, decoding_failure = _text_for_scan(relative, data)
    if decoding_failure is not None:
        return {decoding_failure}
    if text is None:
        return set()

    categories: set[str] = set()
    is_fixture = relative.startswith(("tests/", "instruction_tests/"))
    if OWNER_HOME_PATH.search(text) or (not is_fixture and HOME_PATH.search(text)):
        categories.add("personal-home-path")
    if PERSONAL_EMAIL.search(text) or (not is_fixture and SSN.search(text)):
        categories.add("personal-identifier")
    for line in text.splitlines():
        if PRIVATE_KEY.search(line) or HIGH_CONFIDENCE_SECRET.search(line):
            categories.add("credential-material")
            break
        credential = CREDENTIAL_ASSIGNMENT.search(line)
        value = credential and (credential.group("quoted") or credential.group("bare"))
        is_bare_code_value = (
            credential is not None
            and credential.group("bare") is not None
            and Path(relative).suffix.lower() in CODE_SUFFIXES
        )
        if value and not SYNTHETIC_MARKER.search(value) and not is_bare_code_value:
            categories.add("credential-material")
            break
    if (
        (Path(relative).suffix.lower() in ACCOUNT_DETAIL_SUFFIXES or relative.endswith(".xlsx"))
        and not is_exempt_account_document(relative)
        and ACCOUNT_LEVEL_DETAIL.search(text)
    ):
        categories.add("account-level-fact")
    return categories


def _public_refs(repo_root: Path) -> list[str]:
    def list_refs(prefix: str) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "for-each-ref", "--format=%(refname)", prefix],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_git_env(),
        )
        return sorted(line for line in result.stdout.splitlines() if line)

    remote_refs = [
        ref for ref in list_refs("refs/remotes/origin") if ref != "refs/remotes/origin/HEAD"
    ]
    return remote_refs or list_refs("refs/heads")


def audit_public_refs(repo_root: Path) -> dict[str, dict[str, int]]:
    """Summarize private path/content categories across fetched public branch refs."""

    file_counts: Counter[str] = Counter()
    ref_counts: Counter[str] = Counter()
    ref_entries: dict[str, list[tuple[str, str]]] = {}
    blob_oids: set[str] = set()
    for ref in _public_refs(repo_root):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-r", "-z", ref],
            check=True,
            capture_output=True,
            env=_clean_git_env(),
        )
        entries: list[tuple[str, str]] = []
        for raw_entry in result.stdout.split(b"\0"):
            if not raw_entry:
                continue
            metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
            _mode, object_type, raw_oid = metadata.split(b" ", maxsplit=2)
            if object_type != b"blob":
                continue
            path = raw_path.decode(errors="surrogateescape")
            oid = raw_oid.decode("ascii")
            entries.append((path, oid))
            blob_oids.add(oid)
        ref_entries[ref] = entries

    blobs = _read_blobs(repo_root, blob_oids)
    classification_cache: dict[tuple[str, str], frozenset[str]] = {}
    for _ref, entries in ref_entries.items():
        categories: set[str] = set()
        for relative, oid in entries:
            cache_key = (relative, oid)
            file_categories = classification_cache.get(cache_key)
            if file_categories is None:
                detected_categories = content_violation_categories(relative, blobs[oid])
                path_category = forbidden_path_category(relative)
                if path_category is not None:
                    detected_categories.add(path_category)
                file_categories = frozenset(detected_categories)
                classification_cache[cache_key] = file_categories
            for category in file_categories:
                file_counts[category] += 1
                categories.add(category)
        ref_counts.update(categories)
    return {
        category: {"files": file_counts[category], "refs": ref_counts[category]}
        for category in sorted(file_counts)
    }


def _read_blobs(repo_root: Path, object_ids: set[str]) -> dict[str, bytes]:
    ordered_ids = sorted(object_ids)
    if not ordered_ids:
        return {}
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=("\n".join(ordered_ids) + "\n").encode("ascii"),
        check=True,
        capture_output=True,
        env=_clean_git_env(),
    )
    blobs: dict[str, bytes] = {}
    offset = 0
    for expected_oid in ordered_ids:
        header_end = result.stdout.index(b"\n", offset)
        header = result.stdout[offset:header_end].decode("ascii")
        oid, object_type, raw_size = header.split(" ")
        if oid != expected_oid or object_type != "blob":
            raise RuntimeError(f"unexpected git object while auditing {expected_oid}")
        size = int(raw_size)
        content_start = header_end + 1
        content_end = content_start + size
        blobs[oid] = result.stdout[content_start:content_end]
        offset = content_end + 1
    return blobs


def verify(repo_root: Path) -> list[str]:
    violations: list[str] = []
    for relative in tracked_files(repo_root):
        path = Path(relative)
        if forbidden_path_category(relative) is not None:
            violations.append(f"forbidden tracked path: {relative}")
            continue
        if relative == "execution/verify_public_tree.py":
            continue
        try:
            data = (repo_root / path).read_bytes()
        except IsADirectoryError:
            continue
        for category in sorted(content_violation_categories(relative, data)):
            violations.append(f"{category} in: {relative}")
    return violations


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-refs",
        action="store_true",
        help="audit fetched origin branches by path category without printing paths",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    if args.all_refs:
        summary = audit_public_refs(repo_root)
        if summary:
            print("Public-ref path audit failed:")
            for category, counts in summary.items():
                print(f"- {category}: refs={counts['refs']} files={counts['files']}")
            return 1
        print("Public-ref path audit passed.")
        return 0
    violations = verify(repo_root)
    if violations:
        print("Public-tree verification failed:")
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print("Public-tree verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
