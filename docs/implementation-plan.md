# 구현 계획

## 현재 진행 상태

2026-07-16 기준으로 Phase 0 기반 구성, Phase 1 MCP 수직 기능, Phase 2A bounded trip workflow, Phase 2B 공개 presentation contract, Phase 2C capability-gated facility workflow, Phase 3A 통합 Web vertical slice, Phase 3B의 durable conversation·객체 소유권 기반선과 Phase 3C-0 public projection hardening, Phase 3C-1 상태 적응형 Assistant, Phase 3C-2 public product shell, Phase 3C-3 structured clarification, Phase 4A durable run ledger, Phase 4B-1 repository coordination substrate, Phase 4B-2 single-worker durable execution, Phase 4B-3A worker readiness·admission safety, Phase 4B-3B core multi-replica chaos safety와 Phase 4B-3C Worker replica release-gate 자동화가 구현된 상태입니다. Phase 4B는 ADR-0011에 따라 repository coordination substrate, worker 활성화, readiness/admission safety, core chaos correctness와 soak·replica release를 분리한 단계형 rollout입니다. 3C 자동화는 Agent 1개·Worker 2개 범위만 판정하며, long-lived Worker exact-registration watcher와 Agent queue stream을 적용한 smoke 재검증은 대기 중이고 정식 4시간 release profile도 미실행·미승인이므로 기본 Worker replica는 계속 1개입니다. ADR-0006 Stage A 구현 완료 상태이며, Stage B에서는 Tool 선택을 허용하지 않는 strict model output, ephemeral context/invocation assembler, checkpoint-safe reservation 정산 코어, 128-case credential-free offline gate와 첫 OpenAI Responses candidate adapter·보호형 live evaluator까지 구현했습니다. production graph는 structured clarification topology를 포함한 `mobility-v2`, state schema 3과 model 0회 경로를 사용하며 hosted adapter 선택·활성화는 아직 하지 않았습니다. 같은 저장소의 `web/` UI·BFF와 Python Agent runtime은 하나의 웹 기반 Agent 제품으로 검증됩니다. 실제 MCP mock 서버와 Agent Uvicorn을 별도 프로세스로 실행하는 contract/E2E smoke를 로컬과 Linux CI에서 동일한 실행기로 검증하며, OIDC client-credentials의 token 갱신 경계도 포함합니다. 현재 CI-pinned MCP SHA에는 facility extension이 없으므로 trip capability만 활성화됩니다. Phase 3C-3은 ADR-0009의 v3/v4 호환 계약, typed graph resume, Web native form과 PostgreSQL restart 복구까지 통합 검증했습니다. hosted model 후보 비교·선택은 별도 runtime track으로 유지하며 UI 개편의 선결 조건으로 만들지 않습니다. 실제 graph model 활성화와 context reduction은 각각 Stage B runtime slice와 Stage C에서 수행합니다. opt-in 장기 기억은 후속 단계이며, Phase 2 전체 완료에 필요한 alternative subgraph도 남아 있습니다. Phase 4A는 ADR-0010의 reservation·event sequence·Tool transition·owner·redaction 계약과 crash orphan 탐지를 memory, PostgreSQL, graph orchestration, 재시작 제품 경로에서 검증했습니다. Phase 4B-1은 memory와 실제 PostgreSQL에서 concurrent reservation·claim, lease 만료·재획득, stale token fencing, private payload scrub과 schema v6 migration을 검증했고 Phase 4B-2는 BFF/API/worker, AES-GCM schema v7, lease-bound finalization과 conservative recovery gate를 추가했습니다. Phase 4B-3A는 schema v9 Worker registry, idle heartbeat, 신규 reservation rollback gate와 low-cardinality queue health를 추가했습니다. Phase 4B-3B는 exact registration claim, lease-fenced checkpoint와 execution provenance, forced-stop lease revocation, deterministic fault seam을 추가하고 실제 PostgreSQL 경쟁·stale write·checkpoint lock과 subprocess hard-kill recovery를 검증했습니다. 3C release profile은 image build를 필수 provenance로 포함하며, container image build는 CI에서도 독립 release gate로 유지합니다.

## 진행 원칙

- 각 단계는 실행 가능한 vertical slice로 끝낸다.
- 실제 Barrier-Free Mobility MCP와의 통신은 Python import가 아니라 Streamable HTTP contract test로 검증한다.
- 이동 위험 판단 로직은 Agent에 복제하지 않는다.
- 새 기능은 정상 경로뿐 아니라 timeout, 부분 실패, schema drift와 재개 테스트를 함께 추가한다.
- 운영 의존성은 필요해지는 단계에서 도입한다. PostgreSQL은 checkpoint 단계부터, Redis는 분산 rate limit이나 event fan-out이 실제로 필요할 때 추가한다.

## Phase 0. 프로젝트 기반

### 산출물

- Git 저장소와 기본 브랜치 정책
- `.python-version`의 Python 3.13 고정
- `pyproject.toml`, `uv.lock`, `src` layout
- `.gitignore`, `.dockerignore`, `.env.example`
- Ruff format/lint, mypy strict, pytest strict 설정
- FastAPI application factory와 config validation
- Dockerfile과 local Compose의 최소 골격
- CI의 lock check, lint, type check, unit test, image build

### 완료 조건

- `uv sync --locked`가 Windows와 Linux CI에서 성공한다.
- `uv run ruff check .`, `uv run mypy src`, `uv run python -m pytest`가 통과한다.
- 빈 API가 `/health/live`를 제공하고 non-root image로 실행된다.
- secret이 없는 `.env.example`만 version control에 포함된다.

## Phase 1. MCP contract vertical slice

### 산출물

- `MultiServerMCPClient` 기반 `McpGateway`
- HTTP transport, static Bearer/OIDC auth adapter
- startup Tool discovery, 필수 Tool 검증과 schema fingerprint
- MCP structured artifact → Agent DTO mapper
- transport/tool/business/contract 오류 분류
- `answer_accessibility_question` 단일 playbook
- 요청 제출과 최종 응답 API
- 실제 MCP mock mode contract test
- Linux CI의 별도 MCP checkout 기반 contract/E2E gate

### 완료 조건

- Agent가 실행 중인 MCP의 8개 Tool 목록과 schema fingerprint를 읽고, 사용하는 Tool의 입력·enum·중첩 출력 계약을 readiness에 반영한다.
- 대표 한국어 질문이 MCP canonical `user_message`를 변경 없이 반환한다.
- response `PARTIAL`·`NEEDS_CLARIFICATION`, 중첩 `risk_level=UNKNOWN`, malformed response를 각각 다른 결과로 처리한다.
- `AccessibilityQuestionResult.result`와 `facility_result`를 intent에 따라 구분 검증한다.
- 세부 Tool 빈 배열을 “시설 없음”으로 단정하지 않는 테스트가 있다.
- MCP 제한보다 긴 자연어 입력을 조용히 자르지 않고 구조화 경로 또는 clarification으로 처리한다.
- MCP health의 HTTP status뿐 아니라 degraded payload와 Tool schema fingerprint를 readiness에 반영한다.
- Agent test가 별도 MCP 프로젝트의 소스 모듈을 import하지 않는다.

`facility_result`는 MCP의 후방 호환 가능한 capability extension으로 취급한다. 해당 schema가 있으면 facility 중첩 계약을 startup에 엄격히 검증하고, 아직 확장을 제공하지 않는 고정 MCP 버전에서는 시설 질문의 명확화 응답을 허용한다. CI는 이동하는 브랜치 대신 검증된 MCP commit SHA를 사용하며 upgrade PR에서만 pin을 갱신한다.

## Phase 2. 전용 LangGraph와 다중 턴 문맥

### 현재 구현된 Phase 2A

- JSON 값만 checkpoint하는 `AgentState`와 비저장 `RuntimeContext`
- 출처를 포함한 `true | false | unknown` mobility context와 명시적 MCP profile projection
- deterministic trip pre-router와 enum 기반 `IntentDecision`, `ExecutionPlan`
- 모델에 Tool schema를 노출하지 않는 versioned allowlist와 실행 budget
- MCP clarification turn 사이에도 유지되는 run 누적 step·Tool trajectory와 상한
- `resolve_station`과 `generate_accessibility_brief` typed gateway 및 startup semantic gate
- top-level graph와 bounded trip subgraph
- 역 후보 선택, 이동 조건 질문의 `interrupt()`/`Command(resume=...)`
- MCP의 정상 `NEEDS_CLARIFICATION`을 같은 run의 제한된 후속 turn으로 재개
- 호출 전 Tool 예약 checkpoint, resume 중 실패한 기존 run의 종결과 재시도 차단
- raw MCP 후보 대신 `value`/`label`만 제공하는 공개 clarification option
- canonical `user_message`, `PARTIAL`, `risk_level=UNKNOWN` projection
- memory/PostgreSQL 선택형 thread/run 생성·조회·재개·삭제 API
- 이전 목적지를 사용하는 “거기서 시청까지는?” 후속 질문

Phase 2 checkpoint에는 원본 MCP payload, 전체 메시지 history, gateway/model 객체를 넣지 않습니다. 휠체어 사용 여부와 계단·에스컬레이터 이용 가능 여부는 독립 슬롯이며, 후자의 미확인 값을 휠체어 언급만으로 채우지 않습니다. facility workflow도 provider 원본 대신 safe context와 presentation만 남깁니다. Phase 3B PostgreSQL 경로는 저장 완료된 run과 clarification의 재시작 내구성을 제공하며, 실행 중 crash window와 worker lease는 Phase 4 범위입니다.

### Web 연결 선결 조건

- 첫 Web release는 trip accessibility를 기본 범위로 표시하고 facility는 `readiness.status=ready`, `mobility_mcp.status=ready`, `facility_question` capability를 모두 만족하는 배포에서만 새 진입점을 확장한다.
- facility capability gate는 새 질문 진입점에만 적용하며 이미 받은 facility presentation은 readiness 변화로 숨기지 않는다. alternative subgraph 전에는 통합 Assistant가 모든 자연어 질문을 지원한다고 표현하지 않는다.
- MCP 원본이나 `AgentState`를 공개하지 않는 `ConversationContextView`, `ClarificationView`, discriminated `AnswerPresentation`을 response schema v3로 구현했다.
- 한 active clarification turn, `choice | free_text`, `single_text` answer mode와 stale `clarification_id` 차단을 구현했다. 평면 `questions[]`와 `clarification_options[]`는 migration 기간에만 deprecated field로 유지한다.
- structured multi-answer와 tri-state control은 Phase 3C-3 ADR-0009의 versioned request schema에서만 제공한다.
- 동일 저장소의 `web/` Next.js 계층은 same-origin BFF를 통해 Python Agent API를 호출하고 browser direct CORS를 기본 경로로 사용하지 않는다.
- 통합 Compose의 local/demo Web은 signed anonymous session과 owner-aware PostgreSQL mapping으로 object-level authorization을 적용한다. native 기본값은 단일 local owner와 memory backend를 사용할 수 있다. hosted Web 전에는 delegated OIDC token 검증과 익명 owner를 인증 subject로 승격하는 절차를 추가한다.
- 현재 thread 조회·history·active run API가 없으므로 refresh 복구는 해당 계약 이후 활성화한다.

상세 화면 계약은 [제품 UI/UX 설계](product-design.md), [사용자 흐름](user-flows.md), [wireframe](wireframes.md)과 [ADR-0003](adr/0003-web-frontend-boundary.md)을 따른다.

### 현재 구현된 Phase 2B

- `trip-v2`, state schema 2와 answer/run response schema 2
- graph turn 시점에 생성해 run별로 고정하는 `ConversationContextView`
- unknown·보수적 파생값을 confirmed mobility condition에서 제외하는 provenance projection
- one active form과 한 자연어 답을 정확히 표현하는 `ClarificationView`
- optional wire field이지만 Web에서 필수로 사용하는 `clarification_id`와 `409 STALE_CLARIFICATION`
- route summary, safety notice, confidence reasons와 allowlisted evidence만 담는 `AnswerPresentation`
- legacy clarification field deprecation과 run-status/business-status 조합 검증
- 과거 run GET이 후속 thread state로 변하지 않는 snapshot 회귀 테스트

### 현재 구현된 Phase 2C

- Phase 2C 도입 당시 `mobility-v1`, state schema 3와 answer/run response schema 3
- MCP `facility_result`와 parsed facility field의 nested enum·nullable·child schema deep gate
- readiness의 `trip_accessibility | facility_question` capability 공개
- capability가 없으면 Tool 호출 없이 명시적 business `FAILED`로 끝나는 보수적 분기
- `answer_accessibility_question`을 의미 제한한 `answer_facility_question` gateway wrapper
- 엘리베이터·장애인 화장실 status/location/existence/overview bounded facility graph
- missing station/type clarification의 same-run resume과 이전 facility target 후속 문맥
- facility run에서 retained trip context를 숨기는 intent-scoped `ConversationContextView`
- trip/facility discriminated presentation union과 facility detail cap·privacy projection
- 조회 완결성, 유형별 answer state와 개별 facility status의 교차 필드 validator

일반 배포에서 facility를 활성화하려면 별도 MCP 프로젝트의 현재 facility 변경을 먼저 독립 커밋·검증하고 Agent CI pin을 해당 SHA로 올려야 한다. Agent 코드는 schema presence만으로 신뢰하지 않고 deep gate가 통과한 경우에만 capability를 기록한다.

### 산출물

- `AgentState`, `RuntimeContext`, `IntentDecision`, `ExecutionPlan`
- `true | false | unknown`을 보존하는 Agent 전용 mobility context와 명시적 MCP DTO mapper
- top-level Graph API와 trip/facility/alternative subgraph
- 코드 pre-router + structured LLM fallback
- 동일 fixture 기반 모델 후보 평가와 기본 provider/model 결정 기록
- intent별 Tool allowlist와 실행 budget
- `generate_accessibility_brief` 기반 구조화 이동 판단
- `resolve_station` 기반 모호성 처리
- `interrupt()`/resume 기반 clarification
- 이전 출발지·목적지·profile·선택 경로를 사용하는 후속 질문

### 완료 조건

- 동일 질문은 고정된 playbook과 제한 안에서 실행된다.
- 모델이 임의 Tool 이름이나 무제한 Tool loop를 만들 수 없다.
- “거기서 시청까지는?”, “환승역 화장실은?” 같은 다중 턴 fixture가 통과한다.
- “현재 경로 말고 다른 경로”는 이전 역·이동 조건으로 `compare_alternatives=true` 경로 판단을 다시 받아, MCP가 검증한 alternatives만 답하고 없을 때 비교할 경로가 없다고 설명한다.
- 미확인 계단·에스컬레이터 조건이 MCP 기본값으로 변환되지 않고 clarification으로 가는 테스트가 있다.
- 최대 graph step, Tool/model call, parallelism 제한이 테스트로 강제된다.
- 선택 모델의 structured output 준수율, 한국어 routing 정확도, p95 latency와 비용 기준이 기록된다.
- 명확화 전후 state가 checkpoint 재개와 호환되는 JSON 값으로만 구성된다.

## Phase 3. 웹 제품 통합과 운영 persistence

### Phase 3A. 동일 저장소 Web UI와 BFF vertical slice

상태: 구현 완료 (2026-07-11)

#### 산출물

- root `web/`의 TypeScript strict·Next.js App Router scaffold와 Web lockfile
- `/assistant`, 향후 `/ops` route group과 same-origin `/api/agent` BFF
- OpenAPI schema 3 generated client와 contract validation adapter
- `ConversationContextView`, `ClarificationView`, trip/facility `AnswerPresentation` 컴포넌트
- readiness 3중 조건을 사용하는 capability gate와 trip-only 기본 surface
- start/resume의 indeterminate loading, stale clarification, transport-unknown과 terminal failure 상태
- Python 품질 gate와 함께 실행되는 Web type/lint/unit/accessibility 및 full-stack E2E gate

#### 완료 조건

- Web source와 Python Agent source가 한 저장소·한 제품 변경으로 검증되고 MCP만 별도 프로젝트로 유지된다.
- browser는 Agent port를 직접 호출하지 않고 BFF를 통해 schema 3 계약만 소비한다.
- active clarification 중 일반 composer가 잠기고 현재 `clarification_id` 없는 resume을 Web adapter가 만들지 않는다.
- facility 진입점은 readiness 전체와 dependency가 ready이고 capability가 있을 때만 보이며, 기존 facility 결과는 capability 변화와 무관하게 렌더링된다.
- `PARTIAL`, business `FAILED`, technical failed run, presentation 유·무와 empty/null 상태를 서로 다른 UI로 검증한다.

### Phase 3B. PostgreSQL persistence와 bounded context

상태: durable 기반선·ADR-0006 Stage A 및 Stage B offline/reservation 기반선 구현 완료 (2026-07-13)

#### 현재 구현

- local `InMemorySaver`, 제품 Compose의 `AsyncPostgresSaver`
- owner-aware `agent_threads`·`agent_runs` repository, run payload schema version과 advisory-lock migration
- process 재시작 뒤 동일 run 조회·clarification resume 통합 테스트
- public thread ID와 분리한 owner-scoped checkpoint key
- exact repository/checkpointer migration·invalid index 검증, strict non-pickle serializer, 직렬화 checkpoint payload의 AES 보호와 key fingerprint fail-fast
- hosted DB의 verified TLS·at-rest encryption 배포 전제 fail-fast
- HttpOnly HMAC anonymous session, canonical hosted Origin 검증, BFF shared-token과 stateful API 5종의 object-level 404 격리
- trusted-header mode의 one-shot 질문 인증과 hosted internal HTTP의 명시적 private-network opt-in
- persistence dependency를 포함한 readiness와 PostgreSQL 18 Compose/CI service
- owner별·전체 thread capacity의 원자적 저장소 제한
- psycopg binary cursor의 empty checkpoint namespace를 text OID로 고정하는 회귀 guard
- Phase 4B-3C 정식 release profile 승인 전 Agent API·worker 각각 replica 1개 제한
- current input과 typed trip/mobility/facility slot만 유지하고 raw message history·summary를 저장하지
  않는 bounded state
- deterministic pre-router와 모든 current playbook의 model 호출 0회
- strict model-only intent DTO와 value-minimized ephemeral context/invocation assembler
- domain-separated HMAC fingerprint의 open reservation과 deterministic terminal usage settlement
- synthetic 한국어 128-case corpus, normalized-only replay와 aggregate offline evaluation report

#### ADR-0006 구현 단계

1. **Stage A — state/accounting 기반선 (완료, 2026-07-12)**
   - 외부 MCP/public DTO의 문자열·배열 상한, merged top-level/nested state와 interrupt payload의
     65,536-byte invariant
   - LangGraph `source=input` checkpoint 전 latest state + start/business-clarification input preflight
   - canonical messages·response schema·provider option과 hard output을 묶는 provider-neutral
     `ModelInvocation`, `StructuredModel`, `TokenCounter`, `ModelUsage`, `RunUsage` port와 fake
   - fresh ambiguous turn만 허용하는 versioned `ModelPurposePolicy`
   - model 없는 현재 경로의 zero usage, turn/run 누적 counter와 privacy-safe telemetry sink
2. **Stage B — structured model boundary (offline/reservation 기반선 완료, 2026-07-13)**
   - 완료: Tool·capability·missing field를 받을 수 없는 strict `StructuredIntentDecision`과
     deterministic projector
   - 완료: Tool 이름을 model-visible prompt에서 제외한 ephemeral `ModelContextEnvelope`,
     confidence fail-closed projector, canonical invocation assembler와 128-case offline gate
   - 완료: versioned keyed fingerprint를 가진
     `open → settled | failed_settled | unknown_charged | rejected` reservation 정산 코어
   - 완료: OpenAI Responses 첫 후보의 immutable contract, count/invoke 공통 wire, SDK retry 0,
     strict output·usage failure adapter와 protected live evaluation gate
   - 남음: 보호된 환경에서 첫 후보를 3회 측정하고 같은 fixture의 두 번째 후보와
     tokenizer undercount·hard-output mapping·latency·비용을 비교해 기본 adapter 선택
   - 남음: fresh ambiguous route에만 graph reservation/model node를 연결
   - Phase 4 전 orphan open reservation thread의 새 run 거부와 새 thread 복구 정책
   - model-only DTO는 intent/confidence만 반환하고 Tool playbook은 계속 `ToolPolicy`가 소유
3. **Stage C — context reduction 검증**
   - 4,096-token application input cap, soft 70%, target 50%, output/safety reserve
   - latest input, active clarification, confirmed mobility provenance와 active target deep-equality
   - required context가 hard limit을 넘으면 provider 호출 없이 safe clarification/budget error
4. **Stage D — optional turn digest**
   - typed slot만으로 실패한 cross-turn eval 사례가 있을 때만 최대 6개 structured delta를 도입
   - raw transcript와 자유 문장 authoritative summary는 기본 저장하지 않음

#### 남은 산출물

- Stage B hosted model 후보 live 비교·선택과 fresh ambiguous routing graph 경계
- model 호출 직전 deterministic context projection/reduction과 token accounting
- checkpoint history TTL/pruning 정책은 token reduction과 분리한 retention 작업
- 명시적 opt-in 장기 기억용 `AsyncPostgresStore`와 consent lifecycle
- Tool artifact 저장소와 compact `EvidenceView`
- thread history 조회, memory 조회·수정·동의 철회·삭제 API와 전체 저장 계층 삭제 전파
- encryption key rotation과 checkpoint/state data migration 절차

#### 완료 조건

- 완료: process 재시작 뒤 같은 thread/run의 clarification을 재개한다.
- 완료: 서로 다른 anonymous owner namespace에서 run/checkpoint가 섞이지 않는다.
- 명확한 trip/facility 질문은 model/context reduction 0회를 유지한다.
- active clarification과 nested interrupt resume은 model fallback으로 우회하지 않는다.
- 모든 model 호출은 사전 admission을 거치고 hard/run cap 거부 시 provider 호출이 0회다.
- 장기 기억을 끈 기본 설정에서 이동 조건이 thread 밖으로 유출되지 않는다.
- 긴 대화에서도 model input budget을 지키며 latest input, active clarification과 구조화
  mobility/trip/facility slot은 손실되지 않는다.
- provider usage가 없거나 일부만 있어도 conservative charge와 usage source가 결정적으로 남는다.
- model reservation은 모든 terminal 경로에서 정산·제거되고 restart version/fingerprint drift는
  provider 호출 전에 거부된다.
- graph input merge가 byte limit을 넘으면 `ainvoke`와 input checkpoint가 모두 실행되지 않는다.
- Phase 4 전 orphan open reservation은 자동 replay하지 않고 같은 thread의 새 run을 차단한다.
- 원본 Tool payload가 메시지 history와 trace에 중복 저장되지 않는다.
- token reduction 성과와 PostgreSQL checkpoint history 증가를 같은 지표로 보고하지 않는다.

### Phase 3C. 제품 중심 정보 구조와 신뢰 surface

상태: Phase 3C-0·3C-1·3C-2·3C-3 구현 및 통합 검증 완료 (2026-07-14)

이 단계는 Phase 3A의 기능 계약을 유지하면서 고정 3열 Assistant를 사용자 작업 중심의 상태
적응형 layout으로 바꾼다. public answer, public status와 protected ops projection을 분리하며
구현되지 않은 실행 timeline이나 운영 지표를 만들지 않는다.

기준 문서:

- [제품 정보 구조](information-architecture.md)
- [제품 UI/UX 설계](product-design.md)
- [공개 서비스 상태 계약](public-status.md)
- [ADR-0008](adr/0008-product-trust-and-operations-surfaces.md)
- [ADR-0009](adr/0009-structured-clarification-contract.md)

#### Phase 3C-0. Public projection hardening

상태: 구현 완료 (2026-07-13)

hosted public surface를 늘리기 전에 현재 응답 경계의 내부 정보 노출 가능성을 제거한다.

- `failed_sources.source_name`을 Agent-owned stable public code·표시명으로 projection
- error code별 allowlist detail schema를 만들고 임의 `AgentError.context` 공개 금지
- Tool·connector·budget·내부 reason이 public response에 포함되지 않는 contract test
- 예상하지 못한 exception의 client response와 structured log redaction 경계 검증
- schema v3 호환성·migration 영향과 MCP canonical message 보존 확인

이 단계는 UI에서 field를 숨기는 변경으로 대체하지 않는다. API response 자체와 OpenAPI가
공개 가능한 형태여야 한다.

구현 결과:

- conversation response schema v3는 유지하고 오류 envelope만 `ErrorResponse` v2로 올렸다.
- MCP 내부 source name과 reason은 Agent 경계에서 stable public code·표시명으로 투영한다.
- 등록된 Agent 오류는 빈 detail, request validation만 bounded `issues`를 사용하며 미등록 오류는
  일반 `INTERNAL_SERVER_ERROR`로 닫는다.
- Web BFF는 오류 원문 bytes를 재전송하지 않고 strict 계약으로 검증한 객체만 다시 직렬화한다.
- OpenAPI export 단계가 arbitrary detail, 열린 object와 내부 failed source 계약의 재도입을
  의미 검사로 차단한다.

#### Phase 3C-1. 상태 적응형 Assistant

상태: 구현 완료 (2026-07-13)

공개 API schema v3와 thread/run 의미를 변경하지 않는 첫 구현 단위다.

- 질문 전 최대 880px 중심 layout, 질문·지원 범위와 실제 가능한 예시만 표시
- 정상 readiness sidebar와 빈 `판단 대기 중` 결과 panel 제거
- clarification 중 server context, 짧은 transcript와 active form을 하나의 작업 흐름으로 구성
- 결과 후 canonical result 8-column과 공개 근거 4-column으로 확장
- 결과, 공개 근거, 후속 composer의 DOM·focus·mobile reading order 고정
- task copy에서 MCP·Tool·LangGraph 같은 내부 용어 제거
- 현재 상태 조합과 transport-unknown 복구 의미 유지
- 기존 design token, semantic HTML과 primitive adapter 경계 유지

구현 결과:

- 질문 전 workspace를 최대 880px 중심 흐름으로 바꾸고 정상 readiness와 빈 결과 panel을 제거했다.
- clarification은 server context, 짧은 transcript, canonical 질문과 하나의 active form 순서로 고정했다.
- 결과는 단일 DOM에서 canonical 본문 8-column, 공개 근거 4-column, 후속 composer 순서로 구성했다.
- 상태별 폭과 8:4 비율을 문서·Web 공통 design token 계약으로 승격했다.
- IME, focus 이동, 320px reflow, forced-colors, reduced-motion과 axe 회귀 검증을 추가했다.

완료 조건:

- schema v3 contract test와 기존 start·resume·delete E2E가 그대로 통과한다.
- 질문 전 thread 생성, 빈 결과 panel과 정상 readiness card가 모두 0개다.
- `SUCCESS`, `PARTIAL`, business `FAILED`, technical failed와 presentation null 분기가 유지된다.
- active clarification 중 일반 composer로 새 run을 제출할 수 없다.
- desktop·mobile에서 결과와 근거가 DOM에 한 번만 존재하고 결과가 후속 composer보다 먼저 읽힌다.
- 320px, 400% 확대, keyboard, 한국어 IME, axe, forced-colors와 reduced-motion을 통과한다.

#### Phase 3C-2. Public product shell

상태: 구현 완료 (2026-07-14)

- `/` 지원 범위·안전 원칙과 Assistant 진입
- public header의 `이동 조회 | 작동 방식 | 서비스 상태`
- `/how-it-works`의 일반 처리 방식, 불확실성·한계 설명
- `/status`와 `/api/agent/public-status`의 BFF-owned allowlist DTO
- `/accessibility`의 사용법, 지원 기준과 피드백 경로
- `/ops`를 public navigation, sitemap과 검색 surface에서 제외

완료 조건:

- public status가 raw readiness의 fingerprint, Tool count, backend, 내부 error와 URL을 노출하지 않는다.
- 성공·503·timeout·schema drift가 `PublicServiceStatus`로 안전하게 mapping된다.
- public page는 `/internal/ops/v1`을 import하거나 호출하지 않는다.
- root 진입과 status 조회가 thread 또는 run을 만들지 않는다.
- capability가 없는 기능을 현재 사용할 수 있는 것처럼 설명하지 않는다.

구현 결과:

- public header·footer와 `/`, `/how-it-works`, `/status`, `/accessibility`를 하나의 제품 정보 구조로 연결했다.
- 전용 BFF projection이 raw readiness를 strict `PublicServiceStatus`로 축약하고 5초 timeout, 64 KiB 상한과 HTTP/body 일치 검증을 적용한다.
- Assistant도 같은 공개 상태 계약을 사용하며 Browser에서 raw `/api/agent/readiness` route를 제거했다.
- public status는 자동 polling·재시도·마지막 성공 fallback 없이 진입 GET 1회와 사용자 명시 refresh만 수행한다.
- sitemap과 public navigation에서 `/ops`를 제외하고 root·status 진입이 thread/run을 만들지 않는 E2E를 추가했다.

#### Phase 3C-3. Structured clarification 계약

상태: ADR-0009 계약 승인, runtime·BFF·Web 구현 및 통합 검증 완료 (2026-07-14)

기존 resume request는 하나의 `answer: string`만 받아 여러 deterministic field를 하나의 textarea에
모았다. trip workflow가 출발지·도착지와 계단·에스컬레이터 조건을 함께 요청하는 실제 사례가
확인되었으므로 Web이 prompt를 파싱하지 않는 다음 계약을 승인했다.

- response schema v4의 nested `ClarificationView` v2와
  `answer_request=single_text | fields`
- `inputs[]`의 `id`, `prompt_id`, stable `field`, `text | tri_state | single_choice` control,
  서버 제공 label·option·constraints
- resume `values[]`의 `{input_id, value: {kind: text | choice | tri_state, value}}`; field와 control은
  active contract에 있으므로 answer item에 반복하지 않음
- v2 tagged resume body와 legacy `{answer: string, clarification_id?}` body의 호환 기간 병행
- 출발지·도착지, 계단·에스컬레이터, 역 후보만 structured fields로 projection하고 MCP 후속 질문과
  미승인 조합은 clarification 전체를 `single_text`로 fallback
- field answer를 atomic하게 검증하고 누락·중복·추가·kind/control·option 오류는 422로 거절
- lock 안에서 stale ID를 answer validation보다 먼저 409로 거절하고 draft를 자동 replay하지 않음
- 안전 필수 `tri_state=unknown`은 MCP 호출 없이 업무 `FAILED`로 종료
- application에서 active contract를 검증한 structured 값을 LangGraph `Command`로 전달하고 graph가
  자연어 parser 없이 기존 trip/mobility slot에 원자 적용
- station `option-N`을 checkpoint candidate order에 직접 매핑하고 `unknown`은 정상 완료되는 graph
  terminal branch에서 업무 `FAILED`로 기록
- state schema 3을 유지하되 topology 변경을 반영해 flow를 `mobility-v2`로 전환
- 기본 response v3를 한 호환 기간 유지하고 BFF가 `X-Agent-Response-Schema: 4`로 v4를 선택

구현 범위는 Python public projection·resume validator와 typed graph resume, BFF schema 선택, OpenAPI v4 client,
Web structured form과 field error 연결이다. multi-select, optional·conditional input, 부분 server draft,
state schema 4, POST idempotency와 MCP 동적 form schema는 제외한다.

완료 조건:

- 복수 field가 하나의 atomic resume으로 완료되고 graph resume과 MCP 호출이 중복되지 않는다.
- 422와 stale 409 뒤 active clarification과 checkpoint가 바뀌지 않는다.
- station option은 현재 clarification에 묶인 opaque token이며 raw MCP station ID를 노출하지 않는다.
- `unknown` 안전 조건은 외부 Tool을 호출하지 않고 명시적인 판단 불가 결과로 종료한다.
- structured field가 자연어 parser를 거치지 않고 한 graph update로 slot에 적용된다.
- `mobility-v2`, state schema 3과 기존 v1 thread mismatch 경계가 restart test에서 검증된다.
- default v3와 BFF-selected v4가 같은 호환 기간에 contract, restart와 E2E 검증을 통과한다.
- structured form은 default selection 없이 keyboard, 한국어 IME, 320px reflow와 axe를 통과한다.

기준 결정은 [ADR-0009](adr/0009-structured-clarification-contract.md)다.

#### 후속 경계

- public processing activity: Phase 4 run/event ledger와 owner-scoped allowlist projection 이후
- Agent Console: Phase 5 OIDC·RBAC·redaction·audit 이후
- 지도: 구조화된 route segment·geometry 계약 이후
- 실행 비용·token 절감 표현: 실제 provider usage와 비교 측정 이후

## Phase 4. 내구 실행과 장애 복구

### Phase 4A. Durable run ledger 기반선

상태: 구현·통합 검증 완료 (2026-07-14)

기준 결정은 [ADR-0010](adr/0010-durable-run-ledger-and-crash-semantics.md)이며 장애 분류와 수동
대응은 [durable run ledger runbook](runbooks/durable-run-ledger.md)을 따른다.

#### 계약과 현재 구현 범위

- public `ConversationRun` snapshot과 내부 `running` execution을 분리한다.
- graph 전에 run ID, thread active pointer와 sequence 1의 `run.started`를 같은 transaction으로
  예약한다.
- `run_events`가 run과 Tool transition의 전체 순서 source of truth다.
- `tool_executions`는 started/terminal event sequence를 참조하는 immutable terminal summary이며
  시작 row를 terminal 상태로 UPDATE하지 않는다.
- start와 resume invocation별 request/run/thread correlation을 RuntimeContext와 Tool recorder까지
  전달한다.
- checkpoint와 ledger는 별도 transaction이므로 graph 입력 전, checkpoint 전후와 Tool 호출 전후
  crash window를 허용하고 내부에서 탐지한다.
- 현재 read-only MCP 호출은 at-least-once이며 Phase 4A에서 자동 replay·repair하지 않는다.
- event/Tool DTO는 strict allowlist와 크기 제한을 적용하고 사용자 문장, 역명, mobility 조건,
  Tool argument/result와 exception 원문을 저장하지 않는다.
- event와 Tool summary는 owner-scoped thread 삭제에만 cascade하고 독립 TTL job을 만들지 않는다.
- legacy run에 가상 event를 backfill하지 않고 incomplete ledger로 구분한다.
- 기존 stateless `/v1/accessibility/questions`는 Phase 4A ledger 범위에서 제외한다.

#### 비범위

- public event route, SSE, `/ops`와 public processing timeline
- idempotency key와 결과 재사용
- API/worker 분리, DB lease, heartbeat와 recovery sweeper
- multi-replica 실행과 자동 crash recovery
- 원본 Tool artifact 저장과 checkpoint 밖 결과 재사용

#### 완료 조건

- memory와 PostgreSQL adapter가 같은 reservation, event sequence, transition과 owner 계약을
  통과한다.
- 예약 transaction 실패 시 active pointer, run row, event와 run count가 모두 rollback되고 graph
  호출은 0회다.
- concurrent event append에서 sequence 중복·역순이 없고 terminal 뒤 추가 event가 거부된다.
- graph 전, checkpoint 후, Tool 시작 후와 Tool 응답 후 crash fault injection이 각각 검증된다.
- PostgreSQL restart 뒤 open execution과 open Tool attempt를 탐지하지만 MCP를 자동 호출하지 않는다.
- question, answer, Tool payload와 secret sentinel이 ledger, exception representation과 JSON log에
  남지 않는다.
- thread 삭제가 checkpoint, run, event와 Tool summary 전체에 전파된다.
- response schema v3/v4와 OpenAPI에 ledger field, 새 public route, SSE나 `/ops`가 추가되지 않는다.

2026-07-14 검증에서는 memory·실제 PostgreSQL adapter의 원자적 reservation과 연속 sequence,
owner 격리, immutable transition, graph 기록 순서와 실패 경계, process crash orphan, 재시작 뒤
resume·open Tool 탐지, redaction, thread 삭제 cascade 및 기존 Web/OpenAPI 계약 유지를 확인했다.
이 완료는 crash를 감지하고 수동 분류할 수 있다는 의미이며 자동 복구나 end-to-end exactly-once
보장은 아니다.

### Phase 4B. 단계형 durable execution coordination

기준 결정은 [ADR-0011](adr/0011-staged-durable-execution-coordination.md)이다. 공개 API, worker 실행과
multi-replica를 한 번에 바꾸지 않고 다음 gate를 순서대로 연다.

#### Phase 4B-1 — 구현·검증 완료: repository coordination substrate

- repository 내부 atomic idempotency reservation과 invocation별 durable job 계약
- PostgreSQL queue state, lease claim, heartbeat, 만료 판정과 증가하는 fencing token
- 같은 key·payload 재사용, 같은 key·다른 payload conflict와 owner/target scope 격리
- closed schema·크기 제한이 있는 private JSONB job payload와 terminal/thread-delete scrub
- hosted PostgreSQL verified TLS와 database at-rest encryption fail-closed 검증
- stale token의 queue heartbeat·complete·fail·release compare-and-set 거부

이 단위에서는 public start/resume route가 `Idempotency-Key`를 수용하거나 durable job을 만들지 않는다.
API는 기존 동기 graph path와 response schema v3/v4를 그대로 사용하며 별도 worker loop, bounded wait,
checkpoint cursor recovery와 multi-replica를 활성화하지 않는다. application-level authenticated
encryption은 payload가 실제 process 경계를 넘는 Phase 4B-2의 선결 조건이다.

완료 조건은 다음과 같다.

- concurrent same-key repository reservation이 idempotency record와 job을 하나만 만들고 payload
  mismatch는 기존 record를 바꾸지 않는다.
- job claim은 하나만 성공하며 lease 교체 뒤 이전 fencing token의 write가 모두 거부된다.
- private payload가 event·Tool summary·log·error에 복제되지 않고 terminal과 thread 삭제 뒤 scrub된다.
- migration·restart 뒤 queue invariant가 유지되지만 기존 API crash를 자동 재실행하지 않는다.
- 기존 start → clarification → resume → result, OpenAPI, BFF와 Web 회귀가 그대로 통과한다.

#### Phase 4B-2 — 구현·검증 완료: atomic submit, worker와 bounded wait

- start/resume route와 BFF에 mutation별 `Idempotency-Key` 생성·보존 계약을 연결한다.
- API가 run, event, idempotency record와 job을 원자적으로 예약하고 worker가 lease로 claim한다.
- private payload에 application-level authenticated encryption과 key rotation 계약을 적용한다.
- Tool event append와 run finalization이 현재 queue lease token·generation을 같은 transaction에서
  검증하는 lease-bound writer/finalization command를 추가한다. `assert` 뒤 기존 writer를 호출하는
  분리된 TOCTOU 경계는 허용하지 않는다.
- API는 public snapshot을 bounded wait하고 timeout 뒤 graph를 대신 실행하거나 POST를 blind retry하지
  않는다.
- lease 만료, checkpoint cursor, last event와 open Tool attempt를 함께 분류한 recovery를 활성화한다.
- API와 worker는 각각 한 container·한 replica로 먼저 검증한다.

구현된 호환·장애 계약은 다음과 같다.

- BFF는 start/resume마다 opaque UUID key를 만들고 same-origin route를 통해 한 번만 전달한다.
- Agent API는 호환 기간 동안 header를 optional로 두며, key와 PostgreSQL durable coordinator가 모두
  있을 때만 queue 경로를 사용한다. key 없는 native/memory 요청은 기존 동기 경로다.
- bounded wait 만료는 public processing run을 만들지 않고 retryable
  `409 EXECUTION_IN_PROGRESS`로 끝난다. 같은 key는 기존 job과 snapshot에 다시 결합한다.
- job payload는 AES-256-GCM과 scope-bound AAD로 암호화하고 terminal transaction에서 scrub한다.
  active/decrypt-only previous keyring과 startup fingerprint 검증을 제공한다.
- stale lease의 Tool append와 run finalize는 transaction 안의 DB clock·token·generation fence에서
  rollback된다.
- terminal snapshot은 재실행 없이 job만 수렴시키고, exact checkpoint만 재개한다. open Tool이 남은
  모호한 구간은 자동 replay하지 않고 `EXECUTION_RECOVERY_REQUIRED`로 fail closed한다.
- 인증 실패·schema 불일치 payload job은 terminal로 격리·scrub해 poison job 한 건이 queue 전체를
  막지 않게 한다.
- thread 삭제는 schema v8 deletion fence로 새 reservation을 먼저 봉쇄하고 active job이 없을 때만
  checkpoint cleanup과 owner-scoped metadata cascade를 수행한다.

#### Phase 4B-3 — 진행 중: multi-replica release gate

##### Phase 4B-3A — 구현·검증 완료: worker readiness와 admission safety

- PostgreSQL schema v9의 `execution_worker_registry`가 DB clock 기준 ready heartbeat와 만료를
  기록하고 같은 worker ID의 재등록마다 token과 generation을 교체한다.
- idle Worker도 별도 presence heartbeat를 갱신하며 graceful shutdown은 heartbeat 중지, draining
  전환, active job grace, fenced row 제거 순서로 실행한다.
- 신규 start/resume reservation은 idempotency replay 확인 뒤 같은 transaction에서 ready·unexpired
  Worker row를 `FOR SHARE`로 잠근다. Worker가 없으면 run, event, job과 thread pointer를 하나도
  만들지 않고 retryable `503 EXECUTION_WORKER_UNAVAILABLE`로 rollback한다.
- 이미 예약된 동일 key 요청은 Worker 부재 중에도 기존 job 또는 terminal snapshot에 결합한다.
- queue health는 ready Worker 수, queued/leased 수, queue age, expired lease, recovery attempt와
  stuck job의 low-cardinality aggregate만 제공하며 owner, 질문, payload와 개별 job ID를 포함하지
  않는다.
- API `/health/ready`와 Worker readiness를 분리한다. Compose Worker는 hostname 기반 auto identity와
  mode-0600 process-local token·generation proof를 사용한다. 애플리케이션·migration을 로드하지 않는
  전용 read-only probe가 자기 fenced 등록의 ready·fresh 상태를 검사하고, 동일한 조건을
  `mobility-agent worker-health`로 수동 점검할 수 있다. registration loss나 내부 loop 종료는 process를
  non-zero로 끝내 stale container가 replacement row를 보고 healthy로 남지 않게 한다.

##### Phase 4B-3B — 구현·검증 완료: core multi-replica chaos safety

- Compose의 `EXECUTION_WORKER_ID=auto`는 container hostname으로 replica ID를 만들고, healthcheck는
  mode-0600 process-local state의 token·generation과 PostgreSQL registry row가 정확히 일치할 때만
  성공한다. 같은 문자열 ID의 replacement가 있어도 stale process는 healthy가 될 수 없다.
- job claim은 단순 worker ID가 아니라 현재 ready·fresh한 exact Worker registration의
  ID·token·generation에 결합한다. registration 교체와 claim은 같은 PostgreSQL transaction의 row
  lock으로 직렬화한다.
- Tool event와 finalization뿐 아니라 LangGraph `aput`·`aput_writes`도 current execution lease row를
  잠근 상태에서 실행한다. checkpoint에는 correlation과 함께 execution job ID·lease generation
  provenance를 저장하고, 복구는 같은 job의 이전 generation만 이어받는다.
- checkpointer adapter는 process 안의 모든 async saver read·write·delete를 하나의 outer lock으로
  직렬화하고, write는 반드시 이 lock을 먼저 얻은 뒤 lease row guard를 얻는다. 실제 2-worker vertical
  smoke에서 관측한 saver 내부 lock과 PostgreSQL pool/lease lock의 역순 획득을 제거하며 이 순서는
  회귀 테스트로 고정한다. cross-process correctness는 계속 DB lease guard가 담당한다.
- shutdown grace를 넘겨 task를 취소할 때 active lease를 compare-and-set으로 먼저 revoke한다.
  cancellation을 무시한 늦은 task가 checkpoint, Tool event나 terminal snapshot을 확정하려 해도
  stale lease fence에서 거부된다.
- production Settings나 환경 변수로 열리지 않는 constructor-only fault injector가 `after_claim`,
  `before_graph`, `before_finalize`, `after_finalize` 경계를 제공한다.
- 실제 PostgreSQL에서 복수 claim 경쟁, lease 교체 뒤 heartbeat·release·Tool append·finalize의 stale
  delayed write, checkpoint write와 reclaim의 row-lock 직렬화를 검증했다. 별도 subprocess를
  `after_claim`에서 hard-kill한 뒤 generation 2가 START부터 재실행해 terminal snapshot을 한 번만
  확정하고 private payload를 scrub하는 경로도 검증했다.
- Compose에서 서로 다른 auto identity의 Worker 두 개가 동시에 ready임을 확인했다. 실제 질문은
  `generation = claim_count = 1`로 완료되고 private payload가 scrub됐다. Worker 하나를 stop/start한
  단발 rolling smoke 동안 다른 Worker는 healthy·ready를 유지했다. Agent container만 recreate해도 두
  Worker container ID는 바뀌지 않았고, 중단된 run 조회와 기존 clarification resume이 완료됐다. 이
  검증은 correctness core gate이며 일반 운영 replica 제한을 해제하는 release 승인은 아니다.

##### Phase 4B-3C — 자동화 구현·정식 release 검증 대기: Worker replica release gate

구현된 runner는 local/test Compose의 기존 Agent 1개·Worker 1개·PostgreSQL 1개·Web 1개가 모두
healthy이고 Web public status가 operational일 때만 시작한다. Compose ownership label을 확인한 뒤
Agent는 1개로 고정하고 Worker만 2개로 확장한다. 따라서 3C의 release 가능 범위는
`Agent = 1, Worker = 2`이며 Agent 수평 확장을 승인하지 않는다. key 없는 호환 동기 경로가
process-local인 동안 Agent의 기본·최대 운영 replica는 계속 1개다.

- `scripts/run_replica_release_gate.py`와 PowerShell wrapper가 고정 profile, workload, 장애 주입,
  관측, deterministic 판정, evidence 기록과 cleanup을 하나의 실행 단위로 제공한다.
- Worker 2개를 모두 내려 신규 mutation이 정확히
  `503 EXECUTION_WORKER_UNAVAILABLE`로 fail closed하고 run/event/job을 남기지 않는지 먼저 검증한다.
- 대표 이동 workflow를 서로 다른 session lane에서 반복하고 매 다섯 번째 workflow는 structured
  clarification → resume까지 완료한다. 업무 `failed`를 성공으로 세지 않는다.
- Worker rolling은 부하와 겹쳐 실행한다. 대상이 아닌 Worker container 내부의 long-lived watcher가
  mode-0600 process-local token·generation과 PostgreSQL registry의 같은 exact registration을 DB clock
  기준으로 250ms마다 검증해 최소 ready Worker 1개를 요구한다. Agent recreate 동안에는 두 Worker를
  같은 방식으로 감시하고, 중단된 clarification 조회·resume과 Worker container 불변성을 확인한다.
- PostgreSQL을 최소 20초 중단한 동안 mutation은 정확히
  `503 PERSISTENCE_UNAVAILABLE`이어야 하며 POST를 재전송하지 않는다. 복구 후 Web, DB와 Worker 2개가
  budget 안에서 다시 수렴해야 한다.
- Agent container 내부에서 long-lived `execution_health_watch` stream 하나를 유지하며 DB-clock aggregate를
  smoke는 2초, release는 5초 간격으로 관측한다. Agent recreate 뒤에는 stream을 다시 열고 각 판정 전에
  새 표본을 요구한다. 실행 창 전체의 누적 DB evidence도 별도 closed schema로 검증한다.
- report는 profile·Compose SHA-256·image build 여부, aggregate workload/scenario/observer/DB evidence,
  stable violation code와 cleanup 결과만 atomic JSON으로 기록한다. 실행·사용자·Worker ID, 질문·답변,
  payload·ciphertext, token, command output과 raw log는 허용하지 않는다.
- 성공·실패·중단과 관계없이 PostgreSQL을 먼저 복구하고 Worker를 원래 1개로 축소하며 제품 health와
  registry를 확인한 뒤 생성한 thread를 owner session으로 삭제한다. cleanup 불완전은 gate 실패다.

고정 profile은 다음과 같다.

| profile | 관측 시간 | 예정 workflow / 평가 하한 | workload | Worker roll | Agent recreate | PostgreSQL outage | release eligible |
|---|---:|---:|---|---:|---:|---:|---|
| `smoke` | 600초 | 240 / 30 | 동시 2, 5초 간격 | 3 | 2 | 1 | 아니요 |
| `release` | 14,400초 | 2,880 / 600 | 동시 4, 20초 간격 | 12 | 6 | 3 | 모든 gate 통과 시 |

공통 budget은 queue warning 15초, queue failure 60초, stuck 120초, 장애 후 recovery 90초, 최종
drain 120초다. expired lease, 예상하지 않은 recovery attempt, stuck job, non-database readiness loss,
terminal ciphertext 잔존, terminal event 위반, ambiguous recovery 또는 drain 후 queued/leased job은
허용하지 않는다. steady 구간은 ready Worker 2개, Worker rolling 구간은 최소 1개를 요구한다. release
profile의 시간·workflow·장애 횟수 하한은 낮출 수 없고 image build 생략도 허용하지 않는다.

실행 명령은 다음과 같다.

```powershell
.\scripts\run_replica_release_gate.ps1 -Profile smoke
.\scripts\run_replica_release_gate.ps1 -Profile release
```

evidence는 `artifacts/release-gates/phase-4b-3c-<UTC>.json`에 남는다. smoke profile은 runner와 짧은
통합 경로를 확인할 뿐 항상 `release_eligible=false`다. 새 watcher 경로를 적용한 smoke 재검증은 대기
중이고 정식 4시간 release profile도 미실행·미승인이므로 기본 Compose의 Agent 1개·Worker 1개 제한은
유지한다. release report 통과와 수동 검토가 완료된 뒤에만 Worker 기본값을 2개로 바꾸는 별도 배포
변경을 수행한다.

현재 MCP Tool은 읽기 전용이며 recovery 중 같은 logical call이 다시 실행될 수 있다. idempotency는
중복 run을 막는 계약이고 MCP side effect의 exactly-once를 보장하지 않으므로 외부 호출 의미는
계속 at-least-once다. 쓰기 Tool은 provider-side idempotency와 사용자 승인 전에는 등록하지 않는다.

Phase 4B 전체에서 SSE, `/ops`, OIDC, LLM 활성화, UI 신규 기능, Tool artifact 저장, Redis/Kafka와
stateless endpoint ledger는 제외한다.

### Phase 4 전체 후속 산출물

- `agent_runs`, `run_events`, `tool_executions` ledger
- graph 실행 전에 run ID를 할당하고 request/run/thread correlation을 RuntimeContext에 전달
- idempotency key와 중복 run 방지
- API/worker 분리, lease claim과 recovery sweeper
- HTTPX/MCP/model timeout과 제한된 retry
- circuit breaker, cancellation propagation, 전체 deadline
- SSE event 저장·재생과 `Last-Event-ID`
- fault injection test

### Phase 4 전체 완료 조건

- 같은 idempotency key 요청은 새 run을 만들지 않고, 이미 기록된 완료 결과를 재사용한다.
- MCP 호출은 at-least-once임을 테스트와 문서에 명시하고 crash window의 재호출을 허용한다.
- Tool 실행 중 worker를 종료해도 lease 만료 후 같은 run/checkpoint에서 완료된다.
- MCP 내부 retry와 Agent retry가 증폭되지 않는다.
- client SSE 재접속 시 event가 빠지거나 중복 적용되지 않는다.
- MCP schema mismatch는 보수적 오류 응답과 alert로 끝나며 LLM 추측으로 우회하지 않는다.

## Phase 5. 관측성, 보안과 배포

### 산출물

- JSON 구조화 log와 전 구간 correlation
- OTel FastAPI/HTTPX/PostgreSQL 자동 계측과 graph/MCP custom span
- Prometheus metric과 운영 dashboard
- 선택적 LangSmith tracing/evaluation
- OIDC/JWT, object authorization, rate/body/concurrency limit
- trace/log redaction과 보존·삭제 job
- multi-stage non-root image, Compose 운영 profile
- dependency audit, image scan과 SBOM release gate

### 완료 조건

- 한 요청을 API → worker → graph → LLM/MCP → DB까지 trace로 찾을 수 있다.
- token/cost, Tool latency, retry/fallback, `PARTIAL`/`UNKNOWN` 비율을 dashboard에서 확인한다.
- 인증 token, 모델 key, MCP token, 원본 위치·이동 제약이 log/trace에 남지 않는다.
- hosted 환경은 checkpoint/store/artifact encryption key가 없거나 암호화가 꺼져 있으면 시작하지 않는다.
- liveness와 readiness가 분리되고 MCP degraded/schema mismatch가 readiness에 반영된다.
- graceful shutdown이 새 run 수락을 중지하고 처리 중 lease/checkpoint를 안전하게 넘긴다.

## Phase 6. 평가와 릴리스 기준

### 산출물

- 질문 유형·이동 조건·부분 실패·다중 턴·공격 입력별 offline dataset
- deterministic 안전 evaluator와 보조 LLM judge
- 범용 MCP ReAct baseline
- 전용 LangGraph와 baseline 비교 report
- online trace sampling과 regression dataset 승격 절차
- 아키텍처·운영 runbook·장애 대응 문서

### 완료 조건

- routing/task 성공률, Tool 호출 수, token, latency, 비용을 동일 dataset에서 비교한다.
- `PARTIAL`/`UNKNOWN` 왜곡, 필수 clarification 누락, 근거 없는 시설·경로 생성은 release blocker다.
- MCP 및 dependency upgrade PR이 contract, E2E, eval threshold를 모두 통과한다.
- 장애 시나리오별 탐지 신호, 사용자 영향, 자동 복구, 수동 조치가 runbook에 기록된다.

## 현재 구현 단위

Phase 4B-1의 repository substrate와 Phase 4B-2 single-worker 실행 위에 Phase 4B-3A가 DB-clock
Worker registry, idle heartbeat, atomic admission gate, 독립 healthcheck와 redacted queue health를
구현했다. Phase 4B-3B는 exact registration claim, lease-fenced checkpoint, checkpoint execution
provenance, forced-stop lease revocation과 deterministic fault seam을 추가했다. 정적 검사, memory 계약,
Web 계약, 실제 PostgreSQL schema v9의 경쟁·stale write·checkpoint lock과 subprocess hard-kill replay를
함께 검증한다. Phase 4B-3C는 Agent 1개·Worker 2개 scope의 고정 smoke/release profile, 반복 workload와
장애 주입, aggregate queue/database evidence, deterministic 판정, redacted report와 기본 Worker 1개
복구를 자동화했다.

다음 활성화 단위는 새 long-lived watcher 경로로 smoke profile을 재검증한 뒤, 구현된 runner의 정식
4시간 release profile을 실행하고 report와 수동 복구 절차를 검토하는 것이다. 이 승인이 끝나기 전에는
API와 worker를 각각 하나로 제한한다. 승인 뒤에도 Worker
기본값만 2개로 바꾸며 Agent는 별도 계약 전까지 1개로 유지한다. 이 경계에서는
stateless legacy question endpoint ledger, 인증된 history, SSE, `/ops`, 지도와 공개 processing timeline을
계속 제외한다.

## 초기 수직 기능 기록

첫 PR은 Phase 0 전체와 Phase 1의 가장 얇은 vertical slice만 포함한다.

1. Python/uv/src layout과 품질 도구를 고정한다.
2. FastAPI lifespan에서 MCP client를 생성한다.
3. startup 시 `answer_accessibility_question` schema 존재를 검증한다.
4. 하나의 입력 API가 해당 Tool을 호출하고 structured artifact를 DTO로 검증한다.
5. MCP mock mode를 실제 HTTP로 띄운 contract test를 추가한다.
6. canonical `user_message`, `PARTIAL`, timeout 세 경로를 검증한다.

이 단위가 통과하기 전에는 메모리, 범용 planner, worker, Redis나 UI를 추가하지 않는다.

## 공통 Definition of Done

- 변경된 정책은 ADR 또는 architecture 문서에 반영된다.
- public API·state·SSE event에는 schema version이 있다.
- 정상, 경계, 실패, 재시작 경로 테스트가 있다.
- type ignore와 경고 억제에는 구체적인 이유가 있다.
- metric/log/trace에 필요한 correlation과 redaction을 검증한다.
- 새 설정은 validation, `.env.example`, 운영 문서와 함께 추가한다.
- 성능 또는 비용에 영향을 주는 변경은 전후 측정값을 남긴다.
