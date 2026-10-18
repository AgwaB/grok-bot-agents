#!/usr/bin/env bash
# Offline entry point: no inherited auth/config variables, real CLIs, or installers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE=${MATRIX_NODE:-$(command -v node || true)}
PYTHON=$(command -v python3)
SC=$(command -v shellcheck || true)
PWSH=${PWSH:-$(command -v pwsh || true)}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export HOME="$T/home" TMPDIR="$T"
mkdir -p "$HOME/.local/bin"
if [ -z "$NODE" ] || [ -z "$SC" ]; then
  echo 'Required: Python 3.11+, node and shellcheck (nothing is installed by this runner).'
  exit 2
fi
ln -s "$NODE" "$HOME/.local/bin/node"
CLEAN=(env -i HOME="$HOME" USERPROFILE="$HOME" TMPDIR="$T" LANG=C.UTF-8
       PATH="$HOME/.local/bin:/usr/bin:/bin" MATRIX_NODE="$NODE" PWSH="$PWSH"
       PYTHONDONTWRITEBYTECODE=1)
cd "$ROOT"
"${CLEAN[@]}" "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
# Detect/bundle search these conventional global bins too. Refuse rather than invoke a real CLI.
for cli in claude codex pi omp; do
  # shellcheck disable=SC2016 # $1 belongs to the isolated child shell.
  if env -i HOME="$HOME" PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin sh -c 'command -v "$1" >/dev/null' sh "$cli"; then
    echo "Refusing ambient $cli: use an isolated checkout/container without system-wide agent CLIs."
    exit 2
  fi
done
status=0
run_test() {
  local name=$1 rc=0; shift
  "$@" >"$T/$name.log" 2>&1 || rc=$?
  # A failed assertion or timeout must not spill fixture data through a traceback.
  "${CLEAN[@]}" "$PYTHON" - "$T/$name.log" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text(errors="replace")
if "canary-" in text or "CANARY" in text:
    print("Test diagnostics suppressed: canary marker present")
    sys.exit(1)
print(text, end="")
PY
  if [ "$rc" != 0 ]; then status=1; fi
}
run_test matrix "${CLEAN[@]}" "$PYTHON" tests/import_matrix_test.py -v
run_test auth "${CLEAN[@]}" bash tests/auth_import_test.sh
# Static checks follow; a failure must make the combined runner fail too.
for f in skills/*/scripts/*.sh tests/*.sh; do "${CLEAN[@]}" bash -n "$f"; done
echo 'PASS bash -n'
"${CLEAN[@]}" "$SC" -s sh skills/import-agents/scripts/{detect,bundle}_local.sh skills/import-agents/scripts/export_secrets.sh
"${CLEAN[@]}" "$SC" -s bash skills/import-agents/scripts/apply_import.sh tests/{auth_import_test,run_import_tests}.sh
"${CLEAN[@]}" "$SC" -s sh tests/fake_home.sh
echo 'PASS shellcheck'
"${CLEAN[@]}" "$PYTHON" -X "pycache_prefix=$T/pycache" -m py_compile skills/*/scripts/*.py tests/*.py
echo 'PASS python3 -m py_compile'
"${CLEAN[@]}" "$PYTHON" skills/import-agents/scripts/import_plan.py --lint-scripts
"${CLEAN[@]}" "$PYTHON" -m json.tool .cursor-plugin/plugin.json >/dev/null
if [ -n "$PWSH" ]; then
  # shellcheck disable=SC2016 # PowerShell, not Bash, expands $null.
  "${CLEAN[@]}" "$PWSH" -NoProfile -NonInteractive -Command '$null = [scriptblock]::Create((Get-Content -Raw skills/import-agents/scripts/detect_local.ps1))'
  echo 'PASS PowerShell parse'
else
  echo 'SKIP PowerShell parse (pwsh unavailable)'
fi
exit "$status"
