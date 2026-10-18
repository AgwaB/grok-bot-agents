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
#   sh -s -- [--copy-login claude,codex] [--include <home-rel path>]... [--skip <home-rel path>]... <cli>...
# Rotating subscription logins stay OUT by default (the box gets its own login): ~/.claude/.credentials.json, a ChatGPT
# ~/.codex/auth.json and ~/.codex/.credentials.json (MCP OAuth) go in only with --copy-login <cli>. A Codex API-key
# auth.json (static) is always bundled. Pi package auth files match *auth*.json, *token*.json, *credential*.json,
# web-search*.json; ~/.pi/agent/.env only with --include .pi/agent/.env. --skip drops a path the user unticked.
# ~/.agent-import-secrets/ (written by export_secrets.sh from the consent card) is bundled, then deleted here.
umask 077
copy_login=""; incl=" "; skipl=""; n=$#
while [ "$n" -gt 0 ]; do
  a=$1; shift; n=$((n - 1))
  case "$a" in
    --copy-login|--include|--skip)
      [ "$n" -gt 0 ] || { echo '{"ok":false,"error":"option needs a value"}'; exit 2; }
      case "$a" in --copy-login) copy_login=",$1," ;; --include) incl="$incl$1 " ;; --skip) skipl="$skipl$1
" ;; esac
      shift; n=$((n - 1)) ;;
    *) set -- "$@" "$a" ;;
  esac
done
[ $# -gt 0 ] || { echo '{"ok":false,"error":"usage: bundle_local.sh [--copy-login claude,codex] [--include P] [--skip P] <cli>..."}'; exit 2; }
for c in "$@"; do
  case "$c" in
    codex) override=${CODEX_HOME:-}; default="$HOME/.codex" ;;
    claude) override=${CLAUDE_CONFIG_DIR:-}; default="$HOME/.claude" ;;
    pi) override=${PI_CODING_AGENT_DIR:-}; default="$HOME/.pi/agent" ;;
    omp) override=""; default="" ;;
    *) echo '{"ok":false,"error":"unknown cli"}'; exit 2 ;;
  esac
  if [ -n "$override" ] && [ "${override%/}" != "$default" ]; then
    echo '{"ok":false,"error":"CLI directory override is set: use per-file import, not bundle_local.sh"}'; exit 2
  fi
done
PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
cd "$HOME" || exit 2
hp=$(pwd -P)
out="$HOME/agent-import.tgz"
man=".agent-import-manifest.txt"
list=$(mktemp) || exit 1
trap 'rm -f "$list" "$list.js" "$list.safe" "$man"' EXIT HUP INT TERM
: >"$man"
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
static_codex() { # Parse, never infer "static" from the absence of a single-line tokens pattern.
  cat >"$list.js" <<'JS'
function run(a) {
  try {
    var t;
    if (typeof require === "function") t = require("fs").readFileSync(a[0], "utf8");
    else { ObjC.import("Foundation"); t = ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError($(a[0]), $.NSUTF8StringEncoding, $())); }
    var j = JSON.parse(t);
    return j && typeof j.OPENAI_API_KEY === "string" && j.OPENAI_API_KEY.length > 0 && !j.tokens ? "static" : "unknown";
  } catch (e) { return "unknown"; }
}
if (typeof require === "function") console.log(run(process.argv.slice(2)));
JS
  if command -v node >/dev/null 2>&1; then r=$(node "$list.js" .codex/auth.json 2>/dev/null)
  elif command -v bun >/dev/null 2>&1; then r=$(bun "$list.js" .codex/auth.json 2>/dev/null)
  elif [ "$(uname -s)" = Darwin ]; then r=$(osascript -l JavaScript "$list.js" .codex/auth.json 2>/dev/null)
  else r=unknown; fi
  [ "$r" = static ]
}
entries=""; notes=""; npk=0; left=""
leave() { left="$left${left:+,}\"$(esc "$1")\""; }
add() { # a file, or every file under a dir (symlinked dirs followed, so linked skills become real files)
  [ -e "$1" ] || return 0
  if [ -d "$1" ]; then
    find -L "$1" \( -name node_modules -o -name .git -o -name .venv -o -name __pycache__ -o -name .DS_Store -o -name '._*' \) \
      -prune -o -type f -print >>"$list" 2>/dev/null
  else printf '%s\n' "$1" >>"$list"; fi
  entries="$entries${entries:+,}\"$(esc "$1")\""
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
  # A CLI may print credentials in diagnostics: retain only a numeric version.
  wait "$p" 2>/dev/null; rc=$?
  if [ "$rc" = 0 ]; then head -n 1 "$t" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1; fi
  rm -f "$t"
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
         case "$r" in .pi/agent/packages/*|.pi/agent/packages) ;; .*|Library|Library/*|AppData|AppData/*|*/../*|*/..) note "unsafe package root skipped"; continue ;; esac
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
login() { # <cli> <path>: a rotating login file, bundled only with --copy-login <cli>
  [ -e "$2" ] || return 0
  case "$copy_login" in *",$1,"*) add "$2" ;; *) leave "$2 (default: log in on the box; --copy-login $1 to copy)" ;; esac
}
for c in "$@"; do
  case "$c" in
    codex)  # a ChatGPT login has a "tokens" object (rotating); an API-key auth.json doesn't (static, always copied)
            if [ -f .codex/auth.json ] && static_codex; then add .codex/auth.json
            else login codex .codex/auth.json; fi
            login codex .codex/.credentials.json
            for f in config.toml AGENTS.md prompts skills rules; do add ".codex/$f"; done ;;
    claude) login claude .claude/.credentials.json
            for f in settings.json CLAUDE.md agents commands skills hooks plugins/installed_plugins.json; do add ".claude/$f"; done
            add ".claude.json" ;;
    pi)     for f in auth.json settings.json models.json AGENTS.md prompts skills extensions themes agents \
                     workflows workflow-profiles memory hud.json .pi-lens.json trust.json; do add ".pi/agent/$f"; done
            for g in .pi/agent/*auth*.json .pi/agent/*token*.json .pi/agent/*credential*.json .pi/agent/web-search*.json; do
              case "$g" in .pi/agent/auth.json|.pi/agent/oauth.json) ;; *) [ -f "$g" ] && add "$g" ;; esac
            done
            if [ -f .pi/agent/.env ]; then case "$incl" in *" .pi/agent/.env "*) add .pi/agent/.env ;; *) leave ".pi/agent/.env (off by default; --include .pi/agent/.env)" ;; esac; fi
            before=$(wc -l <"$list"); pi_packages; npk=$(( $(wc -l <"$list") - before ))
            [ "$npk" -gt 0 ] && entries="$entries${entries:+,}\".pi/agent/packages/\",\".pi/agent/npm/node_modules/\"" ;;
    omp)    for f in agent.db agent.db-wal agent.db-shm config.yml models.yml AGENTS.md mcp.json; do add ".omp/agent/$f"; done ;;
    *) exit 2 ;;
  esac
  command -v "$c" >/dev/null 2>&1 && echo "version $c $(ver "$c")" >>"$man"
done
if command -v uv >/dev/null 2>&1; then # uv tools (MCP server binaries): "uvtool <name>==<ver> <exe>"
  uv tool list 2>/dev/null | awk '/^- /{ if (t != "") print "uvtool " t " " $2; next } NF>=2 { v=$2; sub(/^v/, "", v); t=$1 "==" v }' >>"$man"
fi
secrets=0
if [ -d .agent-import-secrets ]; then # consented exports from export_secrets.sh
  find .agent-import-secrets -type f ! -path '*/.tmp/*' >>"$list"; secrets=$(find .agent-import-secrets -type f ! -path '*/.tmp/*' | wc -l | tr -d ' ')
fi
# Resolve links before tar -h, including parent-directory links. Never follow a skill into credentials.
safe_source() {
  p="$hp/$1"; hops=0
  while [ -L "$p" ]; do
    hops=$((hops + 1)); [ "$hops" -le 40 ] || return 1
    link=$(readlink "$p") || return 1
    case "$link" in /*) p=$link ;; *) p="$(dirname "$p")/$link" ;; esac
  done
  parent=$(cd "$(dirname "$p")" 2>/dev/null && pwd -P) || return 1
  p="$parent/$(basename "$p")"
  case "$p" in "$hp"/.agent-keys|"$hp"/.claude.json|"$hp"/.claude/.credentials.json|"$hp"/.claude/settings.json|"$hp"/.codex/auth.json|"$hp"/.codex/.credentials.json|"$hp"/.codex/config.toml|"$hp"/.pi/agent/*auth*.json|"$hp"/.pi/agent/*token*.json|"$hp"/.pi/agent/*credential*.json|"$hp"/.pi/agent/.env|"$hp"/.omp/agent/agent.db*)
    [ "$p" = "$hp/$1" ] || return 1 ;; esac
  case "$p" in "$hp"/.ssh/*|"$hp"/.gnupg/*|"$hp"/.aws/*|"$hp"/.azure/*|"$hp"/.kube/*|"$hp"/.config/*|"$hp"/.mozilla/*|"$hp"/Library/*|"$hp"/AppData/*|"$hp"/.netrc|"$hp"/.git-credentials) return 1 ;;
    "$hp"/*) [ -f "$p" ] ;; *) return 1 ;; esac
}
: >"$list.safe"
while IFS= read -r f; do
  skip=0
  # Newline-delimited selections preserve spaces; a directory selection applies to all descendants.
  oldifs=$IFS; IFS='
'
  for s in $skipl; do case "$f" in "${s%/}"|"${s%/}"/*) skip=1 ;; esac; done
  IFS=$oldifs
  if [ "$skip" = 1 ]; then leave "$f (unticked)"
  elif safe_source "$f"; then printf '%s\n' "$f" >>"$list.safe"
  else note "unsafe or dangling source skipped"; fi
done <"$list"
mv "$list.safe" "$list"
if [ ! -s "$list" ]; then echo '{"ok":false,"error":"nothing to bundle"}'; exit 3; fi
[ -s "$man" ] && printf '%s\n' "$man" >>"$list"
rm -f "$out"
# Every entry is a file already (dirs were expanded above), so node_modules of an npm overlay survives.
COPYFILE_DISABLE=1 tar czhf "$out" --exclude='._*' --exclude='.DS_Store' --exclude='*.pyc' -T "$list" 2>/dev/null
rc=$?
if [ "$rc" -ne 0 ] || [ ! -f "$out" ]; then
  rm -f "$out"
  echo '{"ok":false,"error":"tar failed; consented exports kept for retry"}'; exit 1
fi
rm -rf .agent-import-secrets
chmod 600 "$out"
bytes=$(wc -c <"$out" | tr -d ' ')
members=$(tar tzf "$out" 2>/dev/null | wc -l | tr -d ' ')
# rc != 0 with a bundle usually means dangling symlinks were skipped.
printf '{"ok":true,"bundle":"%s","bytes":%s,"members":%s,"pi_package_files":%s,"secret_files":%s,"tar_rc":%s,"entries":[%s],"left_out":[%s],"notes":[%s]}\n' "$(esc "$out")" "$bytes" "$members" "$npk" "$secrets" "$rc" "$entries" "$left" "$notes"
