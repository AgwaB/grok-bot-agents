#!/bin/sh
# Runs ON THE USER'S COMPUTER (macOS/Linux) through one machine Shell call, after the user chose
# "import everything". Bundles only the import_plan.py allowlist for the given CLIs into ONE
# dereferenced tarball (symlinked skills become real files), so a single CopyToBox moves it all.
#   sh -s -- codex pi omp <<'BUNDLE'
#   ...this file...
#   BUNDLE
# Pi: also the packages settings.json `packages` points at. Local ones (git-tracked files only for a repo, otherwise
# everything but node_modules/.venv/.git/.worktrees/.tmp), and the files of the npm ones Pi installed (no nested
# node_modules), so local patches survive. Needs `node` to read settings.json (Pi itself needs it).
# A manifest (.agent-import-manifest.txt, in the bundle only) records package roots outside ~/.pi/agent, each package's
# Python venv (python version + `pip list --format=freeze`, lines with URLs dropped), `uv tool` pins (MCP server
# binaries) and CLI versions. No secrets go into it.
# Dirs are expanded with `find -L` (no node_modules/.git/.venv/__pycache__); dangling links are left out.
# Never prints file contents: only the bundle path, size, member count and top-level entry names.
# The tarball holds login tokens: it is 600, and must be deleted on the computer right after CopyToBox.
umask 077
[ $# -gt 0 ] || { echo '{"ok":false,"error":"usage: bundle_local.sh <cli>..."}'; exit 2; }
PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
cd "$HOME" || exit 2
hp=$(pwd -P)
out="$HOME/agent-import.tgz"
man=".agent-import-manifest.txt"
list=$(mktemp 2>/dev/null || echo "/tmp/agent-import-list.$$")
: >"$man"
entries=""; notes=""; npk=0
add() { # a file, or every file under a dir (symlinked dirs followed, so linked skills become real files)
  [ -e "$1" ] || return 0
  if [ -d "$1" ]; then
    find -L "$1" \( -name node_modules -o -name .git -o -name .venv -o -name __pycache__ -o -name .DS_Store -o -name '._*' \) \
      -prune -o -type f -print >>"$list" 2>/dev/null
  else printf '%s\n' "$1" >>"$list"; fi
  entries="$entries${entries:+,}\"$1\""
}
note() { notes="$notes${notes:+,}\"$(printf '%s' "$1" | tr -d '\042\134')\""; }
tree() { # files of one package dir (home-relative)
  if [ -e "$1/.git" ] && git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$1" ls-files | while IFS= read -r f; do [ -f "$1/$f" ] && printf '%s/%s\n' "$1" "$f"; done >>"$list"
  else
    find "$1" \( -name node_modules -o -name .venv -o -name venv -o -name .git -o -name .worktrees -o -name .tmp \
      -o -name __pycache__ -o -name .DS_Store \) -prune -o -type f -print >>"$list"
  fi
}
ver() { # first line of `<cli> --version`, killed after ~10 s
  t=$(mktemp 2>/dev/null || echo "/tmp/bundle.$$") || return
  ("$1" --version >"$t" 2>&1 </dev/null) &
  p=$!; i=0
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.2; i=$((i + 1)); done
  kill "$p" 2>/dev/null
  head -n 1 "$t" | cut -c1-80; rm -f "$t"
}
pi_packages() {
  s=.pi/agent/settings.json
  [ -f "$s" ] || return 0
  command -v node >/dev/null 2>&1 || { note "node not found: pi packages not bundled"; return 0; }
  node -e 'const s=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));for(const e of s.packages||[]){const v=typeof e==="string"?e:(e&&(e.source||e.path))||"";if(v)console.log(v)}' "$s" 2>/dev/null |
  while IFS= read -r src; do
    case "$src" in
      npm:*) n=${src#npm:}; case "$n" in @*/*@*) n=${n%@*} ;; @*) ;; *@*) n=${n%@*} ;; esac
             d=".pi/agent/npm/node_modules/$n"
             [ -d "$d" ] && find "$d" \( -name node_modules -o -name .DS_Store \) -prune -o -type f -print >>"$list"
             case "$n" in @*/*) for sib in ".pi/agent/npm/node_modules/${n%%/*}"/*; do
                 [ "$sib" = "$d" ] || [ ! -d "$sib" ] || find "$sib" \( -name node_modules -o -name .DS_Store \) -prune -o -type f -print >>"$list"; done ;; esac ;;
      git:*|http*|ssh:*) ;;
      *) case "$src" in /*) p=$src ;; "~"/*) p="$HOME/${src#"~/"}" ;; *) p=".pi/agent/$src" ;; esac
         p=$(cd "$p" 2>/dev/null && pwd -P) || { echo "missing $src" >>"$man"; continue; }
         case "$p" in "$hp"/*) r=${p#"$hp"/} ;; *) echo "outside_home $src" >>"$man"; continue ;; esac
         tree "$r"
         case "$r" in .pi/agent/*) ;; *) echo "home $r" >>"$man" ;; esac
         if [ -x "$r/.venv/bin/python" ]; then
           echo "venv $r $("$r/.venv/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" >>"$man"
           { "$r/.venv/bin/python" -m pip list --format=freeze --exclude-editable 2>/dev/null ||
             uv pip list --format=freeze --python "$r/.venv/bin/python" 2>/dev/null; } |
             grep -v -e '@' -e '://' | sed "s#^#pip $r #" >>"$man"
         fi ;;
    esac
  done
}
for c in "$@"; do
  case "$c" in
    codex)  for f in auth.json config.toml AGENTS.md prompts skills rules; do add ".codex/$f"; done ;;
    claude) for f in .credentials.json settings.json CLAUDE.md agents commands skills hooks plugins/installed_plugins.json; do add ".claude/$f"; done
            add ".claude.json" ;;
    pi)     for f in auth.json settings.json models.json anthropic-auth.json AGENTS.md prompts skills extensions themes agents \
                     workflows workflow-profiles memory hud.json .pi-lens.json trust.json web-search.json \
                     anthropic-auth-state.json anthropic-auth-routing-state.json; do add ".pi/agent/$f"; done
            before=$(wc -l <"$list"); pi_packages; npk=$(( $(wc -l <"$list") - before ))
            [ "$npk" -gt 0 ] && entries="$entries${entries:+,}\".pi/agent/packages/\",\".pi/agent/npm/node_modules/\"" ;;
    omp)    for f in agent.db agent.db-wal agent.db-shm config.yml models.yml AGENTS.md mcp.json; do add ".omp/agent/$f"; done ;;
    *) echo "{\"ok\":false,\"error\":\"unknown cli: $c\"}"; rm -f "$list" "$man"; exit 2 ;;
  esac
  command -v "$c" >/dev/null 2>&1 && echo "version $c $(ver "$c")" >>"$man"
done
if command -v uv >/dev/null 2>&1; then # uv tools (MCP server binaries): "uvtool <name>==<ver> <exe>"
  uv tool list 2>/dev/null | awk '/^- /{ if (t != "") print "uvtool " t " " $2; next } NF>=2 { v=$2; sub(/^v/, "", v); t=$1 "==" v }' >>"$man"
fi
if [ ! -s "$list" ]; then rm -f "$list" "$man"; echo '{"ok":false,"error":"nothing to bundle"}'; exit 3; fi
[ -s "$man" ] && printf '%s\n' "$man" >>"$list"
rm -f "$out"
# Every entry is a file already (dirs were expanded above), so node_modules of an npm overlay survives.
COPYFILE_DISABLE=1 tar czhf "$out" --exclude='._*' --exclude='.DS_Store' --exclude='*.pyc' -T "$list" 2>/dev/null
rc=$?
rm -f "$list" "$man"
[ -f "$out" ] || { echo "{\"ok\":false,\"error\":\"tar failed (rc $rc)\"}"; exit 1; }
chmod 600 "$out"
bytes=$(wc -c <"$out" | tr -d ' ')
members=$(tar tzf "$out" 2>/dev/null | wc -l | tr -d ' ')
# rc != 0 with a bundle usually means dangling symlinks were skipped.
printf '{"ok":true,"bundle":"%s","bytes":%s,"members":%s,"pi_package_files":%s,"tar_rc":%s,"entries":[%s],"notes":[%s]}\n' "$out" "$bytes" "$members" "$npk" "$rc" "$entries" "$notes"
