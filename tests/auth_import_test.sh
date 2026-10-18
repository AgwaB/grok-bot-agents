#!/usr/bin/env bash
# End-to-end auth-import test with FAKE secrets in temp HOMEs (never touches the real box or computer logins).
# Every secret value in the fixture contains a canary string. All script output (stdout + stderr) is captured, and the
# test fails if the canary appears anywhere in it. Also checks: name-only detect, consent-only export, bundle defaults,
# ~/.agent-keys merge (600, no duplicates, idempotent), !command -> env-name rewrites, Codex file-store pin, Claude
# account-field merge on an opted-in login copy, and stage/secret cleanup.
#   bash tests/auth_import_test.sh        (exit 0 = pass)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; S="$ROOT/skills/import-agents/scripts"
CANARY="CANARY$RANDOM$RANDOM"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export HOME="$T/runner"; mkdir -p "$HOME"
SRC="$T/src"; BOX="$T/box"; STUB="$T/stub"; MAC="$T/mac"; LOG="$T/log.txt"; mkdir -p "$SRC" "$BOX" "$STUB" "$MAC"
fail=0; checks=0; ok() { echo "ok   $1"; }; bad() { echo "FAIL $1"; fail=1; }
check() { checks=$((checks + 1)); if eval "$2" >"$T/check.out" 2>&1; then ok "$1"; else bad "$1"; fi; }
bash "$ROOT/tests/fake_home.sh" "$SRC" "$CANARY"
printf 'export DEEPSEEK_API_KEY=%s-envds\necho "rc noise"\n' "$CANARY" >"$SRC/.bashrc"
# stubs: fake macOS (uname, security), password tools, node
printf '#!/bin/sh\necho Darwin\n' >"$MAC/uname"
cat >"$STUB/security" <<SH
#!/bin/sh
for a in "\$@"; do case "\$a" in -w|-ws|-wa) printf '{"claudeAiOauth":{"accessToken":"%s-kc"}}\n' "$CANARY"; exit 0 ;; esac; done
case "\$*" in *"Claude Code-credentials"*|*"Codex Auth"*) printf 'attributes:\n    "acct"<blob>="tester"\n'; exit 0 ;; esac
exit 44
SH
printf '#!/bin/sh\necho %s-passval\n' "$CANARY" >"$STUB/pass"; printf '#!/bin/sh\necho %s-opval\n' "$CANARY" >"$STUB/op"
# Block every executable that could install, refresh, or contact a real credential store.
for tool in claude codex pi omp npm npx uv uvx git secret-tool; do
  printf '#!/bin/sh\nexit 127\n' >"$STUB/$tool"
done
chmod +x "$MAC/uname" "$STUB"/*; ln -s "$(command -v node)" "$STUB/node"
for h in "$SRC" "$BOX"; do mkdir -p "$h/.local"; ln -s "$STUB" "$h/.local/bin"; done
# Emulate login startup using only the fake rc file, never /etc/profile or an OS account home.
# shellcheck disable=SC2016 # These variables belong to the stub, not this test process.
printf '#!/bin/sh\n. "$HOME/.bashrc"\n/bin/sh -c "$2"\n' >"$STUB/login-shell"
chmod +x "$STUB/login-shell"
SRCENV=(env -i HOME="$SRC" USER=tester SHELL="$STUB/login-shell" PATH="$MAC:$STUB:/usr/bin:/bin" OPENAI_API_KEY="$CANARY-proc")
BOXENV=(env -i HOME="$BOX" PATH="$STUB:/usr/bin:/bin" AGENT_IMPORT_STAGE="$BOX/stage")
run() { local n=$1; shift; echo "== $n" >>"$LOG"; "$@" >"$T/$n.out" 2>>"$LOG"; local rc=$?; cat "$T/$n.out" >>"$LOG"; return $rc; }

# 1. detect (macOS mode, Linux mode): names only
run detect_mac "${SRCENV[@]}" sh -s <"$S/detect_local.sh"
check "detect (macOS) is valid JSON with a secrets inventory" "python3 -c 'import json,sys;d=json.load(open(\"$T/detect_mac.out\"));assert d[\"secrets\"][\"scanner\"]==\"node\" and d[\"secrets\"][\"inventory\"][\"items\"]' "
check "detect sees the login-shell env name and the Keychain items (presence only)" "python3 -c 'import json;d=json.load(open(\"$T/detect_mac.out\"))[\"secrets\"];assert d[\"env_set\"].get(\"DEEPSEEK_API_KEY\")==\"login_shell\" and d[\"env_set\"].get(\"OPENAI_API_KEY\")==\"process\" and d[\"stores\"][\"Claude Code-credentials\"]'"
run detect_linux env -i HOME="$SRC" PATH="$STUB:/usr/bin:/bin" sh -s -- --no-login-shell codex pi <"$S/detect_local.sh"
check "detect (Linux, --no-login-shell) is valid JSON" "python3 -c 'import json;d=json.load(open(\"$T/detect_linux.out\"));assert d[\"os\"]==\"linux\" and \"DEEPSEEK_API_KEY\" not in d[\"secrets\"][\"env_set\"]'"
PWSH=${PWSH:-$(command -v pwsh || true)}
if [ -n "$PWSH" ]; then  # Windows twin, run by pwsh on the same fake home
  run detect_ps1 env -i HOME="$SRC" USERPROFILE="$SRC" PATH="$STUB:/usr/bin:/bin" OPENAI_API_KEY="$CANARY-proc" "$PWSH" -NoProfile -NonInteractive -File "$S/detect_local.ps1"
  check "detect_local.ps1: same inventory size as detect_local.sh" "python3 -c 'import json;a=json.load(open(\"$T/detect_ps1.out\"));b=json.load(open(\"$T/detect_mac.out\"));assert a[\"os\"]==\"windows\" and len(a[\"secrets\"][\"inventory\"][\"items\"])==len(b[\"secrets\"][\"inventory\"][\"items\"])'"
else echo "skip detect_local.ps1 (no pwsh; set PWSH=/path/to/pwsh)"; fi
# 2. export: refuses unknown items, writes nothing
run export_bad "${SRCENV[@]}" sh -s -- env:OPENAI_API_KEY 'file:/etc/passwd' <"$S/export_secrets.sh"; rc=$?
check "export refuses an item off the allowed list (exit 2, nothing written)" "[ $rc = 2 ] && [ ! -e '$SRC/.agent-import-secrets' ]"
run exp "${SRCENV[@]}" sh -s -- env:OPENAI_API_KEY env:DEEPSEEK_API_KEY env:NOT_SET_KEY keychain:claude keychain:codex \
  resolve:pi:auth.json:deepseek=PI_DEEPSEEK_KEY resolve:pi:models.json:cmdp=CMDP_API_KEY resolve:omp:models.yml:vault=VAULT_API_KEY <"$S/export_secrets.sh"
check "export: all consented items ok, unset one missing" "python3 -c 'import json;d=json.load(open(\"$T/exp.out\"));st={i[\"item\"]:i[\"status\"] for i in d[\"items\"]};assert st.pop(\"env:NOT_SET_KEY\")==\"missing\" and set(st.values())=={\"ok\"},st'"
check "export files are 600 in a 700 dir" "[ \$(stat -c %a '$SRC/.agent-import-secrets') = 700 ] && [ -z \"\$(find '$SRC/.agent-import-secrets' -type f ! -perm 600)\" ]"
# 3. bundle: defaults leave rotating logins out; opt-in puts them in; secrets dir bundled then deleted
mv "$SRC/.agent-import-secrets" "$T/secrets.keep"   # the default path has no consented exports
run bundle_default "${SRCENV[@]}" sh -s -- codex claude pi omp <"$S/bundle_local.sh"
tar tzf "$SRC/agent-import.tgz" >"$T/members_default.txt"
check "default bundle leaves Claude/Codex logins and pi .env out" "! grep -qxE '\\.claude/\\.credentials\\.json|\\.codex/auth\\.json|\\.codex/\\.credentials\\.json|\\.pi/agent/\\.env' '$T/members_default.txt'"
check "default bundle has generic pi package auth files, not legacy oauth.json" "grep -qx .pi/agent/my-pkg-token.json '$T/members_default.txt' && grep -qx .pi/agent/web-search.json '$T/members_default.txt' && ! grep -q oauth.json '$T/members_default.txt'"
check "default bundle is 600" "[ \$(stat -c %a '$SRC/agent-import.tgz') = 600 ]"
check "no SSH key in the bundle" "! grep -q .ssh '$T/members_default.txt'"
mv "$SRC/agent-import.tgz" "$T/bundle_default.tgz"
# default path on a second box: no rotating login copied -> box-login warnings, no two-machine warning
BOX2="$T/box2"; mkdir -p "$BOX2/.local"; ln -s "$STUB" "$BOX2/.local/bin"
run stage_default env -i HOME="$BOX2" PATH="$STUB:/usr/bin:/bin" AGENT_IMPORT_STAGE="$BOX2/stage" python3 "$S/stage_tar.py" "$T/bundle_default.tgz"
for c in claude codex; do run "default_$c" env -i HOME="$BOX2" PATH="$STUB:/usr/bin:/bin" AGENT_IMPORT_STAGE="$BOX2/stage" bash "$S/apply_import.sh" "$c" --src-home "$SRC" --no-verify; done
check "default import: Claude/Codex say 'log in on the box', no login files, no two-machine warning" "grep -q 'Claude login not copied (default)' '$T/default_claude.out' && grep -q 'Codex login not copied (default)' '$T/default_codex.out' && [ ! -e '$BOX2/.claude/.credentials.json' ] && [ ! -e '$BOX2/.codex/auth.json' ] && grep -q '\"two_machine_warning\": null' '$T/default_claude.out'"
check "default import: Claude account fields are not merged without a copied login" "python3 -c 'import json;d=json.load(open(\"$BOX2/.claude.json\"));assert \"oauthAccount\" not in d and \"mcpServers\" in d'"
cp -r "$T/secrets.keep" "$SRC/.agent-import-secrets"
run bundle_optin "${SRCENV[@]}" sh -s -- --copy-login claude,codex --include .pi/agent/.env codex claude pi omp <"$S/bundle_local.sh"
tar tzf "$SRC/agent-import.tgz" >"$T/members.txt"
check "opt-in bundle has the logins, MCP OAuth file, pi .env and the exported secrets" "grep -qx .claude/.credentials.json '$T/members.txt' && grep -qx .codex/.credentials.json '$T/members.txt' && grep -qx .pi/agent/.env '$T/members.txt' && grep -qx .agent-import-secrets/secrets.env '$T/members.txt'"
check "opt-in bundle deleted ~/.agent-import-secrets" "[ ! -e '$SRC/.agent-import-secrets' ]"
mv "$SRC/agent-import.tgz" "$T/bundle.tgz"
# 4. box: stage, keys, apply (no CLIs installed: --no-verify), parity
run stage "${BOXENV[@]}" python3 "$S/stage_tar.py" "$T/bundle.tgz"
printf 'export OPENAI_API_KEY=old\nexport KEEP_ME=1\nexport OPENAI_API_KEY=olddup\n' >"$BOX/.agent-keys"; chmod 600 "$BOX/.agent-keys"
run keys_dry "${BOXENV[@]}" bash "$S/apply_import.sh" keys --dry-run
check "keys --dry-run lists names only and changes nothing" "grep -q OPENAI_API_KEY '$T/keys_dry.out' && grep -q '^export OPENAI_API_KEY=old$' '$BOX/.agent-keys'"
for c in codex claude pi omp; do run "dry_$c" "${BOXENV[@]}" bash "$S/apply_import.sh" "$c" --src-home "$SRC" --dry-run; done
for c in codex claude pi omp; do run "apply_$c" "${BOXENV[@]}" bash "$S/apply_import.sh" "$c" --src-home "$SRC" --overwrite --no-verify; done
check ".agent-keys is 600, merged, no duplicate names, other lines kept" "[ \$(stat -c %a '$BOX/.agent-keys') = 600 ] && [ \$(grep -c '^export OPENAI_API_KEY=' '$BOX/.agent-keys') = 1 ] && grep -q '^export KEEP_ME=1' '$BOX/.agent-keys' && grep -q '^export VAULT_API_KEY=' '$BOX/.agent-keys' && grep -q '^export DEEPSEEK_API_KEY=' '$BOX/.agent-keys'"
check "the merged values are the source's (sourced in a subshell, compared, not printed)" "bash -c '. \"$BOX/.agent-keys\"; [ \"\$OPENAI_API_KEY\" = \"$CANARY-proc\" ] && [ \"\$CMDP_API_KEY\" = \"$CANARY-passval\" ]'"
check "staged secrets.env was removed after the merge" "[ ! -e '$BOX/stage/_secrets/secrets.env' ]"
check "codex config.toml pinned to file stores" "grep -q '^cli_auth_credentials_store = \"file\"' '$BOX/.codex/config.toml' && grep -q '^mcp_oauth_credentials_store = \"file\"' '$BOX/.codex/config.toml'"
check "codex keychain export landed as auth.json (600)" "[ \$(stat -c %a '$BOX/.codex/auth.json') = 600 ]"
check "pi/omp !command keys now point at env names" "python3 -c 'import json;assert json.load(open(\"$BOX/.pi/agent/models.json\"))[\"providers\"][\"cmdp\"][\"apiKey\"]==\"CMDP_API_KEY\"' && grep -q 'apiKey: VAULT_API_KEY' '$BOX/.omp/agent/models.yml'"
check "copied Claude login also merged oauthAccount + hasCompletedOnboarding" "python3 -c 'import json;d=json.load(open(\"$BOX/.claude.json\"));assert d[\"hasCompletedOnboarding\"] is True and \"oauthAccount\" in d and \"userID\" not in d and \"mcpServers\" in d'"
check "stage dirs removed" "[ ! -e '$BOX/stage/codex' ] && [ ! -e '$BOX/stage/claude' ]"
cp "$BOX/.agent-keys" "$T/keys.before"; mkdir -p "$BOX/stage/_secrets"; grep '^export OPENAI_API_KEY=' "$BOX/.agent-keys" >"$BOX/stage/_secrets/secrets.env"
run keys_again "${BOXENV[@]}" bash "$S/apply_import.sh" keys
check "re-merging the same key is idempotent" "cmp -s '$T/keys.before' '$BOX/.agent-keys' && grep -q unchanged '$T/keys_again.out'"
run parity "${BOXENV[@]}" python3 "$S/parity_check.py" --no-run; :
check "parity auth section reports by name (unset env, missing tool, remote MCP, ToS flag)" "python3 -c 'import json;d=json.load(open(\"$T/parity.out\"));a=json.dumps({c:v[\"auth\"] for c,v in d.items()});assert \"R2_KEY\" in a and \"codex mcp login linear\" in a and \"ToS\" in a and \"apiKeyHelper\" in a, a'"
# 5. the canary must not appear in ANY script output
n=$(grep -c "$CANARY" "$LOG"); check "no secret value in any script output ($(wc -l <"$LOG") lines checked)" "[ $n = 0 ]"
# Never persist value-bearing diagnostics, even when the redaction assertion fails.
if [ -n "${KEEP_LOG:-}" ] && [ "$n" = 0 ]; then cp "$LOG" "$KEEP_LOG"; fi
if [ "$fail" = 0 ]; then echo "PASS ($checks checks)"; else echo "FAILED ($checks checks; diagnostics suppressed)"; fi
exit $fail
