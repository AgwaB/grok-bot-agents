#!/usr/bin/env python3
"""Print what to copy from the user's own computer to the box for each CLI. Never reads any file.

  import_plan.py --os macos|linux|windows --home /Users/alice [--cli codex,claude,pi,omp] [--json]

Each entry: source path on the user's computer -> staging path on the box
(/workspace/uploads/agent-import/<cli>/<rel>), plus kind (auth|config|extra|dir|merge|meta|package|overlay), and notes.
meta: staged for the post-install steps, not copied as-is. package/overlay: code, installed without path rewrites.
Copy only what exists (check with `test -e` / `Test-Path` on the user's machine first).
"""
import argparse, json, re, sys

STAGE = "/workspace/uploads/agent-import"

# rel paths are relative to the CLI's home dir on both sides.
PLAN = {
    "codex": {
        "home": ".codex", "env_override": "CODEX_HOME",
        "files": [
            ("auth.json", "auth", "ChatGPT login or API key. Same token on two machines, see the refresh warning."),
            ("config.toml", "config", "Model, profiles, MCP servers. Check for absolute paths."),
            ("AGENTS.md", "extra", "Global instructions."),
            ("prompts/", "dir", "Custom slash prompts (*.md)."),
            ("skills/", "dir", "User skills, if any."),
            ("rules/", "dir", "Exec-policy rules (*.rules), if any."),
        ],
        "skip": ["sessions/", "log/", "history.jsonl", "*.sqlite*", "cache/", "tmp/", "shell_snapshots/"],
        "verify": "codex login status",
    },
    "claude": {
        "home": ".claude", "env_override": "CLAUDE_CONFIG_DIR",
        "files": [
            (".credentials.json", "auth", "Login (Linux, and Windows builds that use the file). macOS keeps it in Keychain: there's no file, so use `claude auth login` on the box."),
            ("settings.json", "config", "Permissions, env, hooks, model. Check hooks and paths."),
            ("CLAUDE.md", "extra", "Global memory and instructions."),
            ("agents/", "dir", "Subagents."),
            ("commands/", "dir", "Slash commands."),
            ("skills/", "dir", "User skills."),
            ("hooks/", "dir", "Hook scripts that settings.json hooks call (e.g. ~/.claude/hooks/x.sh)."),
            ("plugins/installed_plugins.json", "meta", "Installed plugin versions and git SHAs. Not copied as-is: used to reinstall the enabledPlugins on the box and pin them to the same commit."),
            ("../.claude.json", "merge", "Global state. Only the `mcpServers` key gets merged into the box's ~/.claude.json. Projects, history and account fields are ignored."),
        ],
        "skip": ["projects/", "sessions/", "history.jsonl", "todos/", "shell-snapshots/", "statsig/", "ide/", "backups/", "file-history/", "plans/", "plugins/cache/", "logs/", "debug/"],
        "verify": "claude auth status",
    },
    "pi": {
        "home": ".pi/agent", "env_override": "PI_CODING_AGENT_DIR",
        "files": [
            ("auth.json", "auth", "Provider OAuth logins and stored API keys (`/login`). Keys set only as env vars aren't in here."),
            ("settings.json", "config", "defaultProvider / defaultModel / defaultThinkingLevel."),
            ("models.json", "config", "Custom providers and models. Should point at env-var names, not raw keys."),
            ("anthropic-auth.json", "auth", "Used by the Pi package @cortexkit/pi-anthropic-auth (multi-account Anthropic OAuth), when that package is in settings.json `packages`."),
            ("AGENTS.md", "extra", "Global instructions, if present."),
            ("prompts/", "dir", "Prompt templates, if present."),
            ("skills/", "dir", "Skills, if present."),
            ("extensions/", "dir", "Extensions, if present."),
            ("themes/", "dir", "Themes (settings.json `theme`)."),
            ("agents/", "dir", "Subagent definitions (pi-subagent and similar packages)."),
            ("workflows/", "dir", "Workflow definitions (pi-workflow packages)."),
            ("workflow-profiles/", "dir", "Workflow profiles."),
            ("memory/", "dir", "Saved memories (piMemory)."),
            ("hud.json", "config", "HUD/status settings used by UI packages."),
            (".pi-lens.json", "config", "pi-lens settings."),
            ("trust.json", "config", "Trusted project dirs (home paths are rewritten)."),
            ("web-search.json", "auth", "pi-web-access settings; holds search API keys (e.g. exaApiKey). Never print it."),
            ("anthropic-auth-state.json", "auth", "Account state of @cortexkit/pi-anthropic-auth."),
            ("anthropic-auth-routing-state.json", "auth", "Routing state of @cortexkit/pi-anthropic-auth."),
            ("packages/", "package", "Local Pi packages listed in settings.json `packages` (no node_modules/.venv/.git); deps reinstalled on the box."),
            ("npm/node_modules/", "overlay", "Files of the npm packages Pi installed on the computer (no nested node_modules). Laid over the same versions on the box, so local patches survive."),
            ("_home/", "package", "Local Pi packages outside ~/.pi/agent but under the home dir (e.g. ~/pi/my-ext), from the bundle manifest."),
        ],
        "skip": ["sessions/", "oauth.json (legacy; pi migrates it into auth.json)", "bin/", "logs/"],
        "verify": "pi --list-models",
    },
    "omp": {
        "home": ".omp/agent", "env_override": "PI_CODING_AGENT_DIR",
        "files": [
            ("agent.db", "auth", "SQLite file holding omp's logins and settings. Quit omp on the computer first. Also copy agent.db-wal / agent.db-shm if they exist."),
            ("config.yml", "config", "Settings (tools.approvalMode, etc.)."),
            ("models.yml", "config", "Providers, with `apiKey: ENV_NAME`. Import the env values separately through ~/.agent-keys."),
            ("AGENTS.md", "extra", "Global instructions, if present."),
            ("mcp.json", "config", "MCP servers, if present. Check for absolute paths."),
        ],
        "skip": ["sessions/", "logs/", "cache/", "mnemopi.db (local memory)", "plugins/ (reinstall instead)"],
        "verify": "omp usage",
    },
}

NEVER = ["~/.ssh/", "~/.gnupg/", "~/.aws/", "~/.netrc", "~/.git-credentials", "browser profiles", "any other dotfile"]


def src(os_name, home, cli, rel):
    base = PLAN[cli]["home"]
    if rel.startswith("../"):
        parts = [home] + base.split("/")[:-1] + [rel[3:]]
        if cli == "claude": parts = [home, rel[3:]]
    else:
        parts = [home] + base.split("/") + [rel.rstrip("/")]
    sep = "\\" if os_name == "windows" else "/"
    p = sep.join(x.strip("/\\") if i else x.rstrip("/\\") for i, x in enumerate(parts))
    return p + (sep if rel.endswith("/") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os", required="--lint-scripts" not in sys.argv, choices=["macos", "linux", "windows"])
    ap.add_argument("--home", required="--lint-scripts" not in sys.argv, help="user's home dir on their computer, e.g. /Users/alice or C:\\Users\\alice")
    ap.add_argument("--cli", default="codex,claude,pi,omp")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dereference", action="store_true",
                    help="print commands to bundle symlinked skill dirs as real files (tar -h) on the user's computer")
    ap.add_argument("--lint-scripts", action="store_true",
                    help="check that detect_local.sh and bundle_local.sh cover every path in PLAN (dev check)")
    a = ap.parse_args()
    if a.lint_scripts:
        import os
        here = os.path.dirname(os.path.abspath(__file__)); bad = []
        for script in ("detect_local.sh", "bundle_local.sh"):
            text = open(os.path.join(here, script), encoding="utf-8").read()
            for c, p in PLAN.items():
                for rel, kind, _ in p["files"]:
                    if rel == "_home/": continue  # bundle manifest roots, not a fixed path
                    if script == "detect_local.sh" and kind in ("meta", "package", "overlay"): continue
                    name = ".claude.json" if rel.startswith("../") else rel.rstrip("/")
                    if not re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", text): bad.append(f"{script}: {c} {rel}")
        print("\n".join(bad) if bad else "scripts cover every PLAN path"); sys.exit(1 if bad else 0)
    clis = [c.strip() for c in a.cli.split(",") if c.strip()]
    bad = [c for c in clis if c not in PLAN]
    if bad: sys.exit(f"unknown cli: {bad}")
    out = {"stage_root": STAGE, "never_copy": NEVER, "clis": {}}
    for c in clis:
        p = PLAN[c]; items = []
        for rel, kind, note in p["files"]:
            if c == "claude" and rel == ".credentials.json" and a.os == "macos":
                items.append({"rel": rel, "kind": "auth", "source": None, "stage": None,
                              "note": "macOS: stored in Keychain item 'Claude Code-credentials', not a file. Can't copy it; use `claude auth login` on the box instead."})
                continue
            if rel == "_home/":
                items.append({"rel": rel, "kind": kind, "source": f"{a.home}/<package dir from settings.json>", "stage": f"{STAGE}/{c}/_home/<path under home>", "note": note})
                continue
            stage_rel = rel[3:] if rel.startswith("../") else rel
            items.append({"rel": rel, "kind": kind, "source": src(a.os, a.home, c, rel),
                          "stage": f"{STAGE}/{c}/{'_home/' if rel.startswith('../') else ''}{stage_rel}", "note": note})
        out["clis"][c] = {"box_home": "~/" + p["home"], "env_override": p["env_override"], "items": items,
                          "skip": p["skip"], "verify": p["verify"]}
    if a.dereference:
        dirs = [f"~/{PLAN[c]['home']}/{d}" for c in clis for d in ("skills", "agents", "commands", "prompts") if any(r == d + "/" for r, _, _ in PLAN[c]["files"])]
        if a.os == "windows":
            print("Windows: symlinks are rare there; copy the target folders into the stage instead."); return
        rel = " ".join(d.replace("~/", "") for d in dirs)
        print("# Run in bash or zsh on the user's computer (a file list avoids zsh word-splitting issues).")
        print("# 1. List symlinked entries (paths and targets only):")
        print(f"cd ~ && for d in {rel}; do [ -d \"$d\" ] && find \"$d\" -maxdepth 1 -type l; done > ~/.agent-import-links.txt; cat ~/.agent-import-links.txt")
        print("# 2. Bundle them dereferenced (-h). Dangling links stay links and are skipped on the box:")
        print("COPYFILE_DISABLE=1 tar czhf ~/agent-import-skills.tgz --exclude='._*' --exclude='.DS_Store' --exclude='*.pyc' --exclude='__pycache__' -T ~/.agent-import-links.txt && rm -f ~/.agent-import-links.txt")
        print("# 3. CopyToBox ~/agent-import-skills.tgz -> /workspace/uploads/agent-import-skills.tgz, then on the computer: rm -f ~/agent-import-skills.tgz")
        print("# 4. On the box: python3 stage_tar.py /workspace/uploads/agent-import-skills.tgz --cli <clis>; bash apply_import.sh <cli> --src-home <home>; rm the tgz")
        return
    if a.json:
        print(json.dumps(out, indent=2)); return
    print(f"Stage everything under {STAGE}/<cli>/ with CopyToBox (one approval per file).")
    print("Never copy: " + ", ".join(NEVER))
    for c, d in out["clis"].items():
        print(f"\n== {c}  (box: {d['box_home']}; verify: {d['verify']})")
        for it in d["items"]:
            s = it["source"] or "(not a file)"
            print(f"  [{it['kind']:<6}] {s}\n           -> {it['stage'] or '-'}\n           {it['note']}")
        print("  skip: " + ", ".join(d["skip"]))
    print("\nFor dir/ entries: list files on the user's computer first and copy each one into the same relative path under the stage dir.")
    print("Then: bash apply_import.sh <cli> [--dry-run]")


if __name__ == "__main__":
    main()
