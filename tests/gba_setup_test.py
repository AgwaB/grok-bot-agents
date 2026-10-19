#!/usr/bin/env python3
"""No network/install/provider access: isolated HOME and fake executables only."""
import argparse
import base64
import contextlib
import hashlib
import importlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
setup = importlib.import_module("gba_setup")

CANARY = "s3cret_CANARY_must_never_appear"


def package_archive(agent):
    data = io.BytesIO()
    files = {
        "package/package.json": json.dumps({"name": setup.PACKAGES[agent], "version": "1.2.3",
                                            "bin": {agent: "bin/agent.js"}}).encode(),
        "package/bin/agent.js": b"// fake executable; no provider calls\n",
    }
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    return data.getvalue()


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gba-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / "fakebin"
        self.bin.mkdir()
        env = {"HOME": str(self.home), "PATH": str(self.bin), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.platform = mock.patch.object(setup.platform, "system", return_value="Linux")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.machine = mock.patch.object(setup.platform, "machine", return_value="x86_64")
        self.machine.start()
        self.addCleanup(self.machine.stop)
        self.libc = mock.patch.object(setup.platform, "libc_ver", return_value=("glibc", "2.35"))
        self.libc.start()
        self.addCleanup(self.libc.stop)
        self.network = mock.patch.object(setup.urllib.request, "build_opener", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)
        return path

    def fake_runtime(self):
        # npm's install logic is simulated; this never invokes npm or a shell.
        node = self.executable("node", '''import json, os, pathlib, sys, tarfile
home = pathlib.Path(os.environ["HOME"])
with (home / "events").open("a") as log: log.write("node\\n")
args = sys.argv[1:]
if args == ["--version"]:
    print("v22.19.0")
elif "install" in args:
    assert "--ignore-scripts" in args
    assert os.environ["NPM_CONFIG_USERCONFIG"] == os.devnull
    assert "ANTHROPIC_API_KEY" not in os.environ
    prefix = pathlib.Path(args[args.index("--prefix") + 1])
    with tarfile.open(args[-1]) as archive:
        package = json.load(archive.extractfile("package/package.json"))
        dest = prefix / "lib/node_modules" / package["name"]
        for member in archive.getmembers():
            if member.isfile():
                target = dest / pathlib.Path(member.name).relative_to("package")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    print("s3cret_CANARY_must_never_appear")
    print("s3cret_CANARY_must_never_appear", file=sys.stderr)
else:
    assert args[-1] == "--version"
    print("1.2.3")
''')
        npm = self.executable("npm", 'print("10.9.0")\n')
        return node, npm

    def registry(self):
        blobs = {a: package_archive(a) for a in setup.AGENTS}
        requested = []

        def metadata(url):
            for agent, package in setup.PACKAGES.items():
                if setup.urllib.parse.quote(package, safe="@") in url:
                    requested.append(agent)
                    digest = base64.b64encode(hashlib.sha512(blobs[agent]).digest()).decode()
                    return {"name": package, "version": "1.2.3", "dist": {
                        "tarball": "https://registry.npmjs.org/" + agent + ".tgz", "integrity": "sha512-" + digest}}
            self.fail("unexpected package")

        def fetch(url, maximum=setup.MAX_DOWNLOAD):
            return blobs[url.rsplit("/", 1)[1].split(".")[0]]

        return mock.patch.object(setup, "_json_download", side_effect=metadata), mock.patch.object(setup, "_fetch", side_effect=fetch), requested

    def invoke(self, action, agents="pi"):
        parser = argparse.ArgumentParser()
        setup.configure(parser.add_subparsers(dest="command", required=True))
        args = parser.parse_args(["setup", action, "--agents", agents])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = setup.handle(args, ROOT)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        encoded = json.dumps(response)
        self.assertNotIn(CANARY, encoded)
        for name in ("status", "reason", "next_action", "requires_approval"):
            self.assertIn(name, response)
        return response

    def config(self, enabled):
        cfg = setup._helper(ROOT)["default_config"](enabled)
        path = setup.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg))
        return cfg


class SetupTests(Isolated):
    def test_plan_is_strictly_write_and_process_free(self):
        self.fake_runtime()
        self.config(["codex"])
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("process forbidden")):
            response = self.invoke("plan", "pi,claude")
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(response["reason"], "setup_plan_ready")
        self.assertEqual(response["selected"], ["claude", "pi"])
        self.assertTrue(response["requires_approval"])
        self.assertTrue(response["detected"]["node"]["found"])
        self.assertIsNone(response["detected"]["node"]["version"])

    def test_explicit_apply_selected_only_idempotent_and_wrapper_path(self):
        self.fake_runtime()
        original = self.config(["codex"])
        original["permission_mode"] = "safe"
        original["agents"]["pi"]["model"] = "provider/native-preference"
        setup.config_path().write_text(json.dumps(original))
        os.environ["ANTHROPIC_API_KEY"] = CANARY
        metadata, fetch, requested = self.registry()
        with metadata, fetch:
            response = self.invoke("apply", "pi")
            self.assertEqual(response["status"], "ok", response)
            self.assertEqual(requested, ["pi"])
            cfg = json.loads(setup.config_path().read_text())
            self.assertEqual(cfg["enabled"], ["codex", "pi"])
            self.assertEqual(cfg["permission_mode"], "safe")
            self.assertEqual(cfg["agents"]["pi"], original["agents"]["pi"])
            self.assertFalse((self.home / ".local/bin/claude").exists())
            wrapper = self.home / ".local/bin/pi"
            content = wrapper.read_text()
            self.assertIn(shlex.quote(str(self.bin)), content)
            self.assertIn('"$@"', content)
            self.assertFalse(response["path_ready"])
            self.assertTrue(response["runner_path_ready"])
            result = subprocess.run([str(wrapper), "--version"], capture_output=True, env=dict(os.environ), timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), b"1.2.3")
            events = (self.home / "events").read_bytes()
            config_before = setup.config_path().stat().st_mtime_ns
            second = self.invoke("apply", "pi")
            self.assertEqual(second["agents"]["pi"]["action"], "unchanged")
            self.assertEqual(requested, ["pi"])
            self.assertEqual(events, (self.home / "events").read_bytes())
            self.assertEqual(config_before, setup.config_path().stat().st_mtime_ns)

    def test_all_agents_have_actual_install_paths_and_omp_uses_bun(self):
        self.fake_runtime()
        bun = self.executable("bun", 'print("1.3.3")\n')
        metadata, fetch, requested = self.registry()
        with metadata, fetch:
            response = self.invoke("apply", ",".join(setup.AGENTS))
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(requested, list(setup.AGENTS))
        self.assertEqual(json.loads(setup.config_path().read_text())["enabled"], list(setup.AGENTS))
        self.assertIn("exec " + shlex.quote(str(bun)), (self.home / ".local/bin/omp").read_text())
        for agent in setup.AGENTS:
            self.assertIsNotNone(setup.managed_state(agent))

    def test_existing_external_install_is_not_replaced(self):
        path = self.executable("claude", 'raise AssertionError("must not start agent in apply")\n')
        original = path.read_bytes()
        response = self.invoke("apply", "claude")
        self.assertEqual(response["status"], "ok", response)
        self.assertFalse(response["agents"]["claude"]["managed"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(setup.config_path().read_text())["enabled"], ["claude"])

    def test_registry_integrity_rejection_prevents_npm(self):
        self.fake_runtime()
        bad = {"name": setup.PACKAGES["pi"], "version": "1.2.3", "dist": {
            "tarball": "https://registry.npmjs.org/pi.tgz",
            "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode()}}
        with mock.patch.object(setup, "_json_download", return_value=bad), mock.patch.object(setup, "_fetch", return_value=b"tampered"):
            response = self.invoke("apply")
        self.assertEqual(response["reason"], "package_integrity_mismatch")
        self.assertFalse(setup.config_path().exists())
        self.assertFalse((self.home / ".local/bin/pi").exists())
        self.assertEqual((self.home / "events").read_text(), "node\n")

    def test_node_checksum_rejection_before_extraction(self):
        with mock.patch.object(setup, "_json_download", return_value=[{"version": "v22.20.0"}]), mock.patch.object(
                setup, "_fetch", side_effect=[b"0" * 64 + b"  node-v22.20.0-linux-x64.tar.xz\n", b"tampered"]):
            response = self.invoke("apply")
        self.assertEqual(response["reason"], "runtime_checksum_mismatch")
        self.assertFalse((setup.base_dir() / "runtime/node").exists())

    def test_verified_node_archive_bootstraps_without_system_npm(self):
        stream = io.BytesIO()
        top = "node-v22.20.0-linux-x64"
        with tarfile.open(fileobj=stream, mode="w:xz") as archive:
            for name in ("bin/node", "bin/npm"):
                item = tarfile.TarInfo(top + "/" + name)
                content = b"fake runtime"
                item.size, item.mode = len(content), 0o755
                archive.addfile(item, io.BytesIO(content))
        data = stream.getvalue()
        manifest = hashlib.sha256(data).hexdigest().encode() + b"  " + (top + ".tar.xz\n").encode()
        with mock.patch.object(setup, "_json_download", return_value=[{"version": "v22.20.0"}]), mock.patch.object(
                setup, "_fetch", side_effect=[manifest, data]):
            node, npm = setup._node_runtime()
        self.assertTrue(node.is_file())
        self.assertTrue(npm.is_file())
        self.assertTrue(node.stat().st_mode & 0o111)

    def test_bun_uses_verified_official_npm_binary_archive(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            item = tarfile.TarInfo("package/bin/bun")
            item.size = 4
            item.mode = 0o755
            archive.addfile(item, io.BytesIO(b"fake"))
        with mock.patch.object(setup, "registry_archive", return_value=(stream.getvalue(), "1.3.3", "integrity")) as registry:
            bun = setup._bun_runtime()
        registry.assert_called_once_with("@oven/bun-linux-x64-baseline")
        self.assertEqual(bun.read_bytes(), b"fake")

    def test_corrupt_managed_wrapper_fails_closed(self):
        self.fake_runtime()
        metadata, fetch, _ = self.registry()
        with metadata, fetch:
            self.assertEqual(self.invoke("apply")["status"], "ok")
        (self.home / ".local/bin/pi").write_text("tampered")
        self.assertEqual(self.invoke("apply")["reason"], "managed_install_invalid")

    def test_unknown_agents_and_unsupported_platform_are_stable_json(self):
        self.assertEqual(self.invoke("apply", CANARY)["reason"], "invalid_agents")
        for system in ("Darwin", "Windows"):
            with mock.patch.object(setup.platform, "system", return_value=system):
                response = self.invoke("apply")
                self.assertEqual(response["reason"], "unsupported_platform")
                self.assertEqual(response["next_action"], "setup_on_linux_box")
        self.assertFalse((self.home / ".local").exists())

    def test_config_and_managed_path_symlinks_fail_closed(self):
        target = self.home / "elsewhere"
        target.mkdir()
        (self.home / ".local").symlink_to(target, target_is_directory=True)
        response = self.invoke("apply")
        self.assertEqual(response["reason"], "install_path_unsafe")
        self.assertEqual(list(target.iterdir()), [])

    def test_managed_target_dotdot_rejected(self):
        with self.assertRaises(setup.SetupError) as raised:
            setup._safe_target(self.home / '..' / 'outside-config')
        self.assertEqual(raised.exception.reason, 'install_path_unsafe')

    def test_archive_traversal_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            item = tarfile.TarInfo("package/../../outside")
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
        with self.assertRaises(setup.SetupError) as raised:
            setup._extract(stream.getvalue(), self.home / "extract", "package")
        self.assertEqual(raised.exception.reason, "archive_unsafe")
        self.assertFalse((self.home.parent / "outside").exists())

    def test_download_origin_is_checked_before_network(self):
        for url in ("http://nodejs.org/x", "file:///etc/passwd", "https://evil.example/x", "https://x@nodejs.org/x",
                    "https://:password@nodejs.org/x", "https://nodejs.org:444/x"):
            with self.assertRaises(setup.SetupError) as raised:
                setup._fetch(url)
            self.assertEqual(raised.exception.reason, "download_origin_rejected")
        with self.assertRaises(setup.SetupError):
            setup._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.example/")

    def test_pi_old_node_is_replaced_only_inside_its_wrapper(self):
        old, npm = self.fake_runtime()
        new_script = old.read_bytes()
        old.write_bytes(new_script.replace(b"v22.19.0", b"v20.0.0"))
        stream = io.BytesIO()
        top = "node-v22.20.0-linux-x64"
        with tarfile.open(fileobj=stream, mode="w:xz") as archive:
            for name, content in (("bin/node", new_script), ("bin/npm", npm.read_bytes())):
                item = tarfile.TarInfo(top + "/" + name)
                item.size, item.mode = len(content), 0o755
                archive.addfile(item, io.BytesIO(content))
        data = stream.getvalue()
        manifest = hashlib.sha256(data).hexdigest().encode() + b"  " + (top + ".tar.xz\n").encode()
        metadata, fetch, requested = self.registry()
        with metadata as package_meta, fetch as package_fetch, mock.patch.object(setup, "_json_download", side_effect=
                lambda url: [{"version": "v22.20.0"}] if "nodejs.org" in url else package_meta(url)), mock.patch.object(
                setup, "_fetch", side_effect=lambda url, *rest: (manifest if url.endswith(".txt") else data)
                if "nodejs.org" in url else package_fetch(url, *rest)):
            response = self.invoke("apply", "pi")
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(requested, ["pi"])
        wrapper = (self.home / ".local/bin/pi").read_text()
        self.assertIn(str(setup.base_dir() / "runtime/node/bin/node"), wrapper)
        self.assertIn(b"v20.0.0", old.read_bytes())
        self.assertFalse((self.home / ".local/bin/node").exists())

    def test_apply_never_enables_unselected_existing_agents(self):
        self.fake_runtime()
        self.executable("pi", 'print("1.0.0")\n')
        metadata, fetch, requested = self.registry()
        with metadata, fetch:
            response = self.invoke("apply", "codex")
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(requested, ["codex"])
        self.assertEqual(json.loads(setup.config_path().read_text())["enabled"], ["codex"])
        self.assertEqual(setup.config_path().stat().st_mode & 0o777, 0o600)

    def test_apply_fails_closed_if_installed_cli_cannot_start(self):
        self.fake_runtime()
        metadata, fetch, _ = self.registry()
        original_probe = setup.probe_version
        with metadata, fetch, mock.patch.object(setup, "probe_version", side_effect=lambda executable, **kwargs:
                {"status": "version_unavailable", "version": None} if Path(executable).name == "pi"
                else original_probe(executable, **kwargs)):
            response = self.invoke("apply")
        self.assertEqual(response["reason"], "installed_version_check_failed")
        self.assertFalse(setup.config_path().exists())
        self.assertEqual(self.invoke("apply")["reason"], "managed_install_invalid")

    def test_missing_integrity_and_bad_prerequisites_fail_closed(self):
        with mock.patch.object(setup, "_json_download", return_value={"name": setup.PACKAGES["pi"], "version": "1.2.3",
                                                                    "dist": {"shasum": "legacy-only"}}), self.assertRaises(setup.SetupError) as raised:
            setup.registry_archive(setup.PACKAGES["pi"])
        self.assertEqual(raised.exception.reason, "package_integrity_missing")
        for libc, expected in ((("musl", "1.2"), "unsupported_libc"), (("glibc", "2.17"), "unsupported_libc_version")):
            with mock.patch.object(setup.platform, "libc_ver", return_value=libc):
                self.assertEqual(self.invoke("apply")["reason"], expected)
        self.assertFalse((self.home / ".local").exists())

    def test_invalid_config_blocks_apply_before_disk_or_process_mutation(self):
        self.config(["pi"])
        setup.config_path().write_text('{"enabled": "' + CANARY + '"}')
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("no process")):
            self.assertEqual(self.invoke("apply")["reason"], "config_invalid")
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
