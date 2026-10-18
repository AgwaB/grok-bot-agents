#!/bin/sh
# Runs ON THE USER'S COMPUTER (macOS/Linux) through one machine Shell call, AFTER the user ticked items on the
# consent card. Exports exactly those secrets, and nothing else, into ~/.agent-import-secrets/ (700, files 600),
# which bundle_local.sh then puts into the bundle and deletes.
#   sh -s -- [--no-login-shell] <item>... <<'EXPORT'
#   ...this file...
#   EXPORT
# Items (anything else is refused before anything is written):
#   env:NAME                         value of NAME from this process, else from the user's interactive login shell
#                                    -> secrets.env (`export NAME='...'`), merged into the box ~/.agent-keys
#   keychain:claude                  macOS Keychain "Claude Code-credentials" -> claude/.credentials.json   (opt-in login copy)
#   keychain:codex                   OS store "Codex Auth" (macOS Keychain, Linux Secret Service) -> codex/auth.json (opt-in)
#   resolve:pi:auth.json:<provider>=NAME     run the provider's `!command` (or `op read` for op://) here,
#   resolve:pi:models.json:<provider>=NAME   store the output as NAME in secrets.env, and record a rewrite so the box
#   resolve:omp:models.yml:<provider>=NAME   config points at the env name NAME instead of the command
# Values go straight from the source into 600 files (umask 077); they never reach stdout, argv or the chat.
# Prints one JSON line: per item "ok" with a byte count, "missing", or the reason it was skipped.
# macOS shows its own "allow security to access" Keychain prompt per keychain item: a second, OS-level consent.
umask 077
PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
case "$(uname -s)" in Darwin) os=macos ;; Linux) os=linux ;; *) os=other ;; esac
loginsh=1; n=$#
while [ "$n" -gt 0 ]; do a=$1; shift; n=$((n - 1)); if [ "$a" = --no-login-shell ]; then loginsh=0; else set -- "$@" "$a"; fi; done
[ $# -gt 0 ] || { echo '{"ok":false,"error":"usage: export_secrets.sh [--no-login-shell] env:NAME|keychain:claude|keychain:codex|resolve:<cli>:<file>:<provider>=NAME ..."}'; exit 2; }
NAME_RE='^[A-Za-z_][A-Za-z0-9_]*$'
for it in "$@"; do  # validate everything first: fail closed
  ok=0
  case "$it" in
    env:*) printf '%s' "${it#env:}" | grep -Eq "$NAME_RE" && ok=1 ;;
    keychain:claude|keychain:codex) ok=1 ;;
    resolve:pi:auth.json:*=*|resolve:pi:models.json:*=*|resolve:omp:models.yml:*=*)
      r=${it#resolve:*:*:}; p=${r%%=*}; v=${r#*=}
      printf '%s' "$p" | grep -Eq '^[A-Za-z0-9_.@:+-]+$' && printf '%s' "$v" | grep -Eq "$NAME_RE" && ok=1 ;;
  esac
  [ "$ok" = 1 ] || { printf '{"ok":false,"error":"refused item (not on the allowed list)"}\n'; exit 2; }
done
OUT="$HOME/.agent-import-secrets"
rm -rf "$OUT"; mkdir -m 700 "$OUT" || { echo '{"ok":false,"error":"cannot create ~/.agent-import-secrets"}'; exit 1; }
T="$OUT/.tmp"; mkdir -m 700 "$T"
res=""
report() { res="$res${res:+,}{\"item\":\"$1\",\"status\":\"$2\"${3:+,\"bytes\":$3}}"; }
tmo() { # <secs> <out file> cmd...: stdout to the file, killed after <secs>
  s=$1; o=$2; shift 2
  ("$@" >"$o" 2>/dev/null </dev/null) &
  p=$!; i=0
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt $((s * 5)) ]; do sleep 0.2; i=$((i + 1)); done
  kill "$p" 2>/dev/null; wait "$p" 2>/dev/null; rc=$?
  # Failed/terminated helpers may have printed a partial secret. Never export it.
  [ "$rc" = 0 ] || : >"$o"
  return "$rc"
}
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
size() { wc -c <"$1" | tr -d ' '; }
add_env() { # <NAME> <file holding the value>: append `export NAME='value'` to secrets.env; never echoes the value
  v=$(cat "$2"); rm -f "$2"
  case "$v" in *'
'*) v=""; return 3 ;; esac
  [ -n "$v" ] || return 1
  vlen=${#v}
  q=$(printf '%s' "$v" | sed "s/'/'\\\\''/g")
  printf "export %s='%s'\n" "$1" "$q" >>"$OUT/secrets.env"
  v=""; q=""
}
login_value() { # <NAME> <out>: the value from the interactive login shell, between markers (rc-file noise is dropped)
  [ "$loginsh" = 1 ] && [ -n "${SHELL:-}" ] && [ -x "$SHELL" ] || return 0
  inner="printf '\\n@@AGENTB@@\\n'; printenv $1; printf '@@AGENTE@@\\n'"
  tmo 10 "$T/raw" "$SHELL" -ilc "/bin/sh -c \"$inner\""
  if ! grep -q '^@@AGENTB@@$' "$T/raw" 2>/dev/null || [ "$(sed -n '/^@@AGENTB@@$/,/^@@AGENTE@@$/p' "$T/raw" | wc -l | tr -d ' ')" -le 2 ]; then
    case "${SHELL##*/}" in bash) tmo 10 "$T/raw" "$SHELL" -ic "/bin/sh -c \"$inner\"" ;; esac
  fi
  sed -n '/^@@AGENTB@@$/,/^@@AGENTE@@$/p' "$T/raw" 2>/dev/null | sed '1d;$d' >"$2"; rm -f "$T/raw"
}
kc_acct() { security find-generic-password -s "$1" 2>/dev/null </dev/null | sed -n 's/^ *"acct"<blob>="\(.*\)"$/\1/p' | head -n 1; }
is_json() { [ -s "$1" ] && [ "$(js_cmd "$1" __json__ none)" = valid ]; }
js_cmd() { # <cli> <file> <provider>: prints the provider's command (for sh -c) to stdout, which the caller captures
  cat >"$T/cmd.js" <<'JS'
var IO = (function () {
  if (typeof require === "function") { var fs = require("fs");
    return { read: function (p) { try { return fs.readFileSync(p, "utf8"); } catch (e) { return null; } } }; }
  ObjC.import("Foundation");
  return { read: function (p) { var s = $.NSString.stringWithContentsOfFileEncodingError($(p), $.NSUTF8StringEncoding, $());
                                return (s && !s.isNil()) ? ObjC.unwrap(s) : null; } };
})();
function yamlGet(text, want) { var stack = [], hit = null;
  text.split(/\r?\n/).forEach(function (line) {
    if (hit !== null || /^\s*(#|$)/.test(line)) return;
    var m = line.match(/^(\s*)("?)([A-Za-z0-9_.@-]+)\2\s*:\s*(.*)$/); if (!m) return;
    while (stack.length && stack[stack.length - 1].ind >= m[1].length) stack.pop();
    var path = stack.map(function (x) { return x.key; }).concat([m[3]]).join("\u0000");
    if (path === want.join("\u0000")) hit = m[4].replace(/\s+#.*$/, "").trim().replace(/^"(.*)"$/, "$1").replace(/^'(.*)'$/, "$1");
    stack.push({ ind: m[1].length, key: m[3] }); });
  return hit; }
function run(a) { // a: path kind provider
  var t = IO.read(a[0]), v = null; if (t === null) return "";
  try {
    if (a[1] === "__json__") { var j = JSON.parse(t); return j && typeof j === "object" && !Array.isArray(j) ? "valid" : ""; }
    if (a[1] === "auth.json") v = (JSON.parse(t)[a[2]] || {}).key;
    else if (a[1] === "models.json") v = ((JSON.parse(t).providers || {})[a[2]] || {}).apiKey;
    else if (a[1] === "models.yml") v = yamlGet(t, ["providers", a[2], "apiKey"]);
  } catch (e) { return ""; }
  if (typeof v !== "string") return "";
  if (v.charAt(0) === "!") return v.slice(1);
  if (/^op:\/\/[^'\s]+$/.test(v)) return "op read '" + v + "'";
  return "";
}
if (typeof require === "function") process.stdout.write(run(process.argv.slice(2)));
JS
  if command -v node >/dev/null 2>&1; then node "$T/cmd.js" "$@" </dev/null 2>/dev/null
  elif command -v bun >/dev/null 2>&1; then bun "$T/cmd.js" "$@" </dev/null 2>/dev/null
  elif [ "$os" = macos ]; then osascript -l JavaScript "$T/cmd.js" "$@" </dev/null 2>/dev/null
  fi
}
for it in "$@"; do
  case "$it" in
    env:*) nm=${it#env:}
      printenv "$nm" >"$T/v" 2>/dev/null
      [ -s "$T/v" ] || login_value "$nm" "$T/v"
      if add_env "$nm" "$T/v"; then report "$it" ok "$vlen"; else
        case $? in 3) report "$it" "skipped: multi-line value" ;; *) report "$it" missing ;; esac; fi ;;
    keychain:claude)
      if [ "$os" != macos ]; then report "$it" "skipped: only macOS keeps Claude's login in the Keychain; use bundle_local.sh --copy-login claude"; continue; fi
      if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ "${CLAUDE_CONFIG_DIR%/}" != "$HOME/.claude" ]; then report "$it" "skipped: CLAUDE_CONFIG_DIR is set (per-dir Keychain item)"; continue; fi
      acct=$(kc_acct 'Claude Code-credentials'); mkdir -p "$OUT/claude"
      tmo 120 "$OUT/claude/.credentials.json" security find-generic-password -s 'Claude Code-credentials' -a "${acct:-$USER}" -w
      if is_json "$OUT/claude/.credentials.json"; then report "$it" ok "$(size "$OUT/claude/.credentials.json")"
      else rm -f "$OUT/claude/.credentials.json"; report "$it" "missing (not found, denied, or not JSON)"; fi ;;
    keychain:codex)
      ch=${CODEX_HOME:-$HOME/.codex}; ch=$(cd "$ch" 2>/dev/null && pwd -P || printf '%s' "$ch")
      h=$( (printf '%s' "$ch" | shasum -a 256 2>/dev/null || printf '%s' "$ch" | sha256sum) | cut -c1-16)
      mkdir -p "$OUT/codex"; f="$OUT/codex/auth.json"
      case "$os" in
        macos) tmo 120 "$f" security find-generic-password -s 'Codex Auth' -a "cli|$h" -w ;;
        linux) if command -v secret-tool >/dev/null 2>&1; then
                 tmo 60 "$f" secret-tool lookup service 'Codex Auth' username "cli|$h"
               fi ;;
      esac
      if is_json "$f"; then report "$it" ok "$(size "$f")"; else rm -f "$f"; report "$it" "missing (not found, denied, or not JSON)"; fi ;;
    resolve:*)
      r=${it#resolve:}; cli=${r%%:*}; r=${r#*:}; file=${r%%:*}; r=${r#*:}; prov=${r%%=*}; nm=${r#*=}
      case "$cli" in pi) d=${PI_CODING_AGENT_DIR:-$HOME/.pi/agent} ;; *) d=$HOME/.omp/agent ;; esac
      cmd=$(js_cmd "$d/$file" "$file" "$prov")
      if [ -z "$cmd" ]; then report "$it" "missing (no !command or op:// ref for that provider)"; continue; fi
      tmo 120 "$T/v" sh -c "$cmd"; cmd=""
      if add_env "$nm" "$T/v"; then
        printf '%s %s %s %s\n' "$cli" "$file" "$prov" "$nm" >>"$OUT/rewrites.txt"
        report "$it" ok "$vlen"
      else report "$it" "missing (the command printed nothing or failed)"; fi ;;
  esac
done
rm -rf "$T"
files=$(cd "$OUT" && find . -type f | sed 's#^\./##' | sort | tr '\n' ' ')
[ -f "$OUT/secrets.env" ] && chmod 600 "$OUT/secrets.env"
printf '{"ok":true,"dir":"%s","files":"%s","items":[%s],"next":"run bundle_local.sh now: it bundles and then deletes this dir"}\n' "$(esc "$OUT")" "${files% }" "$res"
