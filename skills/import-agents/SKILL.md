---
name: import-agents
description: Import allowlisted Claude Code, Codex, Pi or omp files and consented API keys from a registered macOS/Linux/Windows computer to the Grok Bot box. Whole-file consent, per-copy approval, resumable staging and no secret output. Use for copying existing agent settings or logins.
---

# Import agent setups

Resolve this file's directory: the complete plugin root is `../..`; invoke
`python3 <absolute-root>/scripts/gba` (**gba**). Standalone copies use absolute `GBA_ROOT`
pointing to the complete plugin; missing shared helpers means stop and request installation.
Use Python 3.11+. See [CLI/input contract](../../README.md#gba-cli), not the Shell's current directory.

## Consent is a bot/user responsibility

- Pick a connected computer with `ListMachines`; install only selected box agents with `gba setup`.
- Run `scripts/detect_local.sh` on macOS/Linux, or `detect_local.ps1` on Windows (verbatim via the
  machine Shell when the plugin is not installed there). Scanners inspect contents internally but emit names only.
- Show one **multiSelect consent card**: source, destination, names, strategy, and warnings.
  **One row per whole file**, not per provider/secret inside it. All providers in Pi `auth.json`,
  all secrets embedded in a config, and the entire omp database + existing WAL/SHM are inseparable.
  If the user wants only part, leave the whole file unticked and use separate box login/key entry.
  Scripts do not filter auth files. Nothing unticked may move, even as a side effect of bundling.
- Env names can be selected individually **before export**: `export_secrets.sh env:NAME …`.
  Only approved `resolve:pi|omp:FILE:PROVIDER=ENV_NAME` and `keychain:claude|codex` items may run.
  These helpers write mode-600 files; never run secret-store or indirection commands yourself.
  A denied OS-store prompt is not permission to retry. Consent covers the entire generated export file.
- Claude/Codex subscription logins default to **box login**, not copying. Opt-in copies need the
  two-machine refresh/logout warning; Claude subscription OAuth also carries a ToS risk (including Pi/omp use).
  Pi/omp OAuth copies need the same refresh warning. Pi `.env` stays off unless explicitly selected.
- Quit source omp before inventory/copy; disabled/expired logins should be refreshed there first.
  Never import browser profiles/logins, SSH/GPG/cloud credentials, history, caches or unrelated dotfiles.

## Controller sequence

1. Obtain a concrete file inventory on the source: `gba import inventory --agents <list>`
   (`--exported-keys` after approved exports). Save the name/hash-only JSON and transfer that metadata.
   This reuses the allowlist and honors CLI directory overrides; symlinks are omitted, not followed.
   If source Python/plugin is unavailable, use the existing scanner and a names + SHA256 file listing
   in the documented inventory format (PowerShell `Get-FileHash` on Windows); never read bodies into chat.
2. On the box: `gba import plan --inventory <metadata.json>` returns whole-file rows for the card.
   Save only ticked row IDs as a JSON array, then rerun with `--selection <selection.json>`.
   It allocates a private unique operation/stage; save the returned `operation_id`.
3. Follow `copy_to_box` transfers **with a separate CopyToBox approval for each file**. Use exactly
   returned destinations. `gba import status ID` restores the plan after interruption.
   SHA256 receipts bind the selected source bytes; changed/missing/extra files block application.
4. `gba import advance ID` checks receipts. After the user trusts imported packages/hooks/config code,
   add `--approve-runtime`: existing helpers may install dependencies and execute imported runtime code.
   All selected CLIs preflight before writes. Conflicts require a new approval and `--overwrite` (backups).
   Interrupted apply requires inspecting effects, then `--retry-interrupted`; never promise rollback.
5. `gba import verify ID` performs offline parity and removes only that operation's stage on success.
   `gba verify --agents <list>` gives shared diagnostics; only `--live` performs a provider OK-check.
   Quota is not proof of import success. Log in separately on the box if needed; secrets use masked input.
6. Delete only this import's source exports after confirmed transfer/application; report counts, agent status,
   backup presence/warnings and remaining actions. Do not print auth files, sensitive configs or `~/.agent-keys`.

## Explicit fallbacks and boundaries

Windows callers are less tested; POSIX bundle/export has no Windows twin. Use per-file copies and box
secret entry if unavailable. Source overrides `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, or `PI_CODING_AGENT_DIR`
also require per-file transfer rather than `bundle_local.sh`.
For symlinked skills/external Pi packages, the existing reviewed `bundle_local.sh` → `stage_tar.py` →
`apply_import.sh` path remains available outside the controller. Only use it when **every bundled file**
is covered by whole-file/group consent; `--skip` excludes whole paths, never individual providers.
Do not use a broad bundle for a partial selection. Controller external `_home` package manifests are not supported.

The controller wraps apply with `--no-verify`, retains approved receipts for retry, and preserves central
model/reasoning/auth settings. Legacy direct apply resets model/reasoning to `native` only when auth was
imported and its status check passed; settings-only imports keep central overrides. Ask before changing them.
A kept working omp login is reported, not overridden. `--force-auth` requires a separate reviewed legacy operation.
