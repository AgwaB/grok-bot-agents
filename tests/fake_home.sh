#!/bin/sh
# fake_home.sh <home> <canary>: fake Codex/Claude/Pi/omp homes whose secret values all contain <canary>. Test use only.
H=$1; C=$2
mkdir -p "$H/.codex" "$H/.claude/skills/s1" "$H/.pi/agent/extensions" "$H/.omp/agent"
cat >"$H/.codex/auth.json" <<EOF
{"OPENAI_API_KEY":null,"tokens":{"id_token":"$C-id","access_token":"$C-at","refresh_token":"$C-rt","account_id":"acc"},"last_refresh":"2026-10-01T00:00:00Z"}
EOF
printf '{"srv":{"access_token":"%s-mcp"}}\n' "$C" >"$H/.codex/.credentials.json"
cat >"$H/.codex/config.toml" <<EOF
model = "gpt-5"
cli_auth_credentials_store = "keyring"
experimental_bearer_token = "$C-bearer"
[model_providers.deep]
name = "Deep"
env_key = "DEEPSEEK_API_KEY"
[model_providers.vault.auth]
command = "security find-generic-password -w $C-cmd"
[mcp_servers.gh]
command = "npx"
env = { GITHUB_TOKEN = "$C-gh", LOG_LEVEL = "debug" }
[mcp_servers.remote]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "REMOTE_MCP_TOKEN"
[mcp_servers.remote2]
url = "https://mcp2.example.com/mcp"
[mcp_servers.remote2.env_http_headers]
"X-Api-Key" = "R2_KEY"
EOF
printf '{"claudeAiOauth":{"accessToken":"%s-cat","refreshToken":"%s-crt"},"mcpOAuth":{"x|1":{"accessToken":"%s-cm"}}}\n' "$C" "$C" "$C" >"$H/.claude/.credentials.json"
cat >"$H/.claude/settings.json" <<EOF
{"env":{"ANTHROPIC_API_KEY":"$C-ak","DISABLE_TELEMETRY":"1"},"apiKeyHelper":"/usr/local/bin/op read op://v/$C-helper"}
EOF
cat >"$H/.claude.json" <<EOF
{"oauthAccount":{"emailAddress":"a@example.com","accountUuid":"u"},"hasCompletedOnboarding":true,"userID":"$C-uid",
 "mcpServers":{"ctx":{"command":"npx","env":{"CTX_API_KEY":"\${CTX_API_KEY}"}},"lit":{"command":"x","env":{"SERVICE_TOKEN":"$C-lit"}},"web":{"type":"http","url":"https://m.example.com"}}}
EOF
NOWMS=$(($(date +%s)*1000))
cat >"$H/.pi/agent/auth.json" <<EOF
{"openai-codex":{"type":"oauth","access":"$C-pa","refresh":"$C-pr","expires":$((NOWMS+3600000))},
 "anthropic":{"type":"oauth","access":"$C-pa2","refresh":"$C-pr2","expires":$((NOWMS-1000))},
 "deepseek":{"type":"api_key","key":"!security find-generic-password -ws $C-ds"},
 "kimi":{"type":"api_key","key":"KIMI_API_KEY"},
 "groq":{"type":"api_key","key":"$C-groq"}}
EOF
printf '{"providers":{"alibaba":{"baseUrl":"https://x","apiKey":"DASHSCOPE_API_KEY"},"lit":{"apiKey":"%s-mlit"},"cmdp":{"apiKey":"!pass show %s-pass"}}}\n' "$C" "$C" >"$H/.pi/agent/models.json"
printf '{"packages":[]}\n' >"$H/.pi/agent/settings.json"
printf '{"accounts":[{"refresh":"%s-aa"}]}\n' "$C" >"$H/.pi/agent/anthropic-auth.json"
printf '{"exaApiKey":"%s-exa"}\n' "$C" >"$H/.pi/agent/web-search.json"
printf '{"t":"%s-tok"}\n' "$C" >"$H/.pi/agent/my-pkg-token.json"
printf 'KIMI_API_KEY=%s-kimi\nexport OTHER_KEY=%s-o\n' "$C" "$C" >"$H/.pi/agent/.env"
printf '{"legacy":"%s-legacy"}\n' "$C" >"$H/.pi/agent/oauth.json"
cat >"$H/.omp/agent/models.yml" <<EOF
providers:
  deepseek:
    baseUrl: https://api.deepseek.com
    apiKey: DEEPSEEK_API_KEY
  vault:
    apiKey: "!op read op://p/$C-omp"
  lit:
    apiKey: $C-omplit
    headers:
      X-Token: $C-hdr
EOF
cat >"$H/.omp/agent/config.yml" <<EOF
auth:
  broker:
    url: http://box:7777
    token: $C-broker
tools:
  approvalMode: yolo
EOF
printf '{"mcpServers":{"m":{"command":"node","env":{"M_TOKEN":"%s-m"}}}}\n' "$C" >"$H/.omp/agent/mcp.json"
mkdir -p "$H/.ssh"; printf '%s-ssh\n' "$C" >"$H/.ssh/id_rsa"
printf '[mcp_servers.linear]\nurl = "https://mcp.linear.app/mcp"\n' >>"$H/.codex/config.toml"
