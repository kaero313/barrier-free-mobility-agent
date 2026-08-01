# Agent Console 제품 계약

## 1. 목적과 경계

Agent Console은 같은 Web Agent 제품 안에 있지만 사용자 Assistant의 상세 mode가 아닌 별도
권한·DTO를 가진 운영 surface다.

- Web route: `/ops`
- API namespace: `/internal/ops/v1`
- 인증: hosted OIDC
- 권한: 최소 `ops.viewer`, 진단 정보는 `ops.diagnostics`
- 데이터: redacted, versioned read model

사용자용 public DTO, 원본 `AgentState`와 운영 DTO를 공유하지 않는다. Console은 실제 실행 event와 저장된 집계만 보여주며 추정 timeline을 만들지 않는다.

### Public surface와의 경계

| Surface | 대상 | 인증 | 데이터 | 허용 범위 |
|---|---|---|---|---|
| Assistant 결과 | 현재 사용자 | local owner, hosted 사용자 인증 예정 | public response schema v3 | canonical 결과, context, presentation, 공개 evidence |
| `/status` | 일반 사용자 | 없음 | `PublicServiceStatus` | 서비스·기능 상태와 확인 시각 |
| Agent Console | 운영자 | hosted OIDC + RBAC | `/internal/ops/v1` | redacted event, Tool, budget, recovery와 집계 |

`/status`는 Ops Overview를 축약해서 전달하지 않고 raw readiness에서 별도의 allowlist mapping을
만든다. Assistant에 run processing event를 추가할 때도 internal timeline DTO를 재사용하지
않는다. 구체 경계는
[ADR-0008](adr/0008-product-trust-and-operations-surfaces.md)과
[공개 서비스 상태 계약](public-status.md)을 따른다.

## 2. 정보 구조

```text
Agent Console
├─ Overview
├─ Runs
│  └─ Run Detail
├─ MCP & Dependencies
├─ Evaluations
└─ Releases
```

`Threads`를 최상위 탐색 메뉴로 두지 않는다. 사용자의 대화 탐색 제품처럼 보이고 위치·이동 조건 노출 위험이 커지기 때문이다. Runs에서 hash 처리한 thread 연관성만 제한적으로 제공한다.

## 3. Overview

### Service health

- API readiness
- MCP contract status와 마지막 확인 시각
- Tool count와 schema fingerprint
- 배포 revision, flow version과 state schema version
- PostgreSQL readiness와, model이 필수 경로로 활성화된 단계부터 별도 model readiness

### 최근 집계

- 실행 상태: completed, interrupted, failed
- 업무 상태: SUCCESS, PARTIAL, NEEDS_CLARIFICATION, FAILED
- intent 분포
- p50/p95/p99 latency
- Tool 오류·timeout 비율
- clarification 비율과 cycle 초과율
- graph·Tool·model budget 초과율

집계에는 원문 질문과 정확한 위치를 포함하지 않는다.

## 4. Runs

검색 조건:

- 기간
- 실행 상태와 업무 상태
- intent
- error code
- Tool capability
- clarification·resume·recovery 여부
- flow/state/playbook/schema version
- hash 처리한 thread ID

run 목록 기본 column:

```text
run id | started at | duration | execution | business | intent | error | version
```

원문 질문은 기본 column과 검색 index에서 제외한다.

## 5. Run Detail

### Header

- run ID, request/trace ID
- hash 처리한 thread ID
- 실행 상태와 업무 결과를 분리한 badge
- 시작·종료 시각과 duration
- intent, answer source
- flow/state/playbook/tool-schema version

### Overview tab

- 사용자에게 공개된 최종 message
- risk, confidence, last checked
- available partial info
- failed sources, limitations와 unverified parts
- clarification 질문과 option
- terminal 또는 interrupt 이유

### Timeline tab

lane:

```text
API | Graph | Tool | Model | Checkpoint | Recovery
```

각 event는 ID, type, status, timestamp, duration, parent ID와 safe summary를 갖는다. Tool arguments, 원본 result와 reasoning은 timeline payload에 싣지 않는다.

### State & Policy tab

- redacted state diff
- intent source, confidence와 signal code
- selected playbook와 Tool allowlist
- mobility slot의 known/unknown과 provenance category
- pending clarification
- projection assumption 이름
- flow/state schema version

정확한 이동 조건 값은 `ops.diagnostics` 권한에서도 기본 masking하고 필요성이 입증된 field만 별도 reveal audit를 거친다.

### Budget tab

- turn graph steps / limit
- run graph steps / limit
- turn Tool calls / limit
- run Tool calls / limit
- model calls / limit (현재 playbook은 0회)
- model input/output/total token / run limit과 usage source
- context reduction 전후 token, reason code와 budget rejection
- clarification cycles / limit
- max parallelism

turn과 run 누적 값을 같은 numerator로 합치지 않는다. 질문·역명·mobility value, prompt와
compacted context 원문은 Budget tab payload나 metric label에 포함하지 않는다. token accounting과
reduction 계약은 [ADR-0006](adr/0006-bounded-model-context-and-token-accounting.md)을 따른다.

### Tools & Evidence tab

- capability, attempt와 status
- started/finished/duration
- contract/schema hash
- error classification과 retryable
- artifact reference와 추출된 evidence summary
- cache 또는 fallback 여부

Console은 raw Tool payload를 권한과 관계없이 제공하지 않는다. 사고 분석에서 원본 artifact가 반드시 필요하면 Console 밖의 별도 break-glass 절차, 짧은 승인 시간과 감사 기록을 사용한다.

### Recovery & Evaluation tab

- interrupt/resume
- retry/fallback
- checkpoint ID
- 향후 worker lease와 idempotency 상태
- safety invariant 결과
- 연결된 regression case와 evaluation 결과

## 6. 현재 데이터와 선행 작업

| 데이터 | 현재 상태 | Console 전 선행 작업 |
|---|---|---|
| run/thread ID | public 응답과 durable repository에 존재 | 실행 전 run ID 할당 |
| 실행·업무 상태 | public 응답에 존재 | 상태 transition history |
| 최종 message와 evidence | public 응답에 존재 | ops projection |
| readiness/fingerprint | health API에 존재 | contract history |
| trajectory | checkpoint state에만 존재 | event ledger와 timeline DTO |
| Tool ledger | 이름 목록만 state에 존재 | attempt·latency·result event |
| budget counters | state에 존재 | versioned ops budget view |
| state diff | 없음 | redacted snapshot/diff pipeline |
| timestamps/duration | run model에 없음 | clocked run event |
| token/cost | 실제 model 호출 없음 | ADR-0006 usage aggregate와 provider accounting |
| trace/span | 없음 | OTel propagation |
| worker recovery | 없음 | durable worker/lease |
| run list/aggregation | 개별 run row는 PostgreSQL에 저장, admin list query 없음 | bounded PostgreSQL query model |
| evaluation | 없음 | evaluation repository |

현재 `GraphTurn`이 전체 state를 가지고 `ConversationRun`은 공개 outcome, context와 `RunUsage`
snapshot을 저장한다. node·Tool event와 호출별 model usage는 저장하지 않으므로 완료 뒤 전체
운영 timeline을 안정적으로 조회할 수 없다. checkpoint나 in-memory object 접근을 Console
API로 포장하지 않는다.

## 7. run identity 선결 조건

현재 public run ID는 graph 실행 성공 뒤 생성된다. 실행 중 log·Tool call과 public run을 처음부터 연결하려면 lifecycle을 다음처럼 바꾼다.

1. 요청 수락 시 run ID를 먼저 할당한다.
2. request ID, run ID와 `thread_id_hash`를 RuntimeContext에 전달한다.
3. `run.started` event를 저장한 뒤 graph를 실행한다.
4. node·Tool·checkpoint event를 같은 run ID로 기록한다.
5. final state transition과 public response를 저장한다.
6. process crash 뒤 lease/recovery가 같은 run을 재개한다.

이 변경 전 Console에는 readiness와 synthetic fixture만 연결한다.

## 8. 내부 read model

권장 DTO:

```text
OpsOverview
OpsRunListItem
OpsRunDetail
OpsTimelineEvent
OpsBudgetUsage
OpsToolExecution
OpsStateDiff
OpsContractSnapshot
OpsEvaluationSummary
```

권장 endpoint:

```text
GET /internal/ops/v1/overview
GET /internal/ops/v1/runs
GET /internal/ops/v1/runs/{run_id}
GET /internal/ops/v1/runs/{run_id}/timeline
GET /internal/ops/v1/runs/{run_id}/state-diffs
GET /internal/ops/v1/contracts/mobility-mcp
GET /internal/ops/v1/evaluations
GET /internal/ops/v1/releases
```

list query는 cursor pagination과 bounded time range를 요구한다. endpoint는 public OpenAPI와 분리된 internal schema artifact로 배포한다.

## 9. 보안과 개인정보

- 사용자 질문, 역과 이동 조건은 기본 masking한다.
- thread ID는 irreversible keyed hash로 표시한다.
- 운영 조회와 reveal action을 audit log에 남긴다.
- raw prompt, chain-of-thought와 secret은 저장·제공하지 않는다.
- production Console을 외부에 공개하지 않는다.
- 외부에서 확인 가능한 화면이 필요하면 synthetic fixture와 별도 demo environment만 사용한다.
- CSV export는 초기 범위에서 제외한다.
- unauthenticated request는 sign-in 경계에서 거절하고, 인증됐지만 scope가 없으면 403으로
  거절한다. session 만료를 데이터 없음이나 run 404로 표현하지 않는다.
- public sitemap, header navigation과 검색 index에 Console route를 포함하지 않는다.
- route 비노출은 접근 통제가 아니며 BFF와 Agent internal API가 각각 authorization을 검증한다.

## 10. 구현 순서

1. run ID 사전 할당과 correlation context
2. PostgreSQL run/event/Tool ledger
3. redaction과 versioned ops projection
4. internal API OIDC/RBAC
5. Overview와 Runs
6. Run Detail timeline/state/budget
7. contract history, releases와 evaluations
8. unauthenticated, forbidden, session-expired와 audit E2E 검증

## 11. 완료 조건

- 모든 Console 값은 저장된 event 또는 projection에서 유래한다.
- 실행 상태와 업무 결과를 서로 바꾸어 표시하지 않는다.
- turn과 run 누적 budget을 구분한다.
- public DTO와 internal DTO가 의존성 수준에서 분리된다.
- 기본 권한에서 질문·위치·이동 조건 원문을 볼 수 없다.
- run ID로 API, graph, Tool, checkpoint와 trace를 연결할 수 있다.
- Console 장애가 Agent 실행 경로에 영향을 주지 않는다.
