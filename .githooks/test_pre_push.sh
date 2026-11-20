#!/bin/sh
set -eu

repo_root=$(git rev-parse --show-toplevel)
hook="$repo_root/.githooks/pre-push"
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/earnings-pre-push.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM
bin_dir="$tmp_dir/bin"
mkdir -p "$bin_dir"
log="$tmp_dir/python.log"

cat >"$bin_dir/git" <<'EOF'
#!/bin/sh
case "$*" in
  "rev-parse --show-toplevel") printf '%s\n' "$TEST_REPO_ROOT" ;;
  "rev-parse HEAD") printf '%s\n' checked-head ;;
  "merge-base HEAD origin/main") printf '%s\n' base ;;
  *"diff --name-only"*"instruction_tests/test_*.py"*) printf '%s\n' instruction_tests/test_instruction_contracts.py ;;
  *"diff --name-only"*) printf '%s\n' src/models/documents.py ;;
  show*) exit 0 ;;
  *) printf 'unexpected fake git call: %s\n' "$*" >&2; exit 2 ;;
esac
EOF
chmod +x "$bin_dir/git"

cat >"$bin_dir/fake-python" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$TEST_PYTHON_LOG"
if [ "${FAKE_FAIL_MODULE:-}" = "ruff" ] && [ "$*" = "-m ruff --version" ]; then exit 1; fi
if [ "${FAKE_FAIL_MODULE:-}" = "pyright" ] && echo "$*" | grep -q -- "--check types"; then exit 1; fi
exit 0
EOF
chmod +x "$bin_dir/fake-python"

run_hook() {
  TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" \
    PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$bin_dir/fake-python" \
    "$hook" >/dev/null 2>"$tmp_dir/stderr"
}

if printf '%s\n' 'refs/heads/other unchecked-head refs/heads/other old-head' | run_hook; then
  printf 'expected unchecked outgoing ref to fail\n' >&2
  exit 1
fi
grep -q 'checkout the commit being pushed' "$tmp_dir/stderr"
if printf '%s\n' \
  'refs/heads/main checked-head refs/heads/main old-head' \
  'refs/heads/other unchecked-head refs/heads/other old-head' | run_hook; then
  printf 'expected unchecked second outgoing ref to fail\n' >&2
  exit 1
fi
grep -q 'checkout the commit being pushed' "$tmp_dir/stderr"

if TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" \
  PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$tmp_dir/missing-python" \
  "$hook" >/dev/null 2>"$tmp_dir/stderr"; then
  printf 'expected missing explicit interpreter to fail\n' >&2
  exit 1
fi
grep -q 'PYTHON_BIN is not executable' "$tmp_dir/stderr"

if TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" FAKE_FAIL_MODULE=pyright \
  PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$bin_dir/fake-python" \
  "$hook" >/dev/null 2>"$tmp_dir/stderr"; then
  printf 'expected failed typecheck to block the push\n' >&2
  exit 1
fi
grep -q 'PRE-PUSH FAILED at:.*--check types' "$tmp_dir/stderr"

: >"$log"
printf '%s\n' 'refs/heads/main checked-head refs/heads/main old-head' | run_hook
grep -qx -- 'execution/verify_public_tree.py --pre-push base' "$log"
grep -qx -- 'execution/validate_directive_manifest.py' "$log"
grep -qx -- 'execution/validate_folder_contract.py' "$log"
if grep -q -- "--check tests\|--check full-tests" "$log"; then
  printf "unexpected test-suite execution in static pre-push hook\n" >&2
  exit 1
fi
grep -qx -- '-m quality.check_changed --base base --mode committed --check format --check lint --check types --check suppressions' "$log"

: >"$log"
run_hook
grep -qx -- 'execution/verify_public_tree.py --pre-push base' "$log"
grep -qx -- '-m quality.check_changed --base base --mode committed --check format --check lint --check types --check suppressions' "$log"

printf 'pre-push-tests: ok\n'
