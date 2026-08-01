# Barrier-Free Mobility Agent

교통약자의 자연어 이동 질문을 이해하고 Barrier-Free Mobility MCP의 도구를 안전하게 조합하는 웹 기반 AI Agent 제품입니다.

이 저장소는 사용자 Web UI, same-origin BFF, FastAPI/LangGraph runtime, 여러 턴의 문맥과 운영 surface를 하나의 제품 경계로 관리합니다. Next.js source는 root `web/`, Python source는 `src/barrier_free_mobility_agent/`에 둡니다. MCP 서버만 별도 독립 프로젝트이며, 이 제품은 MCP의 도메인 판단을 다시 구현하지 않습니다.

## 설계 목표

- 일반 MCP 도구 호출과 교통약자 전용 워크플로를 분리합니다.
- 이동 가능성 판단은 자유로운 ReAct 루프가 아니라 제한된 LangGraph 경로로 실행합니다.
- MCP의 `user_message`, `PARTIAL`, `limitations`, `risk_level=UNKNOWN` 의미를 보존하고,
  `failed_sources`는 내부 Tool 이름 대신 안정적인 공개 코드와 표시명으로 투영합니다.
- 사용자 이동 조건은 구조화된 상태로 유지하고, 장기 기억은 명시적으로 저장에 동의한 선호만 보관합니다.
- 도구 수, 모델 호출 수, 그래프 단계 수, 토큰과 실행 시간을 요청별 예산으로 제한합니다.
- 모든 판단에는 도구 실행 이력, 데이터 시각, 실패 원인과 모델·프롬프트 버전을 추적할 수 있게 합니다.

## 책임 경계

| 웹 기반 Agent 제품 | Barrier-Free Mobility MCP |
|---|---|
| Assistant UI, same-origin BFF와 운영 Console | 독립 MCP 서버와 Tool 계약 |
| 대화 문맥과 사용자 이동 조건 유지 | 역·장소 정규화 |
| 의도 분류와 명확화 | 공공 API 연동과 캐시 |
| 허용된 Tool playbook 선택 | 경로·시설 데이터 정규화 |
| 복합 질의의 순서·병렬 실행 | 결정론적 위험 점수와 신뢰도 계산 |
| 재시도·중단·복구·스트리밍 | 부분 실패와 근거가 포함된 구조화 결과 |
| 응답 정책, 감사 로그와 관측 | 최종 사용자용 canonical `user_message` |

## 기준 기술 스택

- Python 3.13, `uv`, `pyproject.toml`, `uv.lock`
- LangGraph Graph API, LangChain, `langchain-mcp-adapters`
- FastAPI, Pydantic v2, SSE
- TypeScript strict, Next.js App Router, same-origin BFF (`web/`)
- PostgreSQL 기반 `AsyncPostgresSaver`와 owner-aware conversation repository
- OpenTelemetry와 구조화 로그, 선택적 LangSmith tracing/evaluation
- pytest, pytest-asyncio, Ruff, mypy, Testcontainers

의존성의 minor 범위는 `pyproject.toml`에 제한하고, 실제 실행 버전은 `uv.lock`으로 고정합니다. MCP Python SDK 2.x는 안정화 전까지 사용하지 않습니다.

## 문서

- [아키텍처](docs/architecture.md)
- [구현 계획](docs/implementation-plan.md)
- [제품 UI/UX 설계](docs/product-design.md)
- [제품 정보 구조](docs/information-architecture.md)
- [사용자 흐름](docs/user-flows.md)
- [Assistant·Console wireframe](docs/wireframes.md)
- [공개 서비스 상태 계약](docs/public-status.md)
- [Agent Console 계약](docs/ops-console.md)
- [디자인 토큰](docs/design-tokens.json)
- [ADR-0001: 기술 스택](docs/adr/0001-python-langgraph-stack.md)
- [ADR-0002: 제어된 Tool orchestration](docs/adr/0002-controlled-mcp-orchestration.md)
- [ADR-0003: Web과 BFF 경계](docs/adr/0003-web-frontend-boundary.md)
- [ADR-0004: capability-gated MCP extension](docs/adr/0004-capability-gated-mcp-extensions.md)
- [ADR-0005: durable conversation과 owner 경계](docs/adr/0005-durable-conversation-ownership.md)
- [ADR-0006: 구조화 문맥 projection과 token budget](docs/adr/0006-bounded-model-context-and-token-accounting.md)
- [ADR-0007: Hosted model 후보의 활성화 전 평가 경계](docs/adr/0007-hosted-model-candidate-evaluation.md)
- [ADR-0008: 제품 신뢰 surface와 운영 surface 경계](docs/adr/0008-product-trust-and-operations-surfaces.md)
- [ADR-0009: 구조화된 clarification 공개 계약과 호환 전환](docs/adr/0009-structured-clarification-contract.md)
- [ADR-0010: Durable run ledger와 crash window 의미론](docs/adr/0010-durable-run-ledger-and-crash-semantics.md)
- [ADR-0011: 단계형 durable execution 조정 계약](docs/adr/0011-staged-durable-execution-coordination.md)
- [Durable run ledger 운영 runbook](docs/runbooks/durable-run-ledger.md)
- [Durable execution worker 운영 runbook](docs/runbooks/durable-execution-worker.md)
- [Structured model offline evaluation](evals/README.md)

## 현재 구현

Phase 0/1 수직 기능, Phase 2A bounded trip workflow, Phase 2B 공개 projection, Phase 2C capability-gated facility workflow, Phase 3A 통합 Web vertical slice, Phase 3B durable conversation·bounded state/accounting 기반선, Phase 3C-3 structured clarification, Phase 4A durable run ledger, Phase 4B-1 repository coordination substrate, Phase 4B-2 single-worker durable execution, Phase 4B-3A worker readiness·admission safety, Phase 4B-3B core multi-replica chaos safety와 Phase 4B-3C Worker replica release-gate 자동화까지 구현되어 있습니다. ADR-0009의 response schema v4, schema v3 호환 projection과 typed resume은 API·graph·Web·PostgreSQL restart 경계를 포함해 통합 검증했습니다. 동일 저장소의 `web/`은 공용 제품 route, `/assistant`, same-origin BFF, versioned response 검증, clarification/resume과 접근 가능한 결과 표현을 제공하며 Python runtime과 하나의 제품 gate로 검증됩니다.

Phase 4A durable run ledger는 [ADR-0010](docs/adr/0010-durable-run-ledger-and-crash-semantics.md)의 계약에 따라 구현·검증을 완료했습니다. public `ConversationRun` snapshot과 내부 running execution을 분리하고 graph 전 run 예약, append-only `run_events`, immutable Tool terminal summary와 crash-window 탐지를 제공합니다. 이 완료는 장애 사실을 보존하고 open execution을 탐지한다는 뜻이며, exactly-once, worker, lease, 자동 replay·repair, public event route, SSE나 `/ops`를 제공한다는 의미는 아닙니다.

Phase 4B는 [ADR-0011](docs/adr/0011-staged-durable-execution-coordination.md)에 따라 단계적으로 진행합니다. Phase 4B-1 substrate 위에 Phase 4B-2가 BFF start/resume idempotency key, atomic submit, AES-256-GCM private payload, lease-bound Tool/finalization transaction, 별도 worker, bounded wait와 exact checkpoint recovery를 연결합니다. Phase 4B-3A는 DB-clock 기반 Worker registry와 idle heartbeat, token·generation fencing, 독립 Docker healthcheck와 redacted queue health를 추가했습니다. ready Worker가 없으면 신규 durable 예약은 run/event/job을 남기지 않고 retryable `503 EXECUTION_WORKER_UNAVAILABLE`로 fail closed하며, 이미 예약된 동일 idempotency 요청의 조회·결합은 계속 허용합니다. Phase 4B-3B는 replica별 hostname auto identity와 process-local token·generation health proof, exact registration에 결합된 claim, lease로 직렬화한 LangGraph checkpoint write와 checkpoint execution provenance, forced-stop lease revocation을 추가했습니다. 실제 PostgreSQL의 복수 claim 경쟁·stale delayed write·checkpoint row-lock 경계와 별도 process hard-kill 뒤 재claim을 검증했고, Compose에서 서로 다른 auto identity의 Worker 두 개가 동시에 ready임을 확인했습니다.

Phase 4B-3C는 local/test Compose에서 Agent를 1개로 고정하고 Worker 2개의 운영 적합성만 판정하는 자동화입니다. 고정된 smoke·release profile, 반복 Worker rolling, Agent recreate, PostgreSQL 일시 장애, Worker 부재 시 admission fail-closed, DB-clock queue 표본과 누적 불변식, redacted JSON evidence와 원래 토폴로지 복구를 하나의 gate로 묶습니다. Worker rolling에서는 peer Worker를, Agent recreate에서는 두 Worker 모두를 container 안의 long-lived watcher로 감시합니다. watcher는 process-local token·generation과 PostgreSQL DB clock의 exact fenced registration을 250ms마다 확인합니다. queue는 Agent container 안의 long-lived `execution_health_watch` stream 하나로 smoke 2초·release 5초마다 관측합니다. 이 watcher 경로를 적용한 smoke 재검증은 아직 대기 중이고 정식 4시간 release profile도 미실행·미승인입니다. smoke는 통과하더라도 `release_eligible=false`입니다. 따라서 기본 Compose와 일반 제품 실행은 계속 Agent 1개·Worker 1개입니다. Worker 2개 기본값은 release profile을 실제 통과하고 결과를 검토한 뒤에만 별도 변경하며, key 없는 동기 호환 경로가 process-local인 동안 Agent replica는 1개로 유지합니다. 기본 bounded wait 60초는 MCP Tool timeout 45초보다 길고 BFF deadline 90초보다 짧습니다. 현재 read-only MCP 호출은 recovery 경계에서 at-least-once이고 end-to-end exactly-once를 주장하지 않습니다.

PostgreSQL schema v9은 v8 deletion fence에 더해 fenced Worker registry를 유지합니다. thread 삭제는
active job과 새 reservation을 막은 뒤 checkpoint와 owner-scoped metadata를 정리하고, Worker 등록은
DB 시각의 heartbeat 만료와 token·generation CAS로 이전 process의 늦은 write를 거부합니다.
인증·schema 검증에 실패한 encrypted job은 terminal로 격리·scrub하므로 손상 job 한 건이 queue
전체를 막지 않습니다.

- Python 3.13과 uv 기반 src layout
- FastAPI application factory와 Uvicorn 실행 진입점
- liveness/readiness 분리와 요청 ID 전파
- MCP health·Tool schema 검증 및 schema fingerprint
- Streamable HTTP session을 통한 answer_accessibility_question 호출
- MCP structuredContent의 typed DTO 검증
- canonical user_message와 PARTIAL·risk_level=UNKNOWN 보존
- MCP 원본 모델과 분리된 Agent 공개 응답
- 입력·Tool·transport·timeout·contract 오류 분류
- static Bearer와 갱신 가능한 OIDC client-credentials 인증
- 실제 MCP mock 서버를 이용한 contract test와 HTTP end-to-end smoke
- 일반 품질, 실제 MCP contract/E2E, container image를 분리한 CI 게이트
- JSON checkpoint state만 사용하는 LangGraph trip subgraph
- `true | false | unknown`과 출처를 보존하는 Agent mobility context
- 고정 Tool policy 기반 `resolve_station → generate_accessibility_brief` 실행
- turn별 graph/Tool/model/parallel budget, run 누적 실행 카운터와 모델 호출 없는 deterministic pre-router
- local `InMemorySaver`와 PostgreSQL `AsyncPostgresSaver` 기반 clarification interrupt/resume
- 역 선택 후보를 Agent 공개 DTO로 제한해 제공하고 MCP clarification도 같은 run에서 재개
- flow `mobility-v2`, state schema 3의 structured clarification과 v1 thread mismatch 차단
- active answer request 검증 뒤 typed LangGraph `Command`로 trip/mobility slot을 원자 적용하는 resume
- checkpoint candidate order에 직접 매핑하는 opaque station option과 MCP 미호출 `unknown` terminal branch
- 외부 호출 전 Tool budget 예약과 resume 중 실패한 run의 terminal 상태·checkpoint ledger 보존
- owner-aware thread/run 생성·조회·재개·삭제와 PostgreSQL metadata repository
- exact version migration, run payload schema version, strict non-pickle serializer와 checkpoint 직렬화 payload의 선택적 AES 보호
- 재시작 뒤 같은 run을 조회·resume하는 PostgreSQL 통합 테스트, startup schema·index 검증과 readiness 권한 확인
- HttpOnly signed anonymous session, BFF 내부 owner ID·shared-token 전달과 객체별 404 격리
- run별 immutable `ConversationContextView`, 단일 `ClarificationView`와 `AnswerPresentation`
- 오래된 화면의 resume을 막는 clarification ID 검증과 공개 evidence allowlist
- MCP facility extension의 deep schema 검증과 readiness capability 공개
- capability가 있는 환경에서만 실행되는 엘리베이터·장애인 화장실 bounded subgraph
- trip/facility intent별 context 격리와 discriminated result presentation
- 모든 graph input·node update와 interrupt payload에 적용하는 canonical UTF-8 65,536-byte hard gate
- provider-neutral `ModelInvocation`·`StructuredModel`·`TokenCounter`와 local/test용 `fallback-v1` estimator
- checkpoint authority인 strict `RunUsage`와 PostgreSQL repository schema 4의 revision-safe usage read model
- 허용된 저카디널리티 label만 받는 Noop·Recording telemetry sink
- Tool 선택 필드를 받지 않는 strict `StructuredIntentDecision`과 ephemeral `ModelContextEnvelope`
- terminal outstanding call을 남기지 않는 checkpoint-safe model reservation·usage 정산 코어
- 128개 synthetic 한국어 corpus, normalized recording과 credential-free offline model evaluation gate
- immutable OpenAI Responses 후보 contract, exact wire count, retry 0 adapter와 opt-in live evaluation gate
- `/`, `/how-it-works`, `/status`, `/accessibility` public shell과 `/ops`가 제외된 navigation·sitemap
- raw readiness를 5초·64 KiB 경계의 strict `PublicServiceStatus`로 축약하는 BFF projection
- public status 진입 GET·명시 refresh와 Assistant capability gate의 단일 공개 상태 계약
- replica hostname auto identity와 process-local token·generation을 함께 검사하는 Worker health proof
- exact Worker registration에 결합된 job claim과 forced-stop active lease revocation
- current lease row를 잠그는 LangGraph checkpoint write fence와 job·generation checkpoint provenance
- constructor dependency injection으로만 열리는 deterministic Worker fault boundary
- 실제 PostgreSQL 경쟁·stale write·checkpoint lock과 subprocess hard-kill recovery chaos gate

기존 단일 질문 endpoint와 legacy resume request는 유지합니다. header가 없는 Agent API는 trip/facility projection union이 포함된 schema v3을 반환하고, 같은 제품의 BFF는 `X-Agent-Response-Schema: 4`로 structured clarification v4를 선택합니다. Structured field answer는 자연어 parser를 거치지 않고 `mobility-v2` graph의 기존 trip/mobility slot에 적용됩니다. 시설 workflow는 MCP의 `facility_question` capability가 deep schema gate를 통과한 환경에서만 활성화됩니다. Web의 시설 진입점은 readiness 전체와 MCP dependency가 모두 ready이고 이 capability가 있을 때만 열립니다. 현재 CI가 고정한 MCP SHA는 trip-only이므로 일반 배포 범위는 이동 조회이며, 별도 MCP 프로젝트의 facility 변경이 커밋되고 pin이 갱신된 뒤 시설 UI를 활성화합니다. Compose 제품 경로는 PostgreSQL에 run metadata, LangGraph checkpoint와 암호화된 durable job을 저장하며, native 기본값은 빠른 테스트를 위한 memory backend입니다. 현재 Agent는 raw message history나 자유 문장 summary를 저장하지 않고 모든 playbook이 model 0회인 deterministic 경로입니다. ADR-0006 Stage B의 offline output/context contract, credential-free corpus gate와 reservation 정산 코어에 이어 첫 hosted candidate adapter와 opt-in 평가기까지 구현했지만 운영 graph node에는 연결하지 않았습니다. 따라서 recorded baseline의 정확도나 아직 실행하지 않은 live gate를 실제 model 품질·token 절감 성과로 주장하지 않습니다. 다음 작업은 보호된 환경의 3회 측정과 두 번째 후보 비교이며 실제 context reduction과 graph 연결은 그 뒤 Stage C에서 활성화합니다. 명시적 opt-in 장기 기억용 `AsyncPostgresStore`와 delegated OIDC 사용자 인증은 이후 단계입니다. Phase 4B-3C Worker replica gate 자동화는 구현됐지만 새 watcher 경로의 smoke 재검증은 대기 중이고 정식 4시간 release profile도 미실행·미승인입니다. 따라서 일반 제품 실행에서는 Agent API와 Worker replica를 각각 하나만 유지합니다.

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

### 다중 턴 clarification

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

### PostgreSQL을 포함한 통합 제품 실행

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

### Worker replica release gate

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
smoke 재검증은 대기 중이고 정식 4시간 release도 미실행·미승인이므로 이 문서의 기본 replica 수는
변경하지 않습니다.

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

### MCP OIDC 인증

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

## 범위에서 제외하는 것

- Agent 내부에서 별도의 이동 위험 점수나 시설 판정 규칙을 만들지 않습니다.
- 공공 API를 Agent가 직접 호출하지 않습니다.
- 원본 Tool 응답 전체를 대화 메시지나 장기 기억에 계속 누적하지 않습니다.
- 모델의 내부 추론 과정은 API나 trace에 노출하지 않습니다.
- 운영 경로에서 무제한 자율 Tool 호출을 허용하지 않습니다.
- 내부 run ledger를 public event route, SSE 또는 `/ops`로 직접 노출하지 않습니다.
