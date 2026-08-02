# 실행과 검증 가이드

이 문서는 Barrier-Free Mobility Agent를 로컬에서 실행하고 검증하는 전체 절차를 다룹니다.
제품 개요는 [README](../README.md), 설계 배경은 [아키텍처](architecture.md)를 참고하세요.

## 로컬 실행

의존성을 설치합니다.

~~~powershell
uv sync --locked
~~~

먼저 별도 터미널에서 MCP를 mock mode로 실행합니다.

~~~powershell
Set-Location C:\project\barrier_free_mobility_mcp
.\scripts\start_local_mcp.ps1 -AppMode mock -CacheBackend memory
~~~

Agent API를 실행합니다.

~~~powershell
Set-Location C:\project\barrier_free_mobility_agent
uv run mobility-agent
~~~

세 번째 터미널에서 같은 제품의 Web UI와 BFF를 실행합니다.

~~~powershell
Set-Location C:\project\barrier_free_mobility_agent\web
Copy-Item .env.example .env.local
npm ci
npm run dev
~~~

`web/.env.local`에는 서로 다른 32 bytes 이상의 `AGENT_BFF_SHARED_SECRET`과
`AGENT_ANONYMOUS_SESSION_SECRET`을 설정하고, `WEB_PUBLIC_ORIGIN`을 브라우저에서 사용할
정확한 origin(기본 예: `http://127.0.0.1:3000`)으로 맞춰야 합니다. native Agent의 기본
`API_OWNER_MODE=disabled`는 한 local owner로 동작합니다. BFF owner 격리를 함께 확인하려면
Agent에 `API_OWNER_MODE=trusted_header`와 Web의 `AGENT_BFF_SHARED_SECRET`과 같은
`API_OWNER_SHARED_SECRET`을 설정합니다.

브라우저에서 `http://127.0.0.1:3000/assistant`를 엽니다. Browser는 FastAPI port를 직접
호출하지 않으며 Next.js same-origin BFF가 server-only `AGENT_INTERNAL_URL`을 사용합니다.

상태와 질문 API를 확인합니다. 아래 직접 호출은 native 기본값인
`API_OWNER_MODE=disabled`에서만 사용합니다. `trusted_header` mode에서는 browser가 내부
header를 만들지 않고 Web BFF를 통해 호출해야 합니다.

~~~powershell
Invoke-RestMethod http://127.0.0.1:8080/health/ready

$body = @{
    question = "휠체어를 사용하고 계단과 에스컬레이터를 이용할 수 없어요. 홍대입구역에서 삼성역까지 갈 수 있어?"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/accessibility/questions -ContentType "application/json; charset=utf-8" -Body $body
~~~

## 다중 턴 clarification

thread를 만든 뒤 run을 제출합니다.

~~~powershell
$thread = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/threads

$runBody = @{
    question = "홍대입구역에서 삼성역까지 갈 수 있어?"
} | ConvertTo-Json

$run = Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8080/v1/threads/$($thread.thread_id)/runs" `
    -ContentType "application/json; charset=utf-8" `
    -Body $runBody
~~~

`run_status=interrupted`이면 같은 run을 재개합니다.

~~~powershell
$resumeBody = @{
    answer = "계단과 에스컬레이터를 모두 이용할 수 없어요"
    clarification_id = $run.clarification.id
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8080/v1/runs/$($run.run_id)/resume" `
    -ContentType "application/json; charset=utf-8" `
    -Body $resumeBody
~~~

native 기본값의 `persistence=ephemeral`은 checkpoint가 프로세스 재시작 시 사라진다는 뜻입니다. PostgreSQL 설정 또는 아래 Compose 제품 경로에서는 `persistence=durable`을 반환하고 성공적으로 저장된 clarification을 재시작 뒤 같은 run에서 이어갑니다. Phase 4B-2 Compose 경로에서는 API가 encrypted job을 원자적으로 예약하고 별도 worker가 lease로 실행합니다. worker crash 뒤에는 terminal snapshot, checkpoint cursor와 open Tool event를 함께 분류하며, 모호한 Tool 구간은 자동 재호출하지 않고 recovery-required로 중단합니다.

## PostgreSQL을 포함한 통합 제품 실행

MCP를 먼저 실행한 뒤 Agent, Web과 PostgreSQL을 한 번에 시작합니다.

~~~powershell
Set-Location C:\project\barrier_free_mobility_agent
docker compose up --build
~~~

기본 진입점은 `http://127.0.0.1:3000/assistant`이며 Agent와 PostgreSQL port는 외부에
공개하지 않습니다. Compose의 기본 password와 session secret은 local demo 전용입니다.
공유 환경에서는 `POSTGRES_PASSWORD`, `AGENT_BFF_SHARED_SECRET`,
`AGENT_ANONYMOUS_SESSION_SECRET`, `EXECUTION_PAYLOAD_ENCRYPTION_KEY`,
`IDEMPOTENCY_HMAC_KEY`를 서로 다른 secret store 값으로 반드시 덮어씁니다. payload key는 정확히
32 UTF-8 bytes이고 HMAC key는 최소 32 bytes여야 합니다.

다른 local origin을 사용할 때는 공개 port와 canonical origin을 함께 설정합니다. 두 값이
다르면 POST/DELETE 요청은 CSRF 검사에서 거부됩니다.

~~~powershell
$env:WEB_PORT = "4317"
$env:WEB_PUBLIC_ORIGIN = "http://localhost:4317"
docker compose up --build
~~~

`127.0.0.1`을 사용할 때도 `WEB_PUBLIC_ORIGIN=http://127.0.0.1:4317`처럼 브라우저 URL과
동일하게 설정합니다. Compose는 값을 생략하면 `WEB_PORT`를 반영한
`http://127.0.0.1:<port>`를 Web container에 주입합니다.

현재 Compose의 일반 운영값은 Agent API와 worker 각각 한 개입니다. Worker는 container hostname 기반
auto identity, 별도 registry heartbeat와 mode-0600 token·generation state를 사용합니다. 애플리케이션
부팅 경로를 로드하지 않는 PostgreSQL read-only Docker healthcheck와
`mobility-agent worker-health`는 이 process-local identity까지 같은 조건으로 확인합니다. 신규 durable mutation은 ready Worker가
없을 때 원자적으로 거부됩니다. Phase 4B-3B의 복수 claim 경쟁, stale write, checkpoint fencing과
hard-kill recovery core chaos gate는 완료했지만 장시간 queue/lease/recovery budget과 운영 절차를
검증하는 Phase 4B-3C release profile을 실제 통과하기 전에는 replica 제한을 풀지 않습니다. 현재 anonymous session은 local/demo의
객체 격리 수단이며 end-user 인증이 아니므로 delegated OIDC 전에는 hosted UI로 취급하지
않습니다.

## Worker replica release gate

gate는 기존 Compose 제품이 Agent 1개·Worker 1개로 모두 healthy이고 Web이 operational인
local/test 환경에서만 시작합니다. 실행 중 Agent는 1개로 고정하고 Worker만 2개로 확장하며, 종료
성공 여부와 관계없이 PostgreSQL을 복구하고 Worker를 원래 1개로 되돌린 뒤 생성한 thread를 삭제합니다.
rolling 중 peer readiness는 Docker health polling이 아니라 Worker container 내부의 long-lived exact
registration watcher가 250ms마다 process-local token·generation과 DB clock 기준 registry row를 함께
검증합니다. queue aggregate는 Agent container 내부의 long-lived `execution_health_watch` stream으로
smoke 2초·release 5초 간격에 수집하며 Agent recreate 뒤에는 stream을 다시 열고 fresh 표본을 요구합니다.

```powershell
.\scripts\run_replica_release_gate.ps1 -Profile smoke
.\scripts\run_replica_release_gate.ps1 -Profile release
```

| profile | 관측 시간 | 예정 workflow / 평가 하한 | 부하 | 장애 주입 | release 판정 |
|---|---:|---:|---|---|---|
| `smoke` | 10분 | 240 / 30 | 동시 2, 5초 간격 | Worker roll 3, Agent recreate 2, PostgreSQL outage 1 | 불가 |
| `release` | 4시간 | 2,880 / 600 | 동시 4, 20초 간격 | Worker roll 12, Agent recreate 6, PostgreSQL outage 3 | 모든 조건 통과 시 가능 |

두 profile 모두 queue age 15초 초과를 warning, 60초 초과를 failure로 평가하고 stuck 기준은 120초,
장애 후 수렴은 90초, 최종 drain은 120초로 제한합니다. expired lease, 예상하지 않은 recovery,
terminal ciphertext 잔존, 중복 terminal, ambiguous recovery 또는 최종 queued/leased job이 하나라도
있으면 실패합니다. release profile은 image build를 생략할 수 없고 PostgreSQL outage는 최소 20초입니다.
결과는 `artifacts/release-gates/phase-4b-3c-<UTC>.json`에 atomic write되며 ID, 질문·답변,
payload·ciphertext, token, command output과 raw log를 포함하지 않습니다. 새 watcher 경로를 적용한
smoke 재검증은 대기 중이고 정식 4시간 release도 미실행·미승인이므로 기본 replica 수는
변경하지 않습니다.

## Hosted 배포 전제

`WEB_APP_ENV=staging | production`에서는 `WEB_PUBLIC_ORIGIN`에 browser가 사용하는 정확한
HTTPS origin을 설정해야 하며 mutation의 Origin header가 없거나 다르면 BFF가 403으로
거부합니다. hosted `AGENT_INTERNAL_URL`도 HTTPS가 기본입니다. private network 또는 service
mesh가 내부 HTTP를 별도로 보호한다고 검증한 경우에만 `AGENT_ALLOW_PRIVATE_HTTP=true`를
사용합니다.

배포 runtime role에서 DDL을 분리할 때는 migration job에서 동일 image에 PostgreSQL 설정과
암호화 key를 주입해 `mobility-agent migrate`를 먼저 실행하고, application에는
`DATABASE_AUTO_SETUP=false`를 설정합니다. application startup은 필요한 repository와
LangGraph checkpoint table, 양쪽 migration의 exact version과 index validity가 맞지 않으면
요청을 받기 전에 실패합니다. readiness는 data table에 필요한 DML 권한도 확인합니다.

`EncryptedSerializer`는 serializer를 통과하는 checkpoint value를 보호하지만 saver의 모든
scalar channel·metadata와 `agent_runs` JSONB 전체를 application-level로 암호화하지는
않습니다. staging/production은 `sslmode=verify-full`인 `DATABASE_URL`,
`DATABASE_AT_REST_ENCRYPTION_CONFIRMED=true`와 16, 24 또는 32 UTF-8 bytes의
`CHECKPOINT_ENCRYPTION_KEY`를 모두 요구하며, 하나라도 충족하지 않으면 시작하지 않습니다.
key fingerprint가 DB에
기록된 값과 다를 때도 startup에 실패합니다. at-rest 확인 값은 배포 플랫폼의 실제
TDE/disk encryption 정책을 검토한 뒤에만 설정해야 합니다.

## MCP OIDC 인증

운영 환경의 MCP 인증은 OAuth 2.0 client credentials를 지원합니다. secret은 배포 환경의 secret store로 주입하고 저장소에 커밋하지 않습니다.

~~~dotenv
MCP_MOBILITY_AUTH_MODE=oidc
MCP_MOBILITY_OIDC_TOKEN_URL=https://identity.example.com/oauth/token
MCP_MOBILITY_OIDC_CLIENT_ID=mobility-agent
MCP_MOBILITY_OIDC_CLIENT_SECRET=replace-from-secret-store
MCP_MOBILITY_OIDC_SCOPE=mcp.read
MCP_MOBILITY_OIDC_REFRESH_LEEWAY_SECONDS=30
~~~

token endpoint는 외부 호스트에서 HTTPS가 필수이며, 평문 HTTP는 `localhost`, loopback 주소와 `host.docker.internal`에서만 허용됩니다. access token은 만료 전에 갱신되며 동시 요청은 하나의 갱신 작업을 공유합니다. `MCP_MOBILITY_OIDC_SCOPE`는 선택 설정입니다.

## 검증

일반 품질 게이트:

~~~powershell
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts/run_contract_tests.py
uv run python -m pytest --cov
uv build
docker compose config --quiet
~~~

실제 MCP mock 서버와 Agent Uvicorn을 자동으로 띄우는 contract/E2E 검증:

~~~powershell
.\scripts\run_contract_tests.ps1
~~~

Linux/macOS나 CI에서는 같은 Python 실행기를 직접 사용할 수 있습니다.

~~~shell
uv run --locked python scripts/run_contract_tests.py \
  --mcp-project ../barrier_free_mobility_mcp
~~~

실행기는 기존 MCP의 환경 설정과 무관하게 mock mode, memory cache, loopback host와 무인증을 child process에 강제하고 종료 시 두 서버를 정리합니다. CI도 검증된 MCP commit SHA를 별도로 checkout해 동일한 contract와 실제 HTTP smoke를 실행합니다. MCP pin은 dependency upgrade PR에서만 변경하며 contract가 통과해야 병합할 수 있습니다.

Web 계층의 게이트는 `web/`에서 실행합니다.

~~~powershell
Set-Location web
npm run contracts:check
npm run lint
npm run typecheck
npm run test
npm run test:e2e
~~~
