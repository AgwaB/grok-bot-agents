# grok-bot-agents (v0.1.0)

English: [README.md](README.md)

Grok Bot 박스(클라우드 컴퓨터)에서 **Claude Code · Codex CLI · Pi · omp(Oh My Pi)** 를 설치하고 로그인하는 스킬 전용 플러그인입니다.
가벼운 일은 Grok Bot이 직접 하고, **무거운 일은 코딩이든 아니든 CLI에 맡깁니다**. 리서치·분석, 데이터·파일 처리, 대량 변환, 긴 초안이나 번역, 로그 분석, 장시간 작업이 모두 해당됩니다.
맡기는 방식은 두 가지입니다. **직접 지목**("codex한테 시켜")하거나 Grok Bot이 **자동 라우팅**하게 둡니다.
실행은 헤드리스로 하고, 결과는 요약과 산출물 경로로 받아요. UX 흐름은 `docs/ux-flow.html` (PNG: `docs/ux-flow.png`)에 정리했습니다.

## 흐름

1. **설치**: Cursor 마켓플레이스에서 설치합니다(Grok Bot 설정의 플러그인).
2. **세팅** (`/setup-agents`)
   - 0: 등록된 컴퓨터에 어떤 CLI와 로그인·설정 파일이 있는지 확인합니다(`detect_local.sh`, 읽기 전용, 파일 이름만).
   - 2-1: 쓸 CLI를 고릅니다(복수 선택 카드). 내 컴퓨터에 있는 것은 미리 체크돼 있습니다. 고른 것만 설치합니다.
   - 2-2: 카드 하나로 **내 컴퓨터에서 전부 가져오기 (추천)** 또는 **에이전트별로 고르기**를 고릅니다. 가져오기는 허용 목록만 묶은 tar 하나를 복사해서 로그인까지 한 번에 옮깁니다(승인 4번 정도). 내 컴퓨터에 없는 CLI와 macOS의 Claude(키체인)는 박스에서 로그인합니다. API 키는 마스킹 입력이나 `~/.agent-keys`(600)로 넣고, **채팅에는 절대 쓰지 않습니다**.
   - 2-3: 기본 모델과 추론 레벨은 **native**(플래그를 넘기지 않음)여서 각 CLI의 자체 설정이나 가져온 설정을 그대로 씁니다. 가져온 경우엔 이 카드를 건너뜁니다. 그다음 중앙 config를 쓰고, CLI마다 OK 테스트를 돌립니다.
3. **명시적 지시** (`/delegate-task`): 지목한 CLI로 바로 실행합니다. 일이 가벼워도 지목이 우선입니다.
4. **자동 라우팅**: 가벼운 일은 봇이 직접 하고, 무거운 일만 활성화된 CLI에 맡깁니다. 반복·대량 작업은 Pi/omp 저가 모델, 어려운 추론·코드는 Claude/Codex, 장시간 무인 작업은 omp, 리뷰는 만들지 않은 쪽 에이전트가 맡습니다. 쓸 수 있는 CLI가 적으면 폴백 규칙을 따릅니다.
5. **설정 변경** (`/configure-agents`): 모델, 추론 레벨, 권한 모드, 라우팅, 로그인/키 교체, 각 CLI의 native config를 바꿉니다.

## 구조

```
grok-bot-agents/
├── .cursor-plugin/plugin.json
├── assets/logo.svg, logo.png
├── docs/ux-flow.html, ux-flow.png
└── skills/
    ├── setup-agents/SKILL.md
    ├── import-agents/
    │   ├── SKILL.md
    │   └── scripts/import_plan.py, detect_local.sh, bundle_local.sh, stage_tar.py, apply_import.sh   # 파일 목록 / 감지 / 묶기 / tar 스테이징 / 설치
    ├── configure-agents/
    │   ├── SKILL.md
    │   └── scripts/agents_config.py        # 중앙 config CLI (init/show/get/set/enable/disable/validate)
    ├── delegate-task/
    │   ├── SKILL.md
    │   ├── scripts/run-agent.sh            # config 기반 헤드리스 실행기 (인자로 덮어쓰기)
    │   ├── scripts/route.py                # 자동 라우팅 + rate limit 상태
    │   ├── scripts/summarize_run.py
    │   └── references/{codex,claude-code,pi,omp}.md
    └── agent-bridge-webhook/
        ├── SKILL.md
        └── scripts/bridge.py, bridge_server.py, bridge-serve.sh, setup-tailscale.sh, sanitize_result.py, send-task.sh, bridge.env.example
```

## 가벼운 일과 무거운 일

아래 중 **하나라도** 해당하면 무거운 일로 보고 CLI에 맡깁니다.

- 파일 약 5개 이상, 또는 레포·폴더 전체를 다룰 때
- 입력이 약 50KB 이상일 때 (큰 로그, CSV/JSON, 긴 PDF·문서 묶음)
- 도구·셸 단계가 약 10개 이상일 때
- 실행 → 확인 → 수정 반복이 필요할 때
- 5분 이상 걸리거나 무인·야간으로 돌릴 때
- 대량 생성처럼 토큰이 많이 드는 작업일 때
- 루틴으로 반복 실행할 때

그 밖에는 봇이 직접 합니다.

## 내 컴퓨터에서 설정 가져오기 (`/import-agents`)

사용자가 이미 쓰는 CLI 로그인과 설정을 등록된 컴퓨터에서 박스로 복사합니다. CLI마다 다시 로그인하지 않아도 됩니다.

- `ListMachines`로 컴퓨터를 고르고, 그 컴퓨터에서 `detect_local.sh`(읽기 전용)로 있는 파일 이름을 확인합니다. `bundle_local.sh`가 허용 목록만 심볼릭 링크를 풀어 `~/agent-import.tgz`(600) 하나로 묶고, `CopyToBox` 한 번으로 옮긴 뒤 컴퓨터에서 지웁니다. Windows 등에서는 파일을 하나씩 복사합니다. 복사할 때마다 사용자 승인을 받습니다. 봇은 토큰 파일 내용을 출력하지 않습니다.
- `import_plan.py`가 OS와 홈 경로를 받아 가져올 파일 목록을 출력합니다(파일은 읽지 않음). `apply_import.sh`가 `/workspace/uploads/agent-import/<cli>/`에서 설치합니다.
  - 허용 목록에 있는 파일만 설치하고, 권한은 600/700으로 맞춥니다.
  - 기존 파일과 다르면 멈춥니다. `--overwrite`를 주면 `*.bak.<ts>`로 백업한 뒤 덮어씁니다.
  - 홈 경로를 박스 경로로 바꾸고, Claude `~/.claude.json`에서는 `mcpServers`만 병합합니다.
  - 상태 확인을 통과하면 config에 `auth: imported`를 기록하고, 스테이징 사본을 지웁니다.

| CLI | 가져오는 것 | 메모 |
|---|---|---|
| Codex | `~/.codex/auth.json`, `config.toml`, `AGENTS.md`, `prompts/`, `skills/` | `codex login status`는 파일만 확인하므로 OK 실행으로 다시 확인합니다 |
| Claude Code | `~/.claude/.credentials.json`, `settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `~/.claude.json`의 `mcpServers` | macOS는 키체인(`Claude Code-credentials`)에 있어서 복사가 안 됩니다. 그 경우 박스에서 로그인합니다 |
| Pi | `~/.pi/agent/auth.json`, `anthropic-auth.json` (@cortexkit/pi-anthropic-auth 패키지용), `settings.json`, `models.json` (+ `AGENTS.md`, `prompts/`, `skills/`, `extensions/`) | Pi는 `@earendil-works/pi-coding-agent`(Node ≥ 22.19)를 씁니다. 첫 실행 때 settings의 npm 패키지를 자동 설치합니다 |
| omp | `~/.omp/agent/agent.db` (로그인·설정 SQLite), `config.yml`, `models.yml`, `mcp.json` | 먼저 omp를 종료합니다. `-wal`/`-shm` 파일이 있으면 같이 가져옵니다 |

세션, 히스토리, 로그, 캐시와 SSH 키 등 관련 없는 dotfile은 가져오지 않습니다. env 변수로 쓰던 API 키는 따로 마스킹 입력으로 `~/.agent-keys`에 넣습니다.
같은 로그인을 두 컴퓨터에서 쓰게 되면, OAuth 토큰이 갱신될 때 한쪽이 로그아웃될 수 있습니다. 그때는 그쪽에서 다시 로그인합니다.

## 답을 내 터미널로 받기 (Tailscale)

맥이나 Claude Code·Codex 안에서 `grok-send-task --wait "할 일"`을 실행하면 진행 상황과 답이 그 터미널로 돌아와요(테일넷 경유).
세팅은 봇이 다 해요. `setup-tailscale.sh`가 봇 컴퓨터에 Tailscale을 설치(root 불필요, sha256 검증)하고 실행한 뒤 로그인
링크를 줘요. 성재님은 링크만 승인하면 되고, 봇이 `--wait-login`으로 기다렸다가 결과 통로 토큰을 채팅에 노출하지 않고 맥에
넣어요. 맥에도 같은 계정의 Tailscale이 있어야 해요. 없으면 `grok-send-task --check`가 알려줘요(`brew install --cask tailscale`).

| 명령 | 하는 일 |
|---|---|
| `setup-tailscale.sh` | 필요하면 설치·실행·로그인 → `STATUS=ready` 또는 `needs_login` + `LOGIN_URL=…` |
| `setup-tailscale.sh --wait-login [초]` | 로그인 승인까지 대기 (기본 300초) |
| `setup-tailscale.sh --status` | `TAILNET_IP`, `DNS_NAME`, `RESULT_URL`, `STATUS` |
| `setup-tailscale.sh --uninstall [--purge] [--dry-run]` | 중지·삭제 (`--purge`면 로그인 상태도 삭제) |

## 실행 격리

- **git 레포**: 작업마다 worktree와 `agent/<run-id>` 브랜치를 만듭니다(항상).
- **레포가 아니거나 비코딩 작업**: `/workspace/agent-runs/<run-id>/work`에서 실행하고, 입력은 `--input`으로 넘겨 읽기용 dir로 추가합니다.
- 산출물은 run 폴더에 남고, 요약 JSON의 `artifacts`에 경로가 나옵니다.

## 중앙 config: `~/.grok-bot-agents/config.json`

모든 봇이 이 파일 하나를 함께 씁니다. 키 값은 저장하지 않고 env 이름(`key_env`)만 둡니다.

```json
{
  "version": 1,
  "enabled": ["claude", "codex", "pi"],
  "permission_mode": "full-auto",
  "worktree_per_task": true,
  "timeout_sec": 1800,
  "agents": {
    "claude": {"auth": "oauth", "model": null, "reasoning": null},
    "codex":  {"auth": "imported", "model": "native", "reasoning": "native"},
    "pi":     {"auth": "api_key", "key_env": "DEEPSEEK_API_KEY", "model": "deepseek/<model-id>", "reasoning": "low"},
    "omp":    {"auth": "imported", "model": "native", "reasoning": "native"}
  },
  "routing": {"mode": "auto", "cheap": ["pi", "omp"], "hard": ["claude", "codex"], "review": "other-frontier", "prefer": [], "fixed": null},
  "runs_dir": "/workspace/agent-runs"
}
```

- `model`/`reasoning`가 `null` 또는 `"native"`이면 플래그를 넘기지 않습니다. 각 CLI의 자체 설정(가져온 설정 포함)이 적용됩니다. 기본값이 이것입니다.
- 값을 넣으면 실행할 때마다 플래그로 덮어씁니다(예: pi `--thinking low`). 가져오기를 하면 해당 CLI는 자동으로 `native`가 됩니다.

## 권한 모드

| | full-auto (기본) | safe | read-only (리뷰) |
|---|---|---|---|
| Codex | `--dangerously-bypass-approvals-and-sandbox` | `-s workspace-write` | `-s read-only` |
| Claude | `--permission-mode bypassPermissions` | `acceptEdits` | `plan` |
| omp | `--approval-mode yolo` | `--approval-mode write` | `write` + `--tools read,grep,glob` |
| Pi | 승인 시스템 없음 | 동일 | `--tools read,grep,find,ls` |

- 작업마다 worktree를 하나씩 씁니다(`agent/<run-id>` 브랜치). 이 설정은 항상 켜져 있고 끌 수 없습니다.
- push, 머지, PR, 배포는 full-auto에서도 매번 사용자에게 확인합니다.
- full-auto에서는 에이전트가 박스 셸, 네트워크, 로그인 세션에 제한 없이 접근합니다.

## 테스트

[TESTING.md](TESTING.md)를 보세요. 검사 방법과 end-to-end 결과를 정리했습니다.

- 자동 라우팅: `route.py decide`가 규칙, config, rate limit 상태(`~/.grok-bot-agents/state.json`)를 보고 고릅니다. 한도에 걸린 CLI는 리셋 시각까지 건너뜁니다.
- 리뷰: `run-agent.sh --review DIR`은 그 자리에서 읽기 전용으로 실행합니다. 실행 전후 fingerprint가 다르면 실패로 처리합니다.

## 배포

public GitHub 레포에 push한 뒤 https://cursor.com/marketplace/publish 에 제출하고, 게시 대상에서 Grok Bot을 체크합니다. 리뷰는 사람이 직접 합니다.

## 미해결

- `minClientVersions`에 Grok Bot 키를 넣어야 하는지
- 앱의 multiSelect 질문 카드와 마스킹 시크릿 입력이 플러그인 스킬에서도 동작하는지
- 스킬끼리의 상대 경로(`../configure-agents/scripts/...`)가 설치된 플러그인 안에서도 유지되는지
- rate limit이 없는 계정에서 Claude Code end-to-end (Codex, Pi, omp는 확인됨)

## 라이선스

MIT © AgwaB
