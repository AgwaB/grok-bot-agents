#!/bin/sh
# Runs ON THE USER'S COMPUTER (macOS/Linux) through one machine Shell call. Read-only:
# it only tests whether paths exist (names, never contents) and runs `<cli> --version`.
# omp, when sqlite3 exists (macOS: /usr/bin/sqlite3): copies agent.db(+wal/shm) to a private temp dir, reads only
# provider + whether disabled_cause is set (never the data column), deletes the copy. The original is never opened.
#   sh -s -- [claude codex pi omp] <<'DETECT'
#   ...this file...
#   DETECT
# Prints one JSON object: {"os","home","clis":{<cli>:{installed,version,dir,has_auth,has_config,
#   importable_auth,present,symlinks,auth_status,note}}}. auth_status: {<provider>:"usable"|"disabled"} (omp only). Paths match import_plan.py (check: import_plan.py --lint-scripts).
PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
[ $# -gt 0 ] || set -- claude codex pi omp
case "$(uname -s)" in Darwin) os=macos ;; Linux) os=linux ;; *) os=other ;; esac

esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
b() { if [ "$1" = 1 ]; then printf true; else printf false; fi; }

omp_auth() { # prints provider|usable or provider|disabled lines; nothing if unknown
  command -v sqlite3 >/dev/null 2>&1 || return 0
  t=$(mktemp -d 2>/dev/null) || return 0
  for n in agent.db agent.db-wal agent.db-shm; do [ -f "$1/$n" ] && cp "$1/$n" "$t/$n" 2>/dev/null; done
  sqlite3 -batch -noheader -list "$t/agent.db" \
    "select provider, case when disabled_cause is null then 'usable' else 'disabled' end from auth_credentials;" \
    2>/dev/null </dev/null
  rm -rf "$t"
}

ver() { # first line of `<cli> --version`, killed after ~10 s
  t=$(mktemp 2>/dev/null || echo "/tmp/detect.$$") || return
  ("$1" --version >"$t" 2>&1 </dev/null) &
  p=$!; i=0
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.2; i=$((i + 1)); done
  kill "$p" 2>/dev/null
  head -n 1 "$t" | cut -c1-80; rm -f "$t"
}

first=1
printf '{"os":"%s","home":"%s","clis":{' "$os" "$(esc "$HOME")"
for c in "$@"; do
  case "$c" in
    codex)  d="${CODEX_HOME:-$HOME/.codex}"; auth="auth.json"; conf="config.toml"
            extra="AGENTS.md prompts/ skills/" ;;
    claude) d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; auth=".credentials.json"; conf="settings.json"
            extra="CLAUDE.md agents/ commands/ skills/" ;;
    pi)     d="$HOME/.pi/agent"; auth="auth.json anthropic-auth.json"; conf="settings.json models.json"
            extra="AGENTS.md prompts/ skills/ extensions/" ;;
    omp)    d="$HOME/.omp/agent"; auth="agent.db"; conf="config.yml models.yml mcp.json"
            extra="AGENTS.md" ;;
    *) continue ;;
  esac
  inst=0; v=""; has_auth=0; has_conf=0; present=""; links=0; note=""; astat=""
  if command -v "$c" >/dev/null 2>&1; then inst=1; v=$(ver "$c"); fi
  add() { present="$present${present:+,}\"$(esc "$1")\""; }
  for f in $auth; do [ -f "$d/$f" ] && { has_auth=1; add "$f"; }; done
  for f in $conf; do [ -f "$d/$f" ] && { has_conf=1; add "$f"; }; done
  for f in $extra; do
    case "$f" in
      */) n="${f%/}"
          if [ -d "$d/$n" ] && [ -n "$(ls -A "$d/$n" 2>/dev/null)" ]; then
            add "$f"; l=$(find "$d/$n" -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' '); links=$((links + l))
          fi ;;
      *) [ -f "$d/$f" ] && add "$f" ;;
    esac
  done
  imp=$has_auth
  if [ "$c" = claude ]; then
    [ -f "$HOME/.claude.json" ] && add "../.claude.json"
    if [ "$os" = macos ] && [ "$has_auth" = 0 ]; then
      # Existence check only (no -w/-g: the secret is never read).
      if security find-generic-password -s 'Claude Code-credentials' >/dev/null 2>&1; then
        note="login is in the macOS Keychain, not a file: can't be imported; log in on the box"
      else note="no login found (macOS keeps it in the Keychain)"; fi
    fi
  fi
  if [ "$c" = omp ] && [ -f "$d/agent.db" ]; then
    [ -f "$d/agent.db-wal" ] && add "agent.db-wal"
    [ -f "$d/agent.db-shm" ] && add "agent.db-shm"
    pgrep -x omp >/dev/null 2>&1 && note="omp is running: quit it before copying agent.db"
    umask 077
    rows=$(omp_auth "$d")
    good=$(printf '%s\n' "$rows" | awk -F'|' '$2=="usable"{print $1}' | sort -u)
    for pr in $(printf '%s\n' "$rows" | awk -F'|' 'NF==2{print $1}' | sort -u); do
      if printf '%s\n' "$good" | grep -qxF "$pr"; then st=usable; else st=disabled
        note="${note}${note:+; }omp login for $pr is disabled on this computer: run omp, then /login there first"; fi
      astat="$astat${astat:+,}\"$(esc "$pr")\":\"$st\""
    done
    if command -v sqlite3 >/dev/null 2>&1 && [ -z "$rows" ]; then
      note="${note}${note:+; }omp has no saved login on this computer (or agent.db could not be read)"
    elif [ -n "$rows" ] && [ -z "$good" ]; then imp=0; fi
  fi
  case "$c" in codex) [ -n "${CODEX_HOME:-}" ] && [ "${CODEX_HOME%/}" != "$HOME/.codex" ] && note="CODEX_HOME is set: copy per file, not with bundle_local.sh" ;;
               claude) [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ "${CLAUDE_CONFIG_DIR%/}" != "$HOME/.claude" ] && note="CLAUDE_CONFIG_DIR is set: copy per file, not with bundle_local.sh" ;; esac
  [ "$first" = 1 ] || printf ','
  first=0
  printf '"%s":{"installed":%s,"version":"%s","dir":"%s","has_auth":%s,"importable_auth":%s,"has_config":%s,"present":[%s],"symlinks":%s,"auth_status":{%s},"note":"%s"}' \
    "$c" "$(b $inst)" "$(esc "$v")" "$(esc "$d")" "$(b $has_auth)" "$(b $imp)" "$(b $has_conf)" "$present" "$links" "$astat" "$(esc "$note")"
done
printf '}}\n'
