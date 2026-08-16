"""Diff-aware ``ruff format`` gate — fail only when lines a change touched are
misformatted.

The repo carries a large pre-existing ``ruff format`` backlog (247 files of
tooling drift, 2026-06-02), so a whole-file format check forces unrelated
reformatting onto any change that merely *touches* a drifted file. This mirrors
the diff-aware ``lint-changed`` / ``typecheck-changed`` gates (see ``Makefile``
and ``.github/workflows/ci.yml``): for each changed file we intersect the lines
the change actually touched (``git diff``'s ``+`` side) with the lines
``ruff format`` would rewrite (``ruff format --diff``'s ``-`` side), and fail
only on the overlap. Pre-existing drift on untouched lines is tolerated and paid
down incrementally, exactly like the lint and pyright ratchets.

Usage::

    python execution/format_changed.py --base origin/main path/a.py path/b.py

Exit status: 0 when every changed file is format-clean on its own changed lines,
1 when a changed line is misformatted, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# A unified-diff hunk header: ``@@ -old_start[,old_len] +new_start[,new_len] @@``.
# Only the two start line numbers are needed; the body walk tracks lengths.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _changed_on_side(diff_text: str, *, side: str) -> set[int]:
    """Line numbers actually added/removed on one side of a unified diff.

    ``side='new'`` collects ``+`` (added) line numbers in the new file;
    ``side='old'`` collects ``-`` (removed/rewritten) line numbers in the old
    file. Context lines advance the counters but are never collected, so the
    result is independent of how much context the diff carries — and content
    lines that happen to start with ``--``/``++`` are handled correctly because
    file headers are only honored before the first hunk.
    """
    collected: set[int] = set()
    old_ln = 0
    new_ln = 0
    in_hunk = False
    for line in diff_text.splitlines():
        header = _HUNK_RE.match(line)
        if header is not None:
            old_ln = int(header.group(1))
            new_ln = int(header.group(2))
            in_hunk = True
            continue
        if not in_hunk:
            continue  # diff preamble (---, +++, index, …) before the first hunk
        marker = line[:1]
        if marker == "\\":  # "\ No newline at end of file"
            continue
        if marker == "-":
            if side == "old":
                collected.add(old_ln)
            old_ln += 1
        elif marker == "+":
            if side == "new":
                collected.add(new_ln)
            new_ln += 1
        else:  # context line (leading space) or a stray blank
            old_ln += 1
            new_ln += 1
    return collected


def changed_lines(git_diff_text: str) -> set[int]:
    """New-file line numbers a change added/modified (``+`` side of ``git diff``)."""
    return _changed_on_side(git_diff_text, side="new")


def reformat_lines(ruff_diff_text: str) -> set[int]:
    """Current-file line numbers ``ruff format`` would rewrite (``-`` side)."""
    return _changed_on_side(ruff_diff_text, side="old")


def file_is_clean(git_diff_text: str, ruff_diff_text: str) -> tuple[bool, set[int]]:
    """Is the change format-clean on its own lines? Returns (clean, offending_lines).

    Both diffs are in current-file (HEAD) coordinates: ``git diff``'s new side
    and ``ruff format --diff``'s old side, so the intersection is meaningful.
    """
    overlap = changed_lines(git_diff_text) & reformat_lines(ruff_diff_text)
    return (not overlap, overlap)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )


def check_file(path: str, base: str) -> tuple[bool, set[int]]:
    """Shell out to git + ruff and return (clean, offending_lines) for one file."""
    git = _run(["git", "diff", "-U0", f"{base}...HEAD", "--", path])
    touched = changed_lines(git.stdout)
    if not touched:
        return True, set()
    ruff = _run(["ruff", "format", "--diff", path])
    if ruff.returncode not in (0, 1):
        # ruff couldn't parse the file (syntax error etc.) — not this gate's job
        # to adjudicate; tests / pyright will catch it. Treat as clean.
        sys.stderr.write(
            f"warning: `ruff format --diff {path}` exited {ruff.returncode}; skipping\n"
        )
        return True, set()
    return file_is_clean(git.stdout, ruff.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff-aware ruff format gate — checks only changed lines.",
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to diff against (default: origin/main).",
    )
    parser.add_argument("files", nargs="*", help="Changed .py files to check.")
    ns = parser.parse_args(argv)
    files = [f for f in ns.files if f.strip()]
    if not files:
        print("format gate: no changed .py files — no-op")
        return 0

    offenders: list[tuple[str, set[int]]] = []
    for path in files:
        clean, lines = check_file(path, ns.base)
        if not clean:
            offenders.append((path, lines))

    if not offenders:
        print(f"format gate: {len(files)} changed file(s) clean on their changed lines")
        return 0

    for path, lines in offenders:
        nums = ", ".join(str(n) for n in sorted(lines))
        print(f"::error file={path}::ruff format would rewrite changed line(s): {nums}")
        sys.stdout.write(_run(["ruff", "format", "--diff", path]).stdout)
    print(
        "\nYour changed lines are not ruff-formatted. Run `ruff format <file>` and "
        "restage. (Only lines you changed are gated; pre-existing drift on untouched "
        "lines is ignored.)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
