#!/usr/bin/env bash
set -euo pipefail
sqlite_dir="$RUNNER_TEMP/sqlite-3.53.4"
archive="$RUNNER_TEMP/sqlite-amalgamation-3530400.zip"
mkdir -p "$sqlite_dir"
curl --fail --location --proto '=https' --tlsv1.2 \
  https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip \
  --output "$archive"
python - "$archive" <<'PY'
import hashlib
import pathlib
import sys

expected = "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"  # pragma: allowlist secret
observed = hashlib.sha3_256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
if observed != expected:
    raise SystemExit(f"SQLite archive SHA3-256 mismatch: {observed}")
PY
unzip -q "$archive" -d "$sqlite_dir"
gcc -O2 -fPIC -shared -pthread -DSQLITE_ENABLE_FTS5 \
  "$sqlite_dir/sqlite-amalgamation-3530400/sqlite3.c" \
  -o "$sqlite_dir/libsqlite3.so.0" -lm -ldl
