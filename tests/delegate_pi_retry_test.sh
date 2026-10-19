#!/usr/bin/env bash
# delegate-task: per-run pi retry and conn_error resume. Offline: mktemp HOME, fake pi on PATH; never touches the real ~/.pi.
set -u
S="$(cd "$(dirname "$0")/.." && pwd)/skills/delegate-task/scripts"
T=$(mktemp -d); export HOME="$T/home"; mkdir -p "$HOME/.pi/agent" "$HOME/bin"
export AGENT_RUNS_ROOT="$T/runs"; export PATH="$HOME/bin:/usr/bin:/bin"
echo '{"retry":{"enabled":false,"maxRetries":7},"theme":"dark"}' > "$HOME/.pi/agent/settings.json"
# shellcheck disable=SC2034  # used inside ck eval strings
SUM0=$(sha256sum "$HOME/.pi/agent/settings.json")
cat > "$HOME/bin/pi" <<'PI'
#!/usr/bin/env bash
case " $* " in *" --help "*) echo "  --approve, -a  Trust project-local files for this run"; exit 0;; esac
{ echo "args: $*"; echo "approve: $(case " $* " in *" --approve "*) echo yes;; *) echo no;; esac)";
  echo "settings: $(cat .pi/settings.json 2>/dev/null || echo none)"; echo "gitignore: $(tr '\n' ' ' < .pi/.gitignore 2>/dev/null || echo none)"
  echo "gitstatus: $(git status --porcelain 2>/dev/null | tr '\n' ' ')"; } > "$FAKE_LOG"
echo '{"type":"session","id":"s1"}'
am() { printf '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%s"}],"stopReason":"%s","errorMessage":%s,"provider":"openai-codex","model":"m"}}\n' "$1" "$2" "$3"; }
case "$FAKE_MODE" in
  ok) am "all done" stop null;;
  ws) am "partial essay about rate limits and quota" error '"WebSocket error"';;
  idle) am "" error '"WebSocket idle timeout after 300000ms"';;
  auth) am "" error '"Invalid API key"';;
  retried) am "part" error '"WebSocket error"'; echo '{"type":"auto_retry_start","attempt":1}'; am "full answer" stop null;;
esac
exit 0
PI
chmod +x "$HOME/bin/pi"
REPO="$T/repo"; git init -q "$REPO"; git -C "$REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
pass=0 fail=0; ck() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
run() { FAKE_MODE=$1 FAKE_LOG="$T/$2.log" bash "$S/run-agent.sh" --agent pi --id "$2" "${@:3}" --prompt "hi" > "$T/$2.json" 2>/dev/null; }
m() { python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m.get(sys.argv[2]))' "$AGENT_RUNS_ROOT/$1/meta.json" "$2"; }

run ok r-ok --cwd "$REPO"
ck "ok: status ok" '[ "$(m r-ok status)" = ok ]'
ck "ok: --approve passed" 'grep -q "approve: yes" $T/r-ok.log'
ck "ok: run settings = retry only" 'grep -q "settings: {\"retry\": {\"enabled\": true}}" $T/r-ok.log'
ck "ok: files git-ignored during run" 'grep -q "gitstatus: $" $T/r-ok.log'
ck "ok: pi_retry=on" '[ "$(m r-ok pi_retry)" = on ]'
ck "ok: .pi removed after run" '[ ! -e $AGENT_RUNS_ROOT/r-ok/wt/.pi ]'
ck "ok: changed_files empty" '[ "$(m r-ok changed_files)" = "[]" ]'

run ws r-ws --cwd "$REPO"
ck "ws: exit0+stopReason error -> error" '[ "$(m r-ws status)" = error ]'
ck "ws: error_kind conn_error" '[ "$(m r-ws error_kind)" = conn_error ]'
ck "ws: resumable" '[ "$(m r-ws resumable)" = True ]'
ck "ws: not rate_limited despite partial text" '[ "$(m r-ws status)" != rate_limited ]'
ck "ws: summary starts with error" 'grep -q "\"summary\": \"error: WebSocket error" $T/r-ws.json'
L=$(bash "$S/supervise.sh" r-ws 60 --new --interval 1); echo "  supervise: $L"
ck "ws: supervise conn_error resumable=1" 'case "$L" in *"status=failed next=return"*"reason=conn_error"*"resumable=1"*) true;; *) false;; esac'
# shellcheck disable=SC2034
D=$(bash "$S/resume-run.sh" r-ws --kill --dry-run 2>&1); 
ck "ws: resume-run accepts it" 'printf "%s" "$D" | grep -q "run-agent.sh"'

run idle r-idle
ck "idle: conn_error (work kind)" '[ "$(m r-idle error_kind)" = conn_error ]'
ck "idle: no .pi artifact" '! grep -q "\.pi" $T/r-idle.json'
run auth r-auth
ck "auth: error, not conn_error" '[ "$(m r-auth status)" = error ] && [ "$(m r-auth error_kind)" = None ]'
L=$(bash "$S/supervise.sh" r-auth 60 --new --interval 1)
ck "auth: supervise resumable=0" 'case "$L" in *"reason=error"*"resumable=0"*) true;; *) false;; esac'
run retried r-ret
ck "retried: last message wins -> ok" '[ "$(m r-ret status)" = ok ] && grep -q "full answer" $AGENT_RUNS_ROOT/r-ret/result.md'

# existing project settings: never clobbered, no --approve
mkdir -p "$REPO/.pi"; echo '{"theme":"light"}' > "$REPO/.pi/settings.json"; git -C "$REPO" add .pi; git -C "$REPO" -c user.name=t -c user.email=t@t commit -qm pi
run ok r-proj --cwd "$REPO"
ck "proj: existing settings untouched" 'grep -q "settings: {\"theme\":\"light\"}" $T/r-proj.log && [ "$(cat $AGENT_RUNS_ROOT/r-proj/wt/.pi/settings.json)" = "{\"theme\":\"light\"}" ]'
ck "proj: no --approve" 'grep -q "approve: no" $T/r-proj.log'
ck "proj: pi_retry skipped" 'case "$(m r-proj pi_retry)" in skipped*) true;; *) false;; esac'
# bare .pi dir with other tracked files: dir kept, our files removed
git -C "$REPO" rm -q .pi/settings.json; mkdir -p "$REPO/.pi"; echo x > "$REPO/.pi/notes.txt"; git -C "$REPO" add .pi; git -C "$REPO" -c user.name=t -c user.email=t@t commit -qm notes
run ok r-bare --cwd "$REPO"
ck "bare: retry on, notes kept" '[ "$(m r-bare pi_retry)" = on ] && [ -f $AGENT_RUNS_ROOT/r-bare/wt/.pi/notes.txt ] && [ ! -e $AGENT_RUNS_ROOT/r-bare/wt/.pi/settings.json ]'
# .agents/skills ancestor
mkdir -p "$T/anc/.agents/skills" "$T/anc/repo2"; git init -q "$T/anc/repo2"; git -C "$T/anc/repo2" -c user.name=t -c user.email=t@t commit -q --allow-empty -m i
AGENT_RUNS_ROOT="$T/anc/runs" run ok r-anc --cwd "$T/anc/repo2"
ck "anc: .agents/skills -> skipped" 'grep -q "approve: no" $T/r-anc.log'
# review: in place, never written
run ok r-rev --review "$REPO"
ck "review: no --approve, unchanged" 'grep -q "approve: no" $T/r-rev.log && [ "$(m r-rev review_unchanged)" = True ]'
ck "HOME pi settings untouched" '[ "$(sha256sum "$HOME/.pi/agent/settings.json")" = "$SUM0" ]'
echo "pass=$pass fail=$fail"; RC=$([ "$fail" = 0 ] && echo 0 || echo 1)
for w in "$AGENT_RUNS_ROOT"/*/wt; do [ -d "$w" ] && git -C "$REPO" worktree remove --force "$w" 2>/dev/null; done
rm -rf "$T"
exit "$RC"
