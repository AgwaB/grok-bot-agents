#!/bin/sh
# Runs ON THE USER'S COMPUTER (macOS/Linux) through one machine Shell call, after the user chose
# "import everything". Bundles only the import_plan.py allowlist for the given CLIs into ONE
# dereferenced tarball (symlinked skills become real files), so a single CopyToBox moves it all.
#   sh -s -- codex pi omp <<'BUNDLE'
#   ...this file...
#   BUNDLE
# Never prints file contents: only the bundle path, size, member count and top-level entry names.
# The tarball holds login tokens: it is 600, and must be deleted on the computer right after CopyToBox.
umask 077
[ $# -gt 0 ] || { echo '{"ok":false,"error":"usage: bundle_local.sh <cli>..."}'; exit 2; }
cd "$HOME" || exit 2
out="$HOME/agent-import.tgz"
list=$(mktemp 2>/dev/null || echo "/tmp/agent-import-list.$$")
entries=""
add() { [ -e "$1" ] || [ -L "$1" ] || return 0; printf '%s\n' "$1" >>"$list"; entries="$entries${entries:+,}\"$1\""; }
for c in "$@"; do
  case "$c" in
    codex)  for f in auth.json config.toml AGENTS.md prompts skills; do add ".codex/$f"; done ;;
    claude) for f in .credentials.json settings.json CLAUDE.md agents commands skills; do add ".claude/$f"; done
            add ".claude.json" ;;
    pi)     for f in auth.json settings.json models.json anthropic-auth.json AGENTS.md prompts skills extensions; do add ".pi/agent/$f"; done ;;
    omp)    for f in agent.db agent.db-wal agent.db-shm config.yml models.yml AGENTS.md mcp.json; do add ".omp/agent/$f"; done ;;
    *) echo "{\"ok\":false,\"error\":\"unknown cli: $c\"}"; rm -f "$list"; exit 2 ;;
  esac
done
if [ ! -s "$list" ]; then rm -f "$list"; echo '{"ok":false,"error":"nothing to bundle"}'; exit 3; fi
rm -f "$out"
COPYFILE_DISABLE=1 tar czhf "$out" --exclude='._*' --exclude='.DS_Store' --exclude='*.pyc' \
  --exclude='__pycache__' --exclude='node_modules' --exclude='.git' -T "$list" 2>/dev/null
rc=$?
rm -f "$list"
[ -f "$out" ] || { echo "{\"ok\":false,\"error\":\"tar failed (rc $rc)\"}"; exit 1; }
chmod 600 "$out"
bytes=$(wc -c <"$out" | tr -d ' ')
members=$(tar tzf "$out" 2>/dev/null | wc -l | tr -d ' ')
# rc != 0 with a bundle usually means dangling symlinks were skipped.
printf '{"ok":true,"bundle":"%s","bytes":%s,"members":%s,"tar_rc":%s,"entries":[%s]}\n' "$out" "$bytes" "$members" "$rc" "$entries"
