# grok-bot-agents (v0.1.0)

English: [README.md](README.md)

Grok Bot 박스(클라우드 컴퓨터)에서 **Claude Code · Codex CLI · Pi · omp(Oh My Pi)** 를 설치하고 로그인하는 스킬 + 얇은 Python `gba` CLI 플러그인입니다. 버전은 **0.1.0** 그대로입니다.
가벼운 일은 Grok Bot이 직접 하고, **무거운 일은 코딩이든 아니든 CLI에 맡깁니다**. 리서치·분석, 데이터·파일 처리, 대량 변환, 긴 초안이나 번역, 로그 분석, 장시간 작업이 모두 해당됩니다.
맡기는 방식은 두 가지입니다. **직접 지목**("codex한테 시켜")하거나 Grok Bot이 **자동 라우팅**하게 둡니다.
실행은 헤드리스로 하고, 결과는 요약과 산출물 경로로 받아요.

## 흐름

1. **설치**: Cursor 마켓플레이스에서 설치합니다(Grok Bot 설정의 플러그인).
2. **세팅** (`/setup-agents`)
   - 0: 등록된 컴퓨터의 CLI와 로그인·설정을 감지합니다. 스캐너는 내용을 내부에서 읽지만 **이름만 출력**합니다.
   - 2-1: 쓸 CLI를 고릅니다(복수 선택 카드). 내 컴퓨터에 있는 것은 미리 체크돼 있습니다. 고른 것만 설치합니다.
   - 2-2: 가져오기 또는 에이전트별 로그인을 선택합니다. **파일 전체 단위 동의** 후 선택된 파일만 고유 스테이징 경로로 옮기며, CopyToBox마다 승인받습니다. 한 auth 파일의 일부 제공자만 선택할 수 없습니다. env 키는 내보내기 전에 이름별로 동의받아 `~/.agent-keys`(600)에 합칩니다. Claude/Codex 구독은 기본적으로 박스에서 별도 로그인합니다. 키/토큰은 **채팅에 쓰지 않습니다**.
   - 2-3: 기본 모델/추론은 **native**입니다. 단, 기존 중앙 override는 설정만 가져왔을 때 유지됩니다. `gba verify`는 오프라인 검사이고, 명시적 허가 후 `--live`를 붙여야 제공자 OK 테스트를 합니다.
3. **명시적 지시** (`/delegate-task`): 지목한 CLI로 바로 실행합니다. 일이 가벼워도 지목이 우선입니다.
4. **자동 라우팅**: 가벼운 일은 봇이 직접 하고, 무거운 일만 활성화된 CLI에 맡깁니다. 반복·대량 작업은 Pi/omp 저가 모델, 어려운 추론·코드는 Claude/Codex, 장시간 무인 작업은 omp, 리뷰는 만들지 않은 쪽 에이전트가 맡습니다. 10분 넘는 실행은 감시 봇이 30분씩 교대로 지켜봐서 박스가 잠들지 않게 하고, 멈추면 이어서 다시 돌립니다. 쓸 수 있는 CLI가 적으면 폴백 규칙을 따릅니다.
5. **설정 변경** (`/configure-agents`): 모델, 추론 레벨, 권한 모드, 라우팅, 로그인/키 교체, 각 CLI의 native config를 바꿉니다.

## 구조

```
grok-bot-agents/
├── .cursor-plugin/plugin.json
├── assets/logo.svg, logo.png
├── scripts/gba, gba_{import,setup,runs,doctor}.py
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
    │   ├── scripts/supervise.sh            # 장시간 실행 감시 (감시 봇 30분 교대, 박스 유휴 정지 대비)
    │   ├── scripts/resume-run.sh           # 멈춘 실행: 진행분 커밋 후 이어서 재실행
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

- `ListMachines`로 컴퓨터를 고르고 이름만 표시하는 감지를 합니다. `gba import inventory` → `plan` → 승인된 파일별 CopyToBox → `advance` → `verify`로 진행하며, `status ID`로 재개합니다. SHA256으로 선택한 바이트를 확인하고 빠진/추가/변경 파일은 차단합니다. 심볼릭 링크나 외부 Pi 패키지는 모든 포함 파일에 동의한 경우에만 기존 bundle 경로를 별도로 검토합니다. Windows는 덜 검증됐으며 POSIX export/bundle 대체물이 없습니다. 실패하면 박스 로그인/마스킹 키 입력을 사용합니다.
- `import_plan.py`가 OS와 홈 경로를 받아 가져올 파일 목록을 출력합니다(파일은 읽지 않음). `apply_import.sh`가 `/workspace/uploads/agent-import/<cli>/`에서 설치합니다.
  - 허용 목록에 있는 파일만 설치하고, 권한은 600/700으로 맞춥니다.
  - 기존 파일과 다르면 멈춥니다. `--overwrite`를 주면 `*.bak.<ts>`로 백업한 뒤 덮어씁니다.
  - 홈 경로를 박스 경로로 바꾸고, Claude `~/.claude.json`에서는 `mcpServers`만 병합합니다.
  - 기존 직접 apply는 **인증 파일을 가져오고 상태 확인을 통과했을 때만** `auth: imported`와 native를 기록합니다. `gba import`는 오프라인 적용이므로 중앙 설정을 유지하고, 별도 verify 성공 후 해당 작업 스테이징만 지웁니다.

| CLI | 가져오는 것 | 메모 |
|---|---|---|
| Codex | `config.toml`(박스에서는 파일 저장소로 고정), `AGENTS.md`, `prompts/`, `skills/`, `rules/`, API 키 방식 `auth.json` | ChatGPT 로그인은 기본으로 박스에서 `codex login --device-auth`. `auth.json`(또는 키링) 복사는 옵션입니다 |
| Claude Code | `settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `hooks/`, 플러그인, `~/.claude.json`의 `mcpServers` | 로그인은 기본으로 박스에서 `claude auth login`. 파일이나 macOS 키체인(`Claude Code-credentials`) 복사는 옵션입니다 |
| Pi | `~/.pi/agent/auth.json`, 패키지 인증 파일(`*auth*.json`, `*token*.json`, `web-search*.json` 등), `settings.json`, `models.json`, 패키지 (+ `AGENTS.md`, `prompts/`, `skills/`, `extensions/`) | Pi는 `@earendil-works/pi-coding-agent`(Node ≥ 22.19)를 씁니다. 첫 실행 때 settings의 npm 패키지를 자동 설치합니다 |
| omp | `~/.omp/agent/agent.db` (로그인·설정 SQLite), `config.yml`, `models.yml`, `mcp.json` | 먼저 omp를 종료합니다. `-wal`/`-shm` 파일이 있으면 같이 가져옵니다 |

세션, 히스토리, 로그, 캐시, SSH 키, 브라우저 프로필 로그인, 클라우드 자격 증명 등은 가져오지 않습니다. env 변수나 `!command`/`op://`로 쓰던 API 키는 동의 카드에서 고른 것만 `export_secrets.sh`로 내보내 박스 `~/.agent-keys`에 합칩니다.
같은 로그인을 두 컴퓨터에서 쓰게 되면, OAuth 토큰이 갱신될 때 한쪽이 로그아웃될 수 있습니다. 그래서 Claude Code와 Codex는 기본으로 박스에서 따로 로그인합니다.

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
- 값을 넣으면 실행할 때마다 덮어씁니다(예: pi `--thinking low`). 설정만 가져오면 기존 override가 유지되므로 자동으로 native가 된다고 약속하지 않습니다. Pi 추론은 `max`도 지원합니다.

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

## gba CLI

Python **3.11+ 표준 라이브러리만** 사용합니다. 각 SKILL.md의 실제 절대 경로에서 `../..`를
플러그인 루트로 계산해 `python3 <root>/scripts/gba`를 실행합니다. 스킬만 따로 복사했다면
전체 플러그인을 가리키는 절대 `GBA_ROOT`가 필요합니다. Shell cwd에 의존하거나 없는 경로를 추측하지 않습니다.

- `setup plan|apply --agents pi,codex`: plan은 파일 생성/CLI 실행/다운로드 없는 계획입니다.
  apply만 선택한 CLI를 사용자 경로에 설치합니다. Linux/glibc 박스용이며 Node SHA256/npm SHA512
  무결성을 확인하고 install 스크립트는 비활성화합니다. 기존 설정/미선택 CLI는 보존합니다.
- `import inventory --agents … [--exported-keys]`: 소스에서 이름/해시만 수집합니다.
  `import plan --inventory 파일 [--selection 선택파일]`, `advance ID`, `status ID`, `verify ID`를 사용합니다.
  제공자별 부분 복사는 거부합니다. omp DB/WAL/SHM은 한 동의 단위입니다.
  코드 신뢰 승인은 `--approve-runtime`, 덮어쓰기는 `--overwrite`, 중단 후 재시도는 `--retry-interrupted`입니다.
- `runs list --open`, `runs next ID`, `runs recover ID`: 원래 입력/시간 제한, 계보/시도 횟수,
  감시 봇 인계를 저장합니다. **계보 전체 자동 재시작 2회** 이후에는 사용자 동의와
  `--allow-extra-restart`가 필요합니다. `--native`는 별도 선택입니다. 실제 감시 봇 배치/30분 교대는 봇이 합니다.
- `doctor --json`, `verify --agents …`: 설정/실행 파일 버전/키 이름 존재 여부/parity 오프라인 검사입니다.
  `verify --live`만 실제 제공자 호출을 하며 quota 부족은 성공으로 간주하지 않습니다.

출력은 `schema:1`, `status:ok|blocked|error`, 고정 `reason`, `next_action`, `requires_approval`,
`action_owner`를 담은 짧은 JSON입니다. 종료 코드는 각각 0/3/2입니다. 다음 행동은 **봇에게 주는 인계**이며
자동 실행 명령이나 사용자 승인 증거가 아닙니다. [영문 계약/입력 예시](README.md#gba-cli)를 참고하세요.
상태는 `~/.grok-bot-agents/imports/import-<uuid>/`에 비공개로 저장합니다. 중단된 apply는 효과를 검토한
뒤 명시적으로 재개해야 하며 전체 롤백/정확히 한 번 실행을 보장하지 않습니다.
`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR` 변경 시 bundle 대신 파일별 복사를 사용합니다.
`agents_config.py set`은 비밀키 패턴/긴 고엔트로피 값을 모든 필드에서 거부하며 값 대신 필드 수만 출력합니다.
휴리스틱이므로 오탐/미탐 가능성이 있습니다. 브리지의 command/git 응답은 기록만 하고 **outbox만 구현**돼 있습니다.
웹훅의 `reply_command`는 절대 실행하지 않습니다. 실설치/제공자/네이티브 플랫폼 테스트는 하지 않았습니다.

## 테스트

[TESTING.md](TESTING.md)를 보세요. 검사 방법과 end-to-end 결과를 정리했습니다.

- 자동 라우팅: `route.py decide`가 규칙, config, rate limit 상태(`~/.grok-bot-agents/state.json`)를 보고 고릅니다. 한도에 걸린 CLI는 리셋 시각까지 건너뜁니다.
- 리뷰: `run-agent.sh --review DIR`은 그 자리에서 읽기 전용으로 실행합니다. 실행 전후 fingerprint가 다르면 실패로 처리합니다.

## 배포

public GitHub 레포에 push한 뒤 https://cursor.com/marketplace/publish 에 제출하고, 게시 대상에서 Grok Bot을 체크합니다. 리뷰는 사람이 직접 합니다.

## 미해결

- `minClientVersions`에 Grok Bot 키를 넣어야 하는지
- 앱의 multiSelect 질문 카드와 마스킹 시크릿 입력이 플러그인 스킬에서도 동작하는지
- 마켓플레이스/독립 복사 설치에서 절대 GBA_ROOT 탐색의 실제 호스트 스모크 테스트
- rate limit이 없는 계정에서 Claude Code end-to-end (Codex, Pi, omp는 확인됨)

## 라이선스

MIT © AgwaB
