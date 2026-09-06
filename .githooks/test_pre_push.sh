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
  "rev-parse --local-env-vars")
    if [ "${FAKE_FAIL_LOCAL_ENV:-0}" = "1" ]; then
      exit 1
    fi
    printf '%s\n' GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_CONFIG GIT_OBJECT_DIRECTORY
    ;;
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
if [ "${FAKE_FAIL_MODULE:-}" = "pytest" ] && [ "$*" = "-m pytest --version" ]; then exit 1; fi
case "$*" in
  "-m pytest -q"*)
    if env | grep -Eq '^GIT_(DIR|WORK_TREE|INDEX_FILE|CONFIG|OBJECT_DIRECTORY)='; then
      printf 'pytest inherited repository-local Git environment\n' >&2
      exit 1
    fi
    ;;
esac
exit 0
EOF
chmod +x "$bin_dir/fake-python"

run_hook() {
  TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" \
    GIT_DIR="$tmp_dir/poison.git" GIT_WORK_TREE="$tmp_dir/poison-worktree" \
    GIT_INDEX_FILE="$tmp_dir/poison.index" GIT_CONFIG="$tmp_dir/poison.config" \
    GIT_OBJECT_DIRECTORY="$tmp_dir/poison-objects" \
    PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$bin_dir/fake-python" \
    "$hook" >/dev/null 2>"$tmp_dir/stderr"
}

if TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" \
  PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$tmp_dir/missing-python" \
  "$hook" >/dev/null 2>"$tmp_dir/stderr"; then
  printf 'expected missing explicit interpreter to fail\n' >&2
  exit 1
fi
grep -q 'PYTHON_BIN is not executable' "$tmp_dir/stderr"

if TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" FAKE_FAIL_MODULE=pytest \
  PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$bin_dir/fake-python" \
  "$hook" >/dev/null 2>"$tmp_dir/stderr"; then
  printf 'expected missing pytest module to fail\n' >&2
  exit 1
fi
grep -q 'PRE-PUSH FAILED at:.*-m pytest --version' "$tmp_dir/stderr"

if TEST_REPO_ROOT="$repo_root" TEST_PYTHON_LOG="$log" FAKE_FAIL_LOCAL_ENV=1 \
  PATH="$bin_dir:/usr/bin:/bin" PYTHON_BIN="$bin_dir/fake-python" \
  "$hook" >/dev/null 2>"$tmp_dir/stderr"; then
  printf 'expected local Git environment discovery failure to fail closed\n' >&2
  exit 1
fi
grep -q 'PRE-PUSH FAILED: unable to resolve repository-local Git environment variables' "$tmp_dir/stderr"

: >"$log"
FAST_PUSH=1 run_hook
grep -qx -- 'execution/verify_public_tree.py' "$log"
grep -qx -- 'execution/validate_directive_manifest.py' "$log"
grep -qx -- 'execution/validate_folder_contract.py' "$log"
grep -q -- '-m pytest -q instruction_tests/test_instruction_contracts.py' "$log"
unset FAST_PUSH

: >"$log"
run_hook
grep -qx -- 'execution/verify_public_tree.py' "$log"
grep -qx -- '-m pytest -q' "$log"

printf 'pre-push-tests: ok\n'
