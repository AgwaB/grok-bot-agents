#!/bin/sh
# Runs ON THE USER'S COMPUTER (macOS/Linux) through one machine Shell call. Read-only:
# it only tests whether paths exist (names, never contents) and runs `<cli> --version`.
# omp, when sqlite3 exists (macOS: /usr/bin/sqlite3): copies agent.db(+wal/shm) to a private temp dir, reads only
# provider + whether disabled_cause is set (never the data column), deletes the copy. The original is never opened.
#   sh -s -- [claude codex pi omp] <<'DETECT'
#   ...this file...
#   DETECT
# Prints one JSON object: {"os","home","clis":{<cli>:{installed,version,dir,has_auth,has_config,
#   importable_auth,login_store,present,symlinks,auth_status,note}},"secrets":{...}}. auth_status: {<provider>:"usable"|"disabled"}
#   (omp only). login_store: file|keychain|keyring|none (where the CLI's own login lives). Paths match import_plan.py
#   (check: import_plan.py --lint-scripts).
# "secrets" is a NAME-ONLY inventory for the consent card: a scanner (node, else bun, else macOS JXA) reads the CLI
#   config/auth files in memory and reports where secrets are (login files, OAuth providers with expired true/false,
#   env names configs reference, !cmd/op:// indirections with the tool's first word only, literal keys in configs by key
#   path, package auth files, remote MCP servers that need OAuth), never a value, command line or token.
#   env_set: which candidate env names are set, in this process or in the user's interactive login shell
#   ($SHELL -ilc, names tested with printenv inside a subshell; values never leave it). --no-login-shell skips that.
#   stores: OS credential-store presence only (macOS `security find-generic-password` without -w/-g, Linux
#   `secret-tool lookup` with stdout to /dev/null): Claude Code-credentials, Codex Auth, Codex MCP Credentials.
PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
loginsh=1; n=$#
while [ "$n" -gt 0 ]; do a=$1; shift; n=$((n - 1)); if [ "$a" = --no-login-shell ]; then loginsh=0; else set -- "$@" "$a"; fi; done
[ $# -gt 0 ] || set -- claude codex pi omp
clis="$*"
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
  # A CLI may print credentials in diagnostics: retain only a numeric version.
  wait "$p" 2>/dev/null; rc=$?
  if [ "$rc" = 0 ]; then head -n 1 "$t" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1; fi
  rm -f "$t"
}

tmo() { # run "$@" with stdout to file $TMO_OUT, killed after ~10 s
  ("$@" >"$TMO_OUT" 2>/dev/null </dev/null) &
  p=$!; i=0
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.2; i=$((i + 1)); done
  kill "$p" 2>/dev/null; wait "$p" 2>/dev/null
}
store_present() { # <service>: 1 if an OS credential store has it (presence only, never the secret)
  case "$os" in
    macos) security find-generic-password -s "$1" >/dev/null 2>&1 </dev/null && echo 1 ;;
    linux) if command -v secret-tool >/dev/null 2>&1 && [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
             TMO_OUT=/dev/null tmo secret-tool lookup service "$1" && echo 1   # value goes to /dev/null
           fi ;;
  esac
}
SEC=$(mktemp -d 2>/dev/null || { mkdir -m 700 "/tmp/detect-sec.$$" && echo "/tmp/detect-sec.$$"; })
chmod 700 "$SEC"
scan_secrets() { # prints "NAMES ..." then one JSON line (names only)
  cat >"$SEC/scan.js" <<'SCANJS'
// grok-bot-agents secret inventory: NAMES ONLY. Reads CLI config/auth files in memory and prints where secrets are,
// what kind they are and which env names configs reference. Never prints a value, a command line or a token.
// Runs under node, bun, or macOS JXA (osascript -l JavaScript). Args: <home> <clis...>
var IO = (function () {
  if (typeof require === "function") {
    var fs = require("fs");
    return { read: function (p) { try { return fs.readFileSync(p, "utf8"); } catch (e) { return null; } },
             ls: function (p) { try { return fs.readdirSync(p); } catch (e) { return []; } },
             isFile: function (p) { try { return fs.statSync(p).isFile(); } catch (e) { return false; } } };
  }
  ObjC.import("Foundation");
  var fm = $.NSFileManager.defaultManager;
  return { read: function (p) { var s = $.NSString.stringWithContentsOfFileEncodingError($(p), $.NSUTF8StringEncoding, $());
                                return (s && !s.isNil()) ? ObjC.unwrap(s) : null; },
           ls: function (p) { var a = fm.contentsOfDirectoryAtPathError($(p), $()); return (a && !a.isNil()) ? ObjC.deepUnwrap(a) : []; },
           isFile: function (p) { var d = Ref(); return fm.fileExistsAtPathIsDirectory($(p), d) && !d[0]; } };
})();
var SECRETISH = /(api[_-]?key|apikey|token|secret|passw(or)?d|bearer|authorization|credential|private[_-]?key|access[_-]?key|cookie)/i;
var ENVNAME = /^[A-Za-z_][A-Za-z0-9_]{1,127}$/;
var SAFE = /^[A-Za-z0-9_.@:+-]{1,80}$/;  // ids we may print (provider ids, server names, keys)
function safe(s) { s = String(s); return SAFE.test(s) ? s : "<id>"; }
function tool(cmd) { // first word only (the rest may hold a secret)
  var w = String(cmd).replace(/^!/, "").trim().split(/\s+/)[0] || "";
  w = w.replace(/^["']|["']$/g, "").split("/").pop();
  return /^[A-Za-z0-9_.+-]{1,40}$/.test(w) ? w : "<cmd>";
}
var items = [], refs = {};
function add(o) { items.push(o); }
function ref(name, cli, where) { if (!ENVNAME.test(name)) return; (refs[name] = refs[name] || []).push(cli + " " + where); }
function json(p) { var t = IO.read(p); if (t === null) return undefined; try { return JSON.parse(t); } catch (e) { return null; } }
// a string value: env name, !command, op:// ref, ${VAR} interpolation, or a literal
function classify(v) {
  if (typeof v !== "string" || !v) return null;
  if (v.charAt(0) === "!") return { k: "cmd", tool: tool(v) };
  if (/^op:\/\//.test(v)) return { k: "op" };
  var m = v.match(/\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}|^\$([A-Za-z_][A-Za-z0-9_]*)$/);
  if (m) return { k: "env", name: m[1] || m[3] };
  if (/^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$/.test(v) && v.length <= 64) return { k: "env", name: v };  // needs an "_": a bare uppercase literal key stays a literal
  return { k: "literal" };
}
function value(cli, where, v, keyName, provider) {
  var c = classify(v); if (!c) return;
  if (c.k === "env") { ref(c.name, cli, where); return; }
  if (c.k === "cmd" || c.k === "op") {
    var o = { cli: cli, kind: "indirect", where: where, via: c.k === "op" ? "op://" : "!cmd", tool: c.tool || "op", rotation: "static", default: provider ? "resolve" : "manual" };
    if (provider) o.provider = provider;
    add(o); return;
  }
  if (SECRETISH.test(keyName || "") || SECRETISH.test(where)) add({ cli: cli, kind: "literal_in_config", where: where, rotation: "static", default: "rides with the config file (sensitive)" });
}
function mcp(cli, file, servers) {
  if (!servers || typeof servers !== "object") return;
  Object.keys(servers).forEach(function (n) {
    var s = servers[n] || {}, w = file + " mcpServers." + safe(n);
    ["env", "headers"].forEach(function (k) {
      var o = s[k]; if (!o || typeof o !== "object") return;
      Object.keys(o).forEach(function (e) { value(cli, w + "." + k + "." + safe(e), o[e], e); });
    });
    var remote = s.url || s.type === "http" || s.type === "sse";
    var hasAuth = s.headers && Object.keys(s.headers).some(function (h) { return /authorization|api[-_]?key|token/i.test(h); });
    if (remote && !hasAuth) add({ cli: cli, kind: "mcp_oauth", where: w, rotation: "oauth", default: "box-login (authenticate the server on the box)" });
  });
}
function codex(d) {
  var a = json(d + "/auth.json");
  if (a) add({ cli: "codex", kind: "login_file", where: "auth.json", type: a.tokens ? "oauth" : (a.OPENAI_API_KEY ? "api_key" : "unknown"),
               rotation: a.tokens ? "oauth" : "static", default: a.tokens ? "box-login" : "copy" });
  var cr = json(d + "/.credentials.json");
  if (cr) add({ cli: "codex", kind: "mcp_oauth_file", where: ".credentials.json", servers: Object.keys(cr).length, rotation: "oauth", default: "box-login" });
  var t = IO.read(d + "/config.toml"); if (t === null) return {};
  var stores = {}, table = "", lines = t.split(/\r?\n/), mcps = {};
  for (var i = 0; i < lines.length; i++) {
    var L = lines[i].replace(/^\s+/, ""); if (!L || L.charAt(0) === "#") continue;
    var h = L.match(/^\[\[?\s*([^\]]+?)\s*\]\]?/); if (h) { table = h[1].replace(/"/g, ""); continue; }
    var kv = L.match(/^("?)([A-Za-z0-9_.-]+)\1\s*=\s*(.*)$/); if (!kv) continue;
    var key = kv[2], raw = kv[3].replace(/\s+#[^"']*$/, "").trim(), path = (table ? table + "." : "") + key;
    var sv = (raw.match(/^"((?:[^"\\]|\\.)*)"|^'([^']*)'/) || []); var str = sv[1] !== undefined ? sv[1] : sv[2];
    var tw = table ? "config.toml " + table.split(".").map(safe).join(".") : "config.toml";
    var sm = table.match(/^mcp_servers\.([^.]+)(\.|$)/);
    if (sm) { var S = mcps[sm[1]] = mcps[sm[1]] || {}; if (key === "url" && !sm[2]) S.url = 1;
              if (key === "bearer_token_env_var" || key === "experimental_bearer_token" || /env_http_headers|http_headers/.test(table + "." + key)) S.auth = 1; }
    if (!table && (key === "cli_auth_credentials_store" || key === "mcp_oauth_credentials_store")) {
      stores[key] = /^(file|keyring|auto|ephemeral)$/.test(str || "") ? str : "other"; continue;
    }
    if (key === "env_key" || key === "bearer_token_env_var") { if (str) ref(str, "codex", "config.toml " + safe(path)); continue; }
    if (key === "env_vars") { (raw.match(/"([A-Za-z_][A-Za-z0-9_]*)"/g) || []).forEach(function (q) { ref(q.replace(/"/g, ""), "codex", "config.toml " + safe(path)); }); continue; }
    if (/(^|\.)auth$/.test(table) && key === "command") { add({ cli: "codex", kind: "indirect", where: tw + ".command", via: "auth.command", tool: tool(str || (raw.match(/"([^"]+)"/) || [])[1] || ""), rotation: "static", default: "manual" }); continue; }
    var inl = raw.match(/^\{(.*)\}$/);
    var envTbl = /(^|\.)env_http_headers$/.test(table) || key === "env_http_headers";
    if (inl) {
      (inl[1].match(/("?)([A-Za-z0-9_.-]+)\1\s*=\s*"((?:[^"\\]|\\.)*)"/g) || []).forEach(function (pair) {
        var m = pair.match(/("?)([A-Za-z0-9_.-]+)\1\s*=\s*"((?:[^"\\]|\\.)*)"/);
        if (envTbl) ref(m[3], "codex", "config.toml " + safe(path) + "." + safe(m[2]));
        else value("codex", "config.toml " + safe(path) + "." + safe(m[2]), m[3], m[2]);
      });
      continue;
    }
    if (str === undefined) continue;
    if (envTbl) { ref(str, "codex", "config.toml " + safe(path)); continue; }
    if (key === "experimental_bearer_token" || /(^|\.)(env|http_headers)$/.test(table) || SECRETISH.test(key))
      value("codex", (table ? tw + "." : "config.toml ") + safe(key), str, key === "experimental_bearer_token" ? "token" : key);
  }
  Object.keys(mcps).forEach(function (n) {
    if (mcps[n].url && !mcps[n].auth) add({ cli: "codex", kind: "mcp_oauth", where: "config.toml mcp_servers." + safe(n), rotation: "oauth", default: "box-login (codex mcp login " + safe(n) + ")" });
  });
  return stores;
}
function claude(d, home) {
  var c = json(d + "/.credentials.json");
  if (c) add({ cli: "claude", kind: "login_file", where: ".credentials.json", type: c.claudeAiOauth ? "oauth" : "unknown",
               mcp_oauth: c.mcpOAuth ? Object.keys(c.mcpOAuth).length : 0, rotation: "oauth", default: "box-login" });
  var s = json(d + "/settings.json") || {};
  if (s.env) Object.keys(s.env).forEach(function (k) {
    if (SECRETISH.test(k)) add({ cli: "claude", kind: "literal_in_config", where: "settings.json env." + safe(k), rotation: "static", default: "rides with the config file (sensitive)" });
  });
  if (typeof s.apiKeyHelper === "string") add({ cli: "claude", kind: "indirect", where: "settings.json apiKeyHelper", via: "apiKeyHelper", tool: tool(s.apiKeyHelper), rotation: "unknown", default: "manual" });
  var cj = json(home + "/.claude.json") || {};
  mcp("claude", "~/.claude.json", cj.mcpServers);
}
function pi(d) {
  var a = json(d + "/auth.json") || {}, now = Date.now();
  Object.keys(a).forEach(function (p) {
    var e = a[p] || {}, w = "auth.json " + safe(p);
    if (e.type === "oauth") add({ cli: "pi", kind: "oauth_provider", provider: safe(p), where: w, expired: typeof e.expires === "number" ? e.expires < now : null,
                                  rotation: "oauth", default: "copy", tos_flag: /anthropic|claude/i.test(p) || undefined });
    else if (e.type === "api_key" || typeof e.key === "string") {
      var c = classify(e.key);
      if (c && c.k === "env") ref(c.name, "pi", w);
      else if (c && (c.k === "cmd" || c.k === "op")) add({ cli: "pi", kind: "indirect", provider: safe(p), where: w, via: c.k === "op" ? "op://" : "!cmd", tool: c.tool || "op", rotation: "static", default: "resolve" });
      else add({ cli: "pi", kind: "api_key_stored", provider: safe(p), where: w, rotation: "static", default: "copy" });
    }
  });
  var m = json(d + "/models.json") || {};
  Object.keys(m.providers || {}).forEach(function (p) {
    var e = m.providers[p] || {}, w = "models.json providers." + safe(p);
    if (e.apiKey !== undefined) value("pi", w + ".apiKey", e.apiKey, "apiKey", safe(p));
    Object.keys(e.headers || {}).forEach(function (h) { value("pi", w + ".headers." + safe(h), e.headers[h], h); });
  });
  var mj = json(d + "/mcp.json"); if (mj) mcp("pi", "mcp.json", mj.mcpServers);
  IO.ls(d).sort().forEach(function (f) {
    if (f === "auth.json" || f === "oauth.json" || !IO.isFile(d + "/" + f)) return;
    if (/\.json$/.test(f) && (/(auth|token|credential)/i.test(f) || /^web-search/.test(f)))
      add({ cli: "pi", kind: "package_auth_file", where: safe(f), rotation: "unknown", default: "copy" });
    else if (f === ".env") {
      var names = ((IO.read(d + "/.env") || "").match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/gm) || []).map(function (x) { return x.replace(/^\s*(export\s+)?/, "").replace(/\s*=$/, ""); });
      add({ cli: "pi", kind: "package_auth_file", where: ".env", names: names.filter(function (n) { return ENVNAME.test(n); }), rotation: "static", default: "off (opt-in)" });
    }
  });
}
function yamlWalk(text, fn) { // indentation-based key paths of a simple YAML file: fn(pathArray, key, rawValue)
  var stack = [];
  text.split(/\r?\n/).forEach(function (line) {
    if (/^\s*(#|$)/.test(line)) return;
    var m = line.match(/^(\s*)(?:-\s+)?("?)([A-Za-z0-9_.@-]+)\2\s*:\s*(.*)$/); if (!m) return;
    var ind = m[1].length;
    while (stack.length && stack[stack.length - 1].ind >= ind) stack.pop();
    var v = m[4].replace(/\s+#.*$/, "").trim().replace(/^["']|["']$/g, "");
    fn(stack.map(function (x) { return x.key; }), m[3], v);
    stack.push({ ind: ind, key: m[3] });
  });
}
function omp(d) {
  var t = IO.read(d + "/models.yml");
  if (t !== null) yamlWalk(t, function (path, key, v) {
    if (!v) return;
    var w = "models.yml " + path.concat([key]).map(safe).join(".");
    if (key === "apiKey") value("omp", w, v, key, path.length === 2 && path[0] === "providers" ? safe(path[1]) : undefined);
    else if (path.indexOf("headers") >= 0) value("omp", w, v, key);
  });
  var c = IO.read(d + "/config.yml");
  if (c !== null) yamlWalk(c, function (path, key, v) {
    if (!v) return;
    var w = "config.yml " + path.concat([key]).map(safe).join(".");
    if (SECRETISH.test(key) || (path.indexOf("broker") >= 0 && /token/i.test(key))) value("omp", w, v, key);
  });
  var mj = json(d + "/mcp.json"); if (mj) mcp("omp", "mcp.json", mj.mcpServers);
}
var WELL_KNOWN = {
  codex: ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"],
  claude: ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
  pi: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
       "MISTRAL_API_KEY", "CEREBRAS_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY", "DASHSCOPE_API_KEY", "EXA_API_KEY",
       "TAVILY_API_KEY", "BRAVE_API_KEY", "PERPLEXITY_API_KEY"],
  omp: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
        "OMP_AUTH_BROKER_URL", "OMP_AUTH_BROKER_TOKEN"]
};
function run(argv) {
  // argv: <home> <codex dir> <claude dir> <pi dir> <omp dir> <claude global dir> <clis...>
  var dirs = { codex: argv[1], claude: argv[2], pi: argv[3], omp: argv[4] }, clis = argv.slice(6), stores = {}, names = {};
  clis.forEach(function (c) {
    try {
      if (c === "codex") stores = codex(dirs.codex) || {};
      else if (c === "claude") claude(dirs.claude, argv[5]);
      else if (c === "pi") pi(dirs.pi);
      else if (c === "omp") omp(dirs.omp);
      (WELL_KNOWN[c] || []).forEach(function (n) { names[n] = 1; });
    } catch (e) { add({ cli: c, kind: "scan_error", where: String(e && e.name || "error") }); }
  });
  Object.keys(refs).forEach(function (n) { names[n] = 1; });
  var out = { codex_stores: stores, items: items, env_refs: refs };
  return "NAMES " + Object.keys(names).sort().join(" ") + "\n" + JSON.stringify(out);
}
if (typeof require === "function") console.log(run(process.argv.slice(2)));
SCANJS
  cdir="${CODEX_HOME:-$HOME/.codex}"; ldir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; pdir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
  # shellcheck disable=SC2086
  if command -v node >/dev/null 2>&1; then echo 'SCANNER node'; node "$SEC/scan.js" "$HOME" "$cdir" "$ldir" "$pdir" "$HOME/.omp/agent" "${CLAUDE_CONFIG_DIR:-$HOME}" $clis </dev/null 2>/dev/null
  elif command -v bun >/dev/null 2>&1; then echo 'SCANNER bun'; bun "$SEC/scan.js" "$HOME" "$cdir" "$ldir" "$pdir" "$HOME/.omp/agent" "${CLAUDE_CONFIG_DIR:-$HOME}" $clis </dev/null 2>/dev/null
  elif [ "$os" = macos ]; then echo 'SCANNER jxa'; osascript -l JavaScript "$SEC/scan.js" "$HOME" "$cdir" "$ldir" "$pdir" "$HOME/.omp/agent" "${CLAUDE_CONFIG_DIR:-$HOME}" $clis </dev/null 2>/dev/null
  fi
}
env_set() { # <names...>: "NAME process" / "NAME login_shell" lines; values are never printed
  for v in "$@"; do [ -n "$(printenv "$v" 2>/dev/null)" ] && echo "$v process"; done
  [ "$loginsh" = 1 ] && [ -n "${SHELL:-}" ] && [ -x "$SHELL" ] || return 0
  inner="for v in $*; do [ -n \"\$(printenv \$v)\" ] && echo @@AGENTENV@@\$v; done"
  TMO_OUT="$SEC/env.txt" tmo "$SHELL" -ilc "/bin/sh -c '$inner'"
  case "${SHELL##*/}" in bash) # a bash login shell skips ~/.bashrc unless ~/.profile sources it
    TMO_OUT="$SEC/env2.txt" tmo "$SHELL" -ic "/bin/sh -c '$inner'"; cat "$SEC/env2.txt" >>"$SEC/env.txt" 2>/dev/null ;; esac
  sed -n 's/^@@AGENTENV@@\([A-Za-z_][A-Za-z0-9_]*\)$/\1 login_shell/p' "$SEC/env.txt" 2>/dev/null
  rm -f "$SEC/env.txt" "$SEC/env2.txt"
}

first=1
printf '{"os":"%s","home":"%s","clis":{' "$os" "$(esc "$HOME")"
for c in "$@"; do
  case "$c" in
    codex)  d="${CODEX_HOME:-$HOME/.codex}"; auth="auth.json .credentials.json"; conf="config.toml"
            extra="AGENTS.md prompts/ skills/ rules/" ;;
    claude) d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; auth=".credentials.json"; conf="settings.json"
            extra="CLAUDE.md agents/ commands/ skills/ hooks/" ;;
    pi)     d="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"; auth="auth.json .env"
            # package auth files: generic globs (same as import_plan.py), not per-package names
            for g in "$d"/*auth*.json "$d"/*token*.json "$d"/*credential*.json "$d"/web-search*.json; do
              b=${g##*/}; [ -f "$g" ] && [ "$b" != auth.json ] && [ "$b" != oauth.json ] && auth="$auth $b"
            done
            conf="settings.json models.json hud.json .pi-lens.json trust.json"
            extra="AGENTS.md prompts/ skills/ extensions/ themes/ agents/ workflows/ workflow-profiles/ memory/ packages/" ;;
    omp)    d="$HOME/.omp/agent"; auth="agent.db"; conf="config.yml models.yml mcp.json"
            extra="AGENTS.md" ;;
    *) continue ;;
  esac
  inst=0; v=""; has_auth=0; has_conf=0; present=""; links=0; note=""; astat=""; lstore=none
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
  case "$c" in
    codex) [ -f "$d/auth.json" ] && lstore="file"
           if [ "$lstore" = none ] && [ "$(store_present 'Codex Auth')" = 1 ]; then lstore=keyring
             note="login is in the OS credential store (Codex Auth): default is codex login --device-auth on the box; copying it is opt-in (export_secrets.sh keychain:codex)"; fi ;;
    claude) [ -f "${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json" ] && add "../.claude.json"
            [ -f "$d/.credentials.json" ] && lstore="file"
            if [ "$os" = macos ] && [ "$lstore" = none ]; then
              # Existence check only (no -w/-g: the secret is never read).
              if [ "$(store_present 'Claude Code-credentials')" = 1 ]; then lstore=keychain
                note="login is in the macOS Keychain: default is claude auth login on the box; copying it is opt-in (export_secrets.sh keychain:claude)"
              else note="no login found (macOS keeps it in the Keychain)"; fi
            fi ;;
    pi|omp) [ "$has_auth" = 1 ] && lstore="file" ;;
  esac
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
    if ! command -v sqlite3 >/dev/null 2>&1; then
      note="${note}${note:+; }sqlite3 not found: omp login status unknown"
    elif [ -z "$rows" ]; then
      note="${note}${note:+; }omp has no saved login on this computer (or agent.db could not be read)"
    elif [ -n "$rows" ] && [ -z "$good" ]; then imp=0; fi
  fi
  case "$c" in codex) [ -n "${CODEX_HOME:-}" ] && [ "${CODEX_HOME%/}" != "$HOME/.codex" ] && note="CODEX_HOME is set: copy per file, not with bundle_local.sh" ;;
               claude) [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ "${CLAUDE_CONFIG_DIR%/}" != "$HOME/.claude" ] && note="CLAUDE_CONFIG_DIR is set: copy per file, not with bundle_local.sh" ;; esac
  [ "$first" = 1 ] || printf ','
  first=0
  printf '"%s":{"installed":%s,"version":"%s","dir":"%s","has_auth":%s,"importable_auth":%s,"has_config":%s,"login_store":"%s","present":[%s],"symlinks":%s,"auth_status":{%s},"note":"%s"}' \
    "$c" "$(b $inst)" "$(esc "$v")" "$(esc "$d")" "$(b $has_auth)" "$(b $imp)" "$(b $has_conf)" "$lstore" "$present" "$links" "$astat" "$(esc "$note")"
done
printf '}'
# ---- name-only secret inventory
raw=$(scan_secrets)
scanner=$(printf '%s\n' "$raw" | sed -n 's/^SCANNER //p'); [ -n "$scanner" ] || scanner=none
names=$(printf '%s\n' "$raw" | sed -n 's/^NAMES //p' | tr ' ' '\n' | grep -E '^[A-Za-z_][A-Za-z0-9_]*$' | sort -u | tr '\n' ' ')
inv=$(printf '%s\n' "$raw" | sed -n '/^{/p' | head -n 1)
case "$inv" in "{"*"}") ;; *) inv='{}' ;; esac
es=""
# shellcheck disable=SC2086
for l in $(env_set $names | sort -u -k1,1 | tr ' ' '='); do es="$es${es:+,}\"${l%%=*}\":\"${l#*=}\""; done
st=""
for svc in 'Claude Code-credentials' 'Codex Auth' 'Codex MCP Credentials'; do
  st="$st${st:+,}\"$svc\":$(b "$(store_present "$svc")")"
done
printf ',"secrets":{"scanner":"%s","inventory":%s,"env_set":{%s},"stores":{%s}}}\n' "$scanner" "$inv" "$es" "$st"
rm -rf "$SEC"
