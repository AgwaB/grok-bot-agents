"""Explicit, user-local installers for gba. Planning never starts a process or writes.

Downloads use HTTPS plus registry SHA-512 SRI / Node's SHA-256 manifest. npm
verifies dependency integrity; lifecycle scripts and ambient npm credentials are
disabled. Nothing here invokes a provider, login flow, shell installer, or sudo.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import runpy
import selectors
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENTS = ("claude", "codex", "pi", "omp")
PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "pi": "@earendil-works/pi-coding-agent",
    "omp": "@oh-my-pi/pi-coding-agent",
}
MIN_NODE = (22, 19, 0)
MAX_OUTPUT = 65536
MAX_DOWNLOAD = 160 * 1024 * 1024
VERSION = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-[A-Za-z0-9.-]{1,50})?")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


class SetupError(Exception):
    def __init__(self, reason, next_action="setup_repair"):
        super().__init__(reason)
        self.reason = reason
        self.next_action = next_action


def result(status="ok", reason="ok", next_action=None, requires_approval=False, **metadata) -> dict[str, Any]:
    return dict(status=status, reason=reason, next_action=next_action,
                requires_approval=requires_approval, **metadata)


def selected_agents(raw):
    if not isinstance(raw, str):
        raise SetupError("agents_required", "select_agents")
    items = raw.split(",")
    if not items or any(x not in AGENTS for x in items):
        raise SetupError("invalid_agents", "select_agents")
    return [a for a in AGENTS if a in items]


def home():
    return Path(os.path.expanduser("~")).absolute()


def user_path():
    return os.pathsep.join((str(home() / ".local/bin"), str(home() / ".bun/bin"),
                            os.environ.get("PATH", os.defpath)))


def location(path):
    """Display only a bounded executable location, never arbitrary CLI/config text."""
    if not path:
        return None
    text = str(path)
    with contextlib.suppress(ValueError):
        text = "$HOME/" + str(Path(text).relative_to(home()))
    return text if len(text) <= 300 and re.fullmatch(r"[A-Za-z0-9_./$@+ -]+", text) else "path_redacted"


def find_executable(name):
    return shutil.which(name, path=user_path())


def clean_env():
    # Do not send provider credentials, npm tokens or user-controlled node hooks
    # to installers. In particular never read ~/.npmrc or launch lifecycle scripts.
    return {"HOME": str(home()), "PATH": user_path(), "LANG": "C.UTF-8",
            "CI": "1", "NO_COLOR": "1", "NPM_CONFIG_USERCONFIG": os.devnull,
            "NPM_CONFIG_GLOBALCONFIG": os.devnull, "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false", "NPM_CONFIG_UPDATE_NOTIFIER": "false"}


def run_captured(command, *, timeout=15, env=None, cwd=None, limit=MAX_OUTPUT):
    """Drain both pipes with a hard output/time bound; never surface raw errors.

    Return (exit code or None, stdout bytes, stderr bytes, stable reason). A new
    session contains even misbehaving version commands and their children.
    """
    proc = None
    output = [bytearray(), bytearray()]
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True)
        deadline = time.monotonic() + timeout
        reason = "ok"
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout.fileno(), selectors.EVENT_READ, 0)
            sel.register(proc.stderr.fileno(), selectors.EVENT_READ, 1)
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "command_timeout"
                    break
                for key, _ in sel.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if sum(map(len, output)) > limit:
                        reason = "command_output_limit"
                        break
                if reason != "ok":
                    break
        if reason == "ok":
            try:
                proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "command_timeout"
        if reason != "ok":
            _kill(proc)
        return proc.returncode, bytes(output[0][:limit]), bytes(output[1][:limit]), reason
    except (OSError, subprocess.SubprocessError):
        if proc:
            _kill(proc)
        return None, b"", b"", "command_unavailable"
    finally:
        if proc:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()


def _kill(proc):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def probe_version(executable, *, env=None):
    if not executable:
        return {"status": "missing", "version": None}
    rc, out, err, code = run_captured([str(executable), "--version"], env=env or clean_env())
    version = None
    if code == "ok" and rc == 0:
        # Full-line allowlist, not a substring of an error containing a token.
        for line in (out + b"\n" + err).decode("utf-8", "replace").splitlines()[:12]:
            m = re.fullmatch(r"(?:node |npm |bun |pi |omp |codex(?:-cli)? |claude )?"
                             r"(v?\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[A-Za-z0-9.-]{1,50})?)"
                             r"(?: \(Claude Code\))?", line.strip(), re.I)
            if m:
                version = m.group(1).lstrip("v").split("-", 1)[0]
                break
    return {"status": "ok" if version else (code if code != "ok" else "version_unavailable"),
            "version": version}


def config_path():
    for name in ("GROK_BOT_AGENTS_CONFIG", "GROK_AGENTS_CONFIG"):
        if os.environ.get(name):
            return Path(os.path.expanduser(os.environ[name])).absolute()
    current = home() / ".grok-bot-agents/config.json"
    legacy = home() / ".grok-agents/config.json"
    return legacy if not current.exists() and legacy.exists() else current


def _helper(root):
    # exec avoids import bytecode writes (notably during strictly read-only plan).
    path = root / "skills/configure-agents/scripts/agents_config.py"
    try:
        return runpy.run_path(str(path), run_name="gba_config_defaults")
    except Exception:
        raise SetupError("config_helper_unavailable", "repair_plugin") from None


def read_config(root):
    path = config_path()
    if path.is_symlink():
        raise SetupError("config_path_unsafe", "configure_agents")
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or not isinstance(cfg.get("enabled"), list):
            raise ValueError
        if any(a not in AGENTS for a in cfg["enabled"]) or len(set(cfg["enabled"])) != len(cfg["enabled"]):
            raise ValueError
        if type(cfg.get("version")) is not int or type(cfg.get("timeout_sec")) is not int:
            raise ValueError
        if not isinstance(cfg.get("runs_dir"), str) or not cfg["runs_dir"].startswith("/"):
            raise ValueError
        agents = cfg.get("agents")
        if not isinstance(agents, dict) or any(a not in AGENTS or not isinstance(c, dict) for a, c in agents.items()):
            raise ValueError
        if any(a not in agents for a in cfg["enabled"]):
            raise ValueError
        if any(c.get("model") is not None and not isinstance(c["model"], str) for c in agents.values()):
            raise ValueError
        if any(not isinstance(c.get("key_env", "KEY"), str) or not ENV_NAME.fullmatch(c.get("key_env", "KEY"))
               for c in agents.values()):
            raise ValueError
        if _helper(root)["validate"](cfg):
            raise ValueError
        return cfg
    except SetupError:
        raise
    except Exception:
        raise SetupError("config_invalid", "configure_agents") from None


def _safe_target(path):
    """Managed writes stay below HOME, with no symlinked components."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise SetupError("install_path_unsafe")
    try:
        path.relative_to(home())
    except ValueError:
        raise SetupError("install_path_unsafe") from None
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise SetupError("install_path_unsafe")
        if parent == home():
            break
    return path


def _mkdir(path):
    _safe_target(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _atomic(path, data, mode=0o600):
    _safe_target(path)
    _mkdir(path.parent)
    fd, name = tempfile.mkstemp(prefix=".gba-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update_config(root, selected):
    # Re-read at publication, rather than saving a stale pre-install snapshot.
    cfg = read_config(root)
    defaults = _helper(root)["default_config"](selected)
    if cfg is None:
        cfg = defaults
    else:
        for a in selected:
            cfg["agents"].setdefault(a, defaults["agents"][a])
        cfg["enabled"] = [a for a in AGENTS if a in cfg["enabled"] or a in selected]
    path = config_path()
    encoded = (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode()
    if not path.exists() or path.read_bytes() != encoded:
        _atomic(path, encoded)


def base_dir():
    return home() / ".local/share/gba"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("download_origin_rejected")


def _fetch(url, maximum=MAX_DOWNLOAD):
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in ("registry.npmjs.org", "nodejs.org")
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
        raise SetupError("download_origin_rejected")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        opener.addheaders = [("User-Agent", "gba-setup/1")]
        with opener.open(url, timeout=60) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != parsed.hostname:
                raise SetupError("download_origin_rejected")
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise SetupError("download_size_limit")
            return data
    except SetupError:
        raise
    except Exception:
        raise SetupError("download_failed", "retry_setup") from None


def _json_download(url):
    try:
        return json.loads(_fetch(url, 8 * 1024 * 1024))
    except SetupError:
        raise
    except Exception:
        raise SetupError("download_metadata_invalid") from None


def registry_archive(package):
    """Authenticate metadata with TLS, then require and verify SHA-512 SRI."""
    meta = _json_download("https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@") + "/latest")
    try:
        version = meta["version"]
        if meta.get("name") != package or not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError
        dist = meta["dist"]
        integrity = dist["integrity"]
        tokens = integrity.split()
        digests = [base64.b64decode(t[7:], validate=True) for t in tokens if t.startswith("sha512-")]
        if not digests or any(len(d) != 64 for d in digests):
            raise ValueError
        url = dist["tarball"]
        if urllib.parse.urlparse(url).hostname != "registry.npmjs.org":
            raise ValueError
    except Exception:
        raise SetupError("package_integrity_missing") from None
    data = _fetch(url)
    if hashlib.sha512(data).digest() not in digests:
        raise SetupError("package_integrity_mismatch")
    return data, version, "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def _extract(data, destination, top):
    """Bounded extraction; links are deferred and cannot escape the archive root."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > 60000 or sum(m.size for m in members) > 800 * 1024 * 1024:
                raise ValueError
            links = []
            for member in members:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != top:
                    raise ValueError
                target = destination / path
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError
                    with source, open(target, "xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                elif member.issym():
                    resolved = (target.parent / member.linkname).resolve()
                    resolved.relative_to(destination / top)
                    links.append((target, member.linkname))
                else:
                    raise ValueError
            for target, link in links:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(link)
            for target, _ in links:
                target.resolve().relative_to(destination / top)
    except Exception:
        raise SetupError("archive_unsafe") from None


def _arch():
    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)


def _node_runtime():
    # Reuse a previously verified private runtime even when bot PATH omits it.
    private = _safe_target(base_dir() / "runtime/node")
    for candidate in (private / "bin/node", Path(find_executable("node") or "/nonexistent")):
        if candidate.is_file():
            found = probe_version(candidate)
            if found["version"] and tuple(map(int, found["version"].split("-")[0].split("."))) >= MIN_NODE:
                npm = candidate.parent / "npm"
                if npm.is_file():
                    return candidate, npm
    index = _json_download("https://nodejs.org/dist/index.json")
    try:
        versions = [r["version"] for r in index if re.fullmatch(r"v22\.\d+\.\d+", r["version"])
                    and tuple(map(int, r["version"][1:].split("."))) >= MIN_NODE]
        version = max(versions, key=lambda x: tuple(map(int, x[1:].split("."))))
    except Exception:
        raise SetupError("node_release_unavailable") from None
    stem = "node-" + version + "-linux-" + _arch()
    filename = stem + ".tar.xz"
    manifest = _fetch("https://nodejs.org/dist/" + version + "/SHASUMS256.txt", 1024 * 1024)
    checksums = re.findall(rb"(?m)^([0-9a-f]{64})\s+" + re.escape(filename.encode()) + rb"\r?$", manifest)
    if len(checksums) != 1:
        raise SetupError("runtime_checksum_missing")
    archive = _fetch("https://nodejs.org/dist/" + version + "/" + filename)
    if hashlib.sha256(archive).hexdigest().encode() != checksums[0]:
        raise SetupError("runtime_checksum_mismatch")
    _mkdir(private.parent)
    with tempfile.TemporaryDirectory(prefix=".node-", dir=private.parent) as temp:
        _extract(archive, Path(temp), stem)
        if not (Path(temp) / stem / "bin/node").is_file() or not (Path(temp) / stem / "bin/npm").is_file():
            raise SetupError("node_archive_invalid")
        if private.exists():
            raise SetupError("runtime_conflict")
        os.replace(Path(temp) / stem, private)
    return private / "bin/node", private / "bin/npm"


def _bun_runtime():
    private = _safe_target(base_dir() / "runtime/bun")
    if private.is_file() and private.stat().st_mode & 0o111:
        return private
    existing = find_executable("bun")
    if existing and probe_version(existing)["status"] == "ok":
        return Path(existing)
    # Official npm binary artifact: avoids executing an unchecked curl|sh installer.
    package = "@oven/bun-linux-" + ("x64-baseline" if _arch() == "x64" else "aarch64")
    archive, _, _ = registry_archive(package)
    _mkdir(private.parent)
    with tempfile.TemporaryDirectory(prefix=".bun-", dir=private.parent) as temp:
        _extract(archive, Path(temp), "package")
        binary = Path(temp) / "package/bin/bun"
        if not binary.is_file() or binary.is_symlink():
            raise SetupError("bun_archive_invalid")
        _atomic(private, binary.read_bytes(), 0o755)
    return private


def _wrapper(agent, runtime, entry, node):
    return ("#!/bin/sh\n# gba-managed-v1 " + agent + "\n"
            + "PATH=" + shlex.quote(str(node.parent)) + ':"$HOME/.local/bin:$HOME/.bun/bin:$PATH"\nexport PATH\n'
            + "exec " + shlex.quote(str(runtime)) + " " + shlex.quote(str(entry)) + ' "$@"\n').encode()


def managed_state(agent):
    prefix = _safe_target(base_dir() / "agents" / agent)
    state_path = _safe_target(prefix / "gba-install.json")
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
        wrapper = _safe_target(home() / ".local/bin" / agent)
        entry = Path(state["entry"])
        runtime = Path(state["runtime"])
        entry.resolve().relative_to(prefix.resolve())
        valid = (state.get("verified") is True and entry.is_file() and runtime.is_file() and wrapper.is_file()
                 and os.access(wrapper, os.X_OK) and not wrapper.is_symlink()
                 and hashlib.sha256(wrapper.read_bytes()).hexdigest() == state["wrapper_sha256"]
                 and hashlib.sha256(entry.read_bytes()).hexdigest() == state["entry_sha256"])
        if valid:
            return state
    except Exception:
        raise SetupError("managed_install_invalid") from None
    raise SetupError("managed_install_invalid")


def _install_agent(agent, node, npm, bun):
    prefix = base_dir() / "agents" / agent
    _safe_target(prefix)
    wrapper = home() / ".local/bin" / agent
    _safe_target(wrapper)
    if wrapper.exists() or prefix.exists():
        raise SetupError("install_target_conflict")
    archive, version, integrity = registry_archive(PACKAGES[agent])
    _mkdir(prefix.parent)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=prefix.parent) as temp:
        temp = Path(temp)
        package_file = temp / "agent.tgz"
        package_file.write_bytes(archive)
        staging = temp / "prefix"
        env = clean_env()
        env["PATH"] = str(node.parent) + os.pathsep + env["PATH"]
        env["NPM_CONFIG_CACHE"] = str(base_dir() / "npm-cache")
        command = [str(node), str(npm), "install", "--global", "--prefix", str(staging),
                   "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/",
                   str(package_file)]
        rc, _, _, code = run_captured(command, timeout=600, env=env, cwd=temp, limit=1024 * 1024)
        if code != "ok" or rc != 0:
            raise SetupError("package_install_failed", "retry_setup")
        package_root = staging / "lib/node_modules" / PACKAGES[agent]
        try:
            package = json.loads((package_root / "package.json").read_text())
            if package.get("name") != PACKAGES[agent] or package.get("version") != version:
                raise ValueError
            entry_rel = package["bin"] if isinstance(package["bin"], str) else package["bin"][agent]
            entry = (package_root / entry_rel).resolve()
            entry.relative_to(package_root.resolve())
            if not entry.is_file():
                raise ValueError
            relative = entry.relative_to(staging.resolve())
        except Exception:
            raise SetupError("package_entry_invalid") from None
        os.replace(staging, prefix)
    entry = prefix / relative
    runtime = bun if agent == "omp" else node
    content = _wrapper(agent, runtime, entry, node)
    state = {"package": PACKAGES[agent], "version": version, "integrity": integrity, "verified": False,
             "entry": str(entry), "runtime": str(runtime),
             "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
             "wrapper_sha256": hashlib.sha256(content).hexdigest()}
    _atomic(wrapper, content, 0o755)
    _atomic(prefix / "gba-install.json", json.dumps(state).encode())
    if probe_version(wrapper)["status"] != "ok":
        raise SetupError("installed_version_check_failed")
    state["verified"] = True
    _atomic(prefix / "gba-install.json", json.dumps(state).encode())
    return version


def _platform_reason():
    if platform.system() != "Linux" or not _arch():
        return "unsupported_platform"
    libc, version = platform.libc_ver()
    if libc != "glibc":
        return "unsupported_libc"
    try:
        if tuple(map(int, version.split(".")[:2])) < (2, 28):
            return "unsupported_libc_version"
    except ValueError:
        return "unsupported_libc_version"
    return None


def plan(root, selected):
    read_config(root)  # Shape validation only. No process, versions, mkdir or network.
    detected = {name: {"found": bool(find_executable(name)), "path": location(find_executable(name)),
                       "version": None, "version_check": "not_run_in_plan"}
                for name in ("node", "npm", "bun", *AGENTS)}
    system = platform.system().lower()
    arch = _arch()
    platform_reason = _platform_reason()
    supported = platform_reason is None
    return result("ok" if supported else "blocked", "setup_plan_ready" if supported else platform_reason,
                  "setup_apply" if supported else "setup_on_linux_box", requires_approval=supported,
                  operation="plan", selected=selected, platform=system if system in ("linux", "darwin", "windows") else "other",
                  arch=arch or "unsupported", detected=detected, prefix=location(base_dir()),
                  path_ready=str(home() / ".local/bin") in os.environ.get("PATH", "").split(os.pathsep),
                  actions={a: "reuse_existing" if detected[a]["found"] else "install" for a in selected})


@contextlib.contextmanager
def _apply_lock():
    import fcntl  # Linux only; non-Linux apply fails before reaching here.
    _mkdir(base_dir())
    path = _safe_target(base_dir() / "setup.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("setup_busy", "retry_setup") from None
        yield
    finally:
        os.close(fd)


def apply(root, selected):
    platform_reason = _platform_reason()
    if platform_reason:
        raise SetupError(platform_reason, "setup_on_linux_box")
    read_config(root)
    _safe_target(config_path())
    installed = {}
    with _apply_lock():
        missing = []
        for a in selected:
            state = managed_state(a)
            if state:
                installed[a] = {"action": "unchanged", "managed": True}
            elif find_executable(a):
                installed[a] = {"action": "unchanged", "managed": False}
            else:
                missing.append(a)
        if missing:
            node, npm = _node_runtime()
            bun = _bun_runtime() if "omp" in missing else None
            for a in missing:
                version = _install_agent(a, node, npm, bun)
                installed[a] = {"action": "installed", "managed": True, "version": version}
        update_config(root, selected)
    return result(reason="setup_applied", operation="apply", selected=selected, agents=installed,
                  path_ready=str(home() / ".local/bin") in os.environ.get("PATH", "").split(os.pathsep),
                  runner_path_ready=True)


def configure(subparsers):
    parser = subparsers.add_parser("setup", help="Plan or explicitly apply user-local agent installation")
    parser.set_defaults(command="setup", _gba_handler=handle)
    actions = parser.add_subparsers(dest="setup_action", required=True)
    for action in ("plan", "apply"):
        child = actions.add_parser(action)
        child.add_argument("--agents", required=True, help="Comma-separated claude,codex,pi,omp")


def handle(args, root):
    try:
        selected = selected_agents(getattr(args, "agents", None))
        action = getattr(args, "setup_action", None)
        if action == "plan":
            return plan(Path(root), selected)
        if action == "apply":
            return apply(Path(root), selected)
        return result("error", "invalid_setup_action", "setup_plan")
    except SetupError as exc:
        return result("blocked", exc.reason, exc.next_action)
    except Exception:
        return result("error", "setup_failed", "setup_repair")
