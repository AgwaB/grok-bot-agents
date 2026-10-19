#!/usr/bin/env bash
# Box-side Tailscale setup for the return channel. No root, idempotent; re-run any time.
#   setup-tailscale.sh [--hostname NAME] [--upgrade] [--wait-login [SECS]] [--authkey-env VAR]
#       install (if missing) → start tailscaled (via bridge-serve.sh) → log in (if needed) → bridge up → print status
#   setup-tailscale.sh --status                      print status only (no changes)
#   setup-tailscale.sh --uninstall [--purge] [--dry-run]
#       stop tailscaled, remove binaries/symlinks; --purge also deletes the login state
# Machine-readable lines on stdout (KEY=value): VERSION, LOGIN_URL, TAILNET_IP, DNS_NAME, RESULT_URL, STATUS.
#   STATUS=ready | needs_login | not_installed | stopped | error
# Exit codes: 0 ready (or uninstall done), 1 error / not installed / stopped, 2 usage, 3 needs login (send LOGIN_URL to the user), 4 login wait timed out.
# Installs the static build from pkgs.tailscale.com/stable (sha256 checked) to ~/.local/opt/tailscale_<ver>_<arch>,
# symlinked as ~/.local/opt/tailscale and ~/.local/bin/{tailscale,tailscaled}. tailscaled runs in userspace mode
# (state ~/.local/state/tailscale, SOCKS5 localhost:1055). An auth key is passed to tailscale as a 600 temp file, never argv.
# Env: TS_STATE, GROK_BRIDGE_PORT (8787), TS_PKGS (https://pkgs.tailscale.com/stable)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVE="$HERE/bridge-serve.sh"
BIN="$HOME/.local/bin"; OPT="$HOME/.local/opt"
TS_STATE="${TS_STATE:-$HOME/.local/state/tailscale}"; export TS_STATE
TS_SOCK="$TS_STATE/tailscaled.sock"
TS="$BIN/tailscale"; export TAILSCALED="$BIN/tailscaled"
PORT="${GROK_BRIDGE_PORT:-8787}"
PKGS="${TS_PKGS:-https://pkgs.tailscale.com/stable}"
HOSTNAME_WANT="grokbot-box"; UPGRADE=0; WAIT=0; WAIT_SECS=300; AUTHKEY_ENV=""
MODE=setup; PURGE=0; DRY=0

die() { echo "error: $*" >&2; echo "STATUS=error"; exit 1; }
log() { echo "[setup-tailscale] $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) HOSTNAME_WANT="${2:-}"; shift ;;
    --upgrade) UPGRADE=1 ;;
    --wait-login) WAIT=1; case "${2:-}" in ''|-*|*[!0-9]*) ;; *) WAIT_SECS="$2"; shift ;; esac ;;
    --authkey-env) AUTHKEY_ENV="${2:-}"; shift ;;
    --status) MODE=status ;;
    --uninstall) MODE=uninstall ;;
    --purge) PURGE=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$HOSTNAME_WANT" in ''|*[!A-Za-z0-9-]*) echo "--hostname: letters, digits and '-' only" >&2; exit 2 ;; esac
case "$AUTHKEY_ENV" in *[!A-Za-z0-9_]*) echo "--authkey-env needs a variable name" >&2; exit 2 ;; esac

tsc() { "$TS" --socket="$TS_SOCK" "$@"; }
installed() { [ -x "$TS" ] && [ -x "$TAILSCALED" ]; }
installed_version() { installed && "$TS" version 2>/dev/null | head -n1; }

# ts_state -> "BackendState<US>AuthURL<US>IPv4<US>DNSName" (US = \x1f, so empty fields survive read)
ts_state() {
  tsc status --json 2>/dev/null | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: print("\x1f\x1f\x1f"); sys.exit()
s = d.get("Self") or {}
ip = next((a for a in s.get("TailscaleIPs") or [] if "." in a), "")
print("\x1f".join([d.get("BackendState", ""), d.get("AuthURL", ""), ip, (s.get("DNSName") or "").rstrip(".")]))'
}
read_state() { IFS=$'\x1f' read -r B_STATE B_AUTH B_IP B_DNS <<<"$(ts_state)"; }

print_status() {
  local v; v=$(installed_version)
  if ! installed; then echo "STATUS=not_installed"; return 1; fi
  echo "VERSION=$v"
  if ! [ -S "$TS_SOCK" ] || ! tsc status --json >/dev/null 2>&1; then echo "STATUS=stopped"; return 1; fi
  read_state
  if [ "$B_STATE" = Running ]; then
    echo "TAILNET_IP=$B_IP"; echo "DNS_NAME=$B_DNS"
    if curl -fsS -m 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      echo "RESULT_URL=http://$B_IP:$PORT"
    else
      log "bridge_server is not answering on 127.0.0.1:$PORT (bridge-serve.sh start)"
    fi
    echo "STATUS=ready"; return 0
  fi
  [ -n "$B_AUTH" ] && echo "LOGIN_URL=$B_AUTH"
  log "backend state: ${B_STATE:-unknown}"
  echo "STATUS=needs_login"; return 3
}

detect_arch() {
  [ "$(uname -s)" = Linux ] || die "box-side setup is Linux only (got $(uname -s))"
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    armv6*|armv7*|armhf) echo arm ;;
    i386|i686) echo 386 ;;
    riscv64) echo riscv64 ;;
    *) die "unsupported arch $(uname -m)" ;;
  esac
}

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

install_ts() {
  local arch meta ver tgz want got tmp dest
  arch=$(detect_arch)
  meta=$(curl -fsSL -m 30 "$PKGS/?mode=json") || die "can't reach $PKGS"
  read -r ver tgz <<<"$(printf '%s' "$meta" | python3 -c '
import json, sys
d = json.load(sys.stdin); a = sys.argv[1]
print(d.get("TarballsVersion") or d.get("Version", ""), (d.get("Tarballs") or {}).get(a, ""))' "$arch")"
  [ -n "$ver" ] && [ -n "$tgz" ] || die "no stable tarball for $arch at $PKGS"
  if installed && [ "$(installed_version)" = "$ver" ]; then log "already on $ver"; return 0; fi
  log "installing tailscale $ver ($arch)"
  tmp=$(mktemp -d); TMPD="$tmp"; trap 'rm -rf "$TMPD"' EXIT
  curl -fsSL -m 600 -o "$tmp/$tgz" "$PKGS/$tgz" || die "download failed: $PKGS/$tgz"
  want=$(curl -fsSL -m 30 "$PKGS/$tgz.sha256" | tr -d '[:space:]') || die "can't fetch $tgz.sha256"
  printf '%s' "$want" | grep -Eq '^[0-9a-f]{64}$' || die "bad sha256 file for $tgz"
  got=$(sha256_of "$tmp/$tgz")
  [ "$got" = "$want" ] || die "sha256 mismatch for $tgz (got $got, want $want)"
  tar -xzf "$tmp/$tgz" -C "$tmp" || die "extract failed"
  dest="$OPT/${tgz%.tgz}"
  mkdir -p "$OPT" "$BIN"
  rm -rf "$dest.new"; mkdir -p "$dest.new"
  cp "$tmp/${tgz%.tgz}/tailscale" "$tmp/${tgz%.tgz}/tailscaled" "$dest.new/" || die "unexpected tarball layout"
  chmod 755 "$dest.new/tailscale" "$dest.new/tailscaled"
  rm -rf "$dest"; mv "$dest.new" "$dest"
  ln -sfn "$(basename "$dest")" "$OPT/tailscale"
  ln -sfn "$OPT/tailscale/tailscale" "$BIN/tailscale"
  ln -sfn "$OPT/tailscale/tailscaled" "$BIN/tailscaled"
  log "installed $("$TS" version | head -n1) → $dest"
  rm -rf "$tmp"
  return 10   # tells the caller a (re)install happened
}

ensure_bashrc() {
  local rc="$HOME/.bashrc"
  grep -q 'grok-bot-agents-bridge' "$rc" 2>/dev/null && return 0
  {
    echo '# grok-bot-agents-bridge: keep tailscaled (userspace) + bridge return channel running (idempotent, silent)'
    echo "[ -x $SERVE ] && \\"
    echo "  ( $SERVE start >/dev/null 2>&1 & )"
  } >>"$rc"
  log "added keep-alive line to $rc"
}

start_login() {
  local keyfile=""
  mkdir -p "$TS_STATE"
  if [ -n "$AUTHKEY_ENV" ]; then
    local key="${!AUTHKEY_ENV:-}"
    [ -n "$key" ] || die "\$$AUTHKEY_ENV is empty"
    keyfile=$(mktemp "$TS_STATE/.authkey.XXXXXX"); chmod 600 "$keyfile"
    printf '%s' "$key" >"$keyfile"; unset key
    log "logging in with the auth key from \$$AUTHKEY_ENV"
    tsc up --hostname "$HOSTNAME_WANT" --auth-key "file:$keyfile" --timeout 60s >>"$TS_STATE/up.log" 2>&1
    local rc=$?; rm -f "$keyfile"
    [ $rc = 0 ] || die "tailscale up with auth key failed (see $TS_STATE/up.log)"
    return 0
  fi
  read_state
  if [ -z "$B_AUTH" ]; then
    # `tailscale up` blocks until login; run it detached and read the URL from status/log
    : >"$TS_STATE/up.log"
    setsid nohup "$TS" --socket="$TS_SOCK" up --hostname "$HOSTNAME_WANT" >>"$TS_STATE/up.log" 2>&1 </dev/null &
    for _ in $(seq 1 40); do
      read_state
      [ "$B_STATE" = Running ] && return 0
      [ -n "$B_AUTH" ] && break
      B_AUTH=$(grep -Eo 'https://[^[:space:]]+' "$TS_STATE/up.log" 2>/dev/null | head -n1)
      [ -n "$B_AUTH" ] && break
      sleep 0.5
    done
  fi
  [ -n "$B_AUTH" ] || die "no login URL from tailscale up (see $TS_STATE/up.log)"
  echo "LOGIN_URL=$B_AUTH"
}

wait_login() {
  local end=$(( $(date +%s) + WAIT_SECS ))
  log "waiting up to ${WAIT_SECS}s for the login to be approved"
  while [ "$(date +%s)" -lt "$end" ]; do
    read_state; [ "$B_STATE" = Running ] && return 0
    sleep 3
  done
  return 1
}

uninstall() {
  local run=""; [ "$DRY" = 1 ] && run="echo [dry-run]"
  [ -x "$SERVE" ] && $run "$SERVE" ts-stop
  for f in "$BIN/tailscale" "$BIN/tailscaled"; do
    if [ -L "$f" ]; then case "$(readlink "$f")" in "$OPT"/tailscale*) $run rm -f "$f" ;; esac; fi
  done
  for d in "$OPT"/tailscale "$OPT"/tailscale_*; do [ -e "$d" ] || [ -L "$d" ] && $run rm -rf "$d"; done
  if [ "$PURGE" = 1 ]; then $run rm -rf "$TS_STATE"; else log "kept login state in $TS_STATE (--purge deletes it)"; fi
  pkill -u "$(id -u)" -f "tailscale --socket=$TS_SOCK up" 2>/dev/null   # a pending login, if any
  log "the ~/.bashrc keep-alive line stays (it skips tailscaled when the binary is gone)"
  [ "$DRY" = 1 ] && { echo "STATUS=dry_run"; return 0; }
  echo "STATUS=not_installed"
}

case "$MODE" in
  status) print_status; exit $? ;;
  uninstall) uninstall; exit 0 ;;
esac

# --- setup ---
command -v python3 >/dev/null || die "python3 is required"
command -v curl >/dev/null || die "curl is required"
REINSTALLED=0
if ! installed; then
  install_ts; [ $? = 10 ] && REINSTALLED=1
elif [ "$UPGRADE" = 1 ]; then
  install_ts; [ $? = 10 ] && REINSTALLED=1
else
  log "tailscale $(installed_version) already installed (--upgrade to update)"
fi
if [ "$REINSTALLED" = 1 ] && pgrep -u "$(id -u)" -f "tailscaled .*--socket=$TS_SOCK" >/dev/null; then
  log "restarting tailscaled on the new version (login is kept)"
  "$SERVE" ts-stop >/dev/null || die "could not stop the old tailscaled"
fi
"$SERVE" ts-start || die "tailscaled did not start"
ensure_bashrc

read_state
if [ "$B_STATE" != Running ]; then
  start_login
  read_state
  if [ "$B_STATE" != Running ]; then
    if [ "$WAIT" = 1 ]; then
      wait_login || { echo "STATUS=needs_login"; log "not approved within ${WAIT_SECS}s; re-run with --wait-login"; exit 4; }
    else
      echo "STATUS=needs_login"; exit 3
    fi
  fi
fi
"$SERVE" start >/dev/null || log "bridge_server did not start (see ~/.local/state/grok-bot-agents/)"
print_status
