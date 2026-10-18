# Import regression matrix

Scope: the public v0.1.0 importer, not a particular workstation. Research:
[eight projects and opened sources](../docs/research/importers.md). No real logins,
provider requests, global installs, or real home directories are used by tests.

## Contract and execution

Claude/Codex subscriptions default to a **separate box login**, copying only by
opt-in. Static keys require one name-only consent card; exported keys land in
`~/.agent-keys`. Pi/omp auth copies retain expiry/disabled warnings. Browser,
SSH/GPG and cloud credentials are never eligible, including through manifests.

Each automated subprocess gets an empty environment, temporary HOME, explicit
stage and stub tools. Synthetic random canaries are checked against **both stdout
and stderr before diagnostics are displayed**. Destination contents, modes,
backups, return codes and source immutability are asserted, not just JSON shape.
Native stores, actual refresh and human consent cannot be certified by stubs.

Legend: **A** = automated by `import_matrix_test.py` (test method named in the last
column); **E** = existing `auth_import_test.sh`; **M** = manual/native follow-up;
**G** = documented remaining gap. Status records coverage, not an expected failure.
Rows are individual scenarios, not a full Cartesian product. Parameterized tests
cover additional combinations. Acceptance: automated regressions and static
checks pass, native skips and remaining gaps are explicit, version stays 0.1.0.

## Discovery, platforms and paths

| ID | Scenario / fixture | Required result | Coverage |
|---|---|---|---|
| D01 | Linux empty home, all four CLIs absent | `none`, no auth/config, valid names-only inventory | A `detect_empty` |
| D02 | macOS mode, installed version stubs, each CLI present | Installed/version independent of saved config/auth | A `detect_installed` |
| D03 | All four CLIs absent but partial config/auth dirs exist | Detect files independently of executable presence | A `detect_partial` |
| D04 | File login for each CLI, no native store | File presence, Pi expiry and omp state reported without refreshing | E; A `detect_partial`, `omp_wal` |
| D05 | macOS Keychain present/denied/missing | Presence only; no value printed; denied export fails closed | A `keychain` |
| D06 | Linux Secret Service present/absent, scoped Codex account | No arbitrary-account fallback; no DBus means no presence probe | A `keyring` |
| D07 | Windows-style `C:\Users\Zoë Smith` source | Plan uses Windows separators; text rewrite understands escaped paths | A `windows_paths` |
| D08 | `CODEX_HOME` override | Detection/export/apply use override; bundle refuses default-dir substitution | A `overrides` |
| D09 | `CLAUDE_CONFIG_DIR` override, `.claude.json` inside it | Scanner, merge and parity use relocated global file | A `overrides` |
| D10 | `PI_CODING_AGENT_DIR` override | Detect/export/apply/parity use Pi override; unsupported bundle refuses | A `pi_override` |
| D11 | HOME and resource paths with spaces, Unicode, quotes | Valid JSON, intact files and paths throughout | A `quoted_home`, `linked_skills` |
| D12 | Neither node nor bun, Linux | `scanner: none`, file detection still works | A `scanner_fallbacks` |
| D13 | bun only | Same scanner contract as node | A `scanner_fallbacks` (bun dispatch stub) |
| D14 | macOS neither node nor bun: JXA | Correct JXA selection; Foundation reads and export work | A `scanner_fallbacks` (dispatch); M native JXA |
| D15 | Windows PowerShell direct and scriptblock invocation | Same inventory/override semantics, no canary | A `powershell` if pwsh; M native Windows ACL/junction/registry |
| D16 | Hung/noisy `--version` | Bounded wait; only a version, never arbitrary CLI diagnostics | A `version_output`; M native process-tree termination |

## Auth selection, consent and secret handling

| ID | Scenario / fixture | Required result | Coverage |
|---|---|---|---|
| S01 | Default subscription files and MCP OAuth | Claude/Codex credentials absent from bundle, box-login warning | E; A `bundle_auth` |
| S02 | Explicit `--copy-login claude,codex` | Login files included; account merge/two-machine warning | E; A `bundle_auth` |
| S03 | Static Codex API key (compact/pretty JSON) | Included without subscription opt-in | A `bundle_auth` |
| S04 | Multiline `tokens`, null key, empty/malformed/scalar auth JSON | Fail closed: no implicit subscription or unknown-auth copy | A `bundle_auth` |
| S05 | Pi OAuth expired/future/unknown expiry | Flag expiry without running Pi or refreshing source | A `pi_expiry` |
| S06 | Literal config keys in JSON/TOML/YAML | Inventory reports key paths only; config bytes remain sensitive | E; A `literal_inventory` |
| S07 | Env set in process vs only interactive login shell | Names/provenance only; `--no-login-shell` excludes shell-only keys | E; A `shell_env` |
| S08 | `!cmd` and `op://` success after consent | 600 export; rewrite only resolved provider to env name | E; A `resolve_success` |
| S09 | Helper prints key then exits nonzero / empty / multiline | No exported key or rewrite from failed helper | A `resolve_failure` |
| S10 | Denied store prints partial JSON / malformed JSON | Reject output regardless of first byte; no auth file retained | A `keychain` |
| S11 | Invalid export item mixed with a valid item | Validate all before writes; error never echoes the rejected value | E; A `export_invalid` |
| S12 | Single quotes, dollar signs, backticks in env values | Safe literal shell quoting; merged file does not execute substitutions | A `env_roundtrip` |
| S13 | Re-import keys, duplicate existing entries, unrelated lines | One definition per imported name, stable re-merge, other lines kept | E; A `keys_idempotent` |
| S14 | Malicious/non-export staged env lines | Reject without execution or value-bearing diagnostics | A `keys_invalid` |
| S15 | Pi package auth globs, legacy oauth.json, opt-in .env | Generic package auth copied; legacy/.env excluded by default | E; A `legacy_auth` |
| S16 | Any subprocess/parser failure includes canary | Never relay raw stdout/stderr or exception text containing input | A `diagnostics` |
| S17 | Claude copied login + oauthAccount/onboarding | Merge only approved fields; preserve unrelated box state | E; A `claude_merge` |
| S18 | Identical copied Claude credential on re-import | Account fields still merge; repeated merge is a no-op | A `claude_identical_login` |
| S19 | One human consent card, deselected static keys | Name-only card before export; `--skip` honors file/directory selection | A `bundle_skip`; M consent UI |
| S20 | Native denied keychain prompt, Linux locked keyring, rotating tokens | No prompt retries or refresh during detect; separate login recovery | M native, never using this test runner |

## SQLite, archives and installation

| ID | Scenario / fixture | Required result | Coverage |
|---|---|---|---|
| I01 | omp disabled-only source, working box | Keep box db unit, import config, exit 5 even with overwrite | A `omp_disabled` |
| I02 | omp mixed enabled/disabled rows for same/different providers | Any enabled row makes that provider usable; disabled-only warned | A `omp_wal` |
| I03 | omp credentials committed only in WAL | Snapshot db+WAL+SHM, detect/apply reads rows; source hashes unchanged | A `omp_wal` |
| I04 | omp unreadable/corrupt source, working box | Preserve working login on uncertainty; explicit force may replace | A `omp_corrupt` |
| I05 | omp source lacks WAL, destination stale WAL/SHM | Backup/remove stale sidecars on replacement; warn about missing source WAL | A `omp_stale_wal` |
| I06 | Missing sqlite3 executable | Detect reports unknown, not disabled/no-login; Python apply still checks | A `missing_sqlite` |
| I07 | Valid symlinked skill and dangling link | Bundle dereferences valid link, excludes dangling; no source changes | A `linked_skills` |
| I08 | Symlink/special file in per-file stage | Skip without copying or following | A `apply_filters` |
| I09 | Archive absolute/traversal/symlink/hardlink/device members | Skip unsafe entries; no writes outside stage | A `tar_safety` |
| I10 | Existing staging symlink (including metadata/secrets) | Refuse before following into another directory | A `stage_symlink` |
| I11 | Destination symlink, dangling link, symlink parent/temp path | Refuse before writes to target; no follow-through | A `destination_symlink` |
| I12 | AppleDouble, .DS_Store, pyc and __pycache__ | Exclude in bundle/stage/apply, including nested metadata dirs | A `tar_safety`, `apply_filters` |
| I13 | Huge (>200 MiB sparse) and binary files | Bound extraction/copy; binary bytes unchanged, no decode traceback | A `large_binary` |
| I14 | Package manifest tries `.ssh`, `.aws`, browsers, absolute/parent paths | Reject roots; direct `_home/` stage cannot bypass consent/allowlist | A `manifest_roots` |
| I15 | Ordinary local Pi package under home | Bundle manifest round trip, no node_modules/.venv/.git | A `package_bundle` |
| I16 | Existing different config | Exit 4 before *any* writes, including secrets and Claude global merge | A `conflicts`, `claude_conflict` |
| I17 | Overwrite twice quickly | Distinct 600 backups, each preserves its prior revision | A `backups` |
| I18 | Dry run then identical re-import | No writes on dry run; identical content skipped, no redundant backup | A `conflicts`, `idempotent_apply` |
| I19 | Codex keyring/auto/ephemeral config | Pin only top-level credential stores to file, idempotently | A `codex_stores` |
| I20 | Codex missing Mac MCP with `enabled = true` | Disable without duplicate TOML key; preserve unrelated servers | A `codex_mcp` |
| I21 | `--keep-stage`, ordinary cleanup, staged key cleanup | Keep only when requested; keys removed after real merge | E; A `idempotent_apply`, `keys_idempotent` |
| I22 | CLI verification failure vs success | Return code retained; no raw output; config recorded only after success | A `verify_status` |
| I23 | `parity_check.py --no-run` | No CLI calls, including `pi list`; inspect files only | A `parity_no_run` |
| I24 | Pi npm overlay/version mismatch, Claude plugin SHA and venv rebuild | Stub/offline validation before real installer use | G broader runtime-parity fixtures; no real installers in suite |
| I25 | Package symlink points outside home or into forbidden home secrets | Do not bundle forbidden target, including via skills | A `forbidden_links`; G arbitrary external package trees require audit |
| I26 | Tar creation fails or returns fatal rc after writing a partial archive | No success, keep consented exports for retry | A `bundle_failure` |
| I27 | Simultaneous imports / concurrent client refresh | Avoid lost writes and live WAL races | M/G no locking/transaction protocol yet; stop source CLI first |
| I28 | Fish/zsh login startup, native ACLs, Windows junctions | No credential output; correct native path/permission semantics | M native; POSIX tests do not certify these |

## Evidence and limits

64 scenarios: 16 discovery, 20 auth and 28 installation. Automated row labels
refer to `test_<name>` methods, with subtests for variants; existing coverage is
listed separately. Run commands, latest results and unavailable tools are in
[TESTING.md](../TESTING.md). Manual/gap rows are not counted as passing tests.
The suite intentionally does not call providers, log in, refresh tokens, install
packages or exercise user accounts. Status success is not proof a live login works.
