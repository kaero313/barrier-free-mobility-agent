# 제품 정보 구조

## 1. 문서 역할

이 문서는 Web route, 대상 사용자, 인증 경계와 화면 reading order의 단일 기준이다. API 의미와
도메인 상태는 [아키텍처](architecture.md), 화면 표현은 [제품 디자인](product-design.md),
상태 전이는 [사용자 흐름](user-flows.md), 운영 진단은 [Agent Console 계약](ops-console.md)을
따른다. 경계 결정은
[ADR-0008](adr/0008-product-trust-and-operations-surfaces.md)에 기록한다.

## 2. Route matrix

| Route | 역할 | 대상 | 인증 | 데이터 계약 | 현재 상태 |
|---|---|---|---|---|---|
| `/` | 지원 범위·안전 원칙·주요 진입점 | 일반 사용자 | 없음 | 정적 제품 계약 + 요약 capability | Phase 3C-2 구현 |
| `/assistant` | 이동 질문·clarification·결과 | 일반 사용자 | local anonymous, hosted 사용자 인증 예정 | public response schema v3/v4 | Phase 3C-3 structured form 진행 |
| `/how-it-works` | 판단 방식·불확실성·데이터 사용 설명 | 일반 사용자·개발자 | 없음 | versioned 정적 설명 | Phase 3C-2 구현 |
| `/status` | 정제된 서비스 가용성 | 일반 사용자 | 없음 | `PublicServiceStatus` | Phase 3C-2 구현 |
| `/accessibility` | 사용법·지원 기준·피드백 | 일반 사용자 | 없음 | 정적 콘텐츠 | Phase 3C-2 구현 |
| `/ops` | 운영 overview | 운영자 | hosted OIDC + `ops.viewer` | `/internal/ops/v1` | Phase 5 목표 |
| `/ops/runs/{run_id}` | 실행 진단 | 운영자 | `ops.viewer`, 일부 `ops.diagnostics` | versioned ops read model | Phase 5 목표 |

`/ops`와 하위 route는 public navigation, sitemap과 일반 검색 surface에 포함하지 않는다.
route를 숨기는 것만으로 보호하지 않고 BFF와 Agent API 양쪽에서 권한을 검증한다.

## 3. Navigation

### Public header

```text
Barrier-Free Mobility | 이동 조회 | 작동 방식 | 서비스 상태
```

- brand는 `/`로 이동한다.
- `/accessibility`는 footer와 mobile 지원 메뉴에서 제공한다.
- 정상 readiness를 지속적인 badge나 sidebar로 강조하지 않는다.
- degraded·unavailable 상태가 현재 행동에 영향을 줄 때만 짧은 상태와 `/status` link를 표시한다.

### Assistant contextual actions

- 질문 전: `조회 시작`
- clarification 중: `입력한 정보로 계속`
- 결과 후: `이어서 질문`, `새 이동 조회`, `판단 근거와 정보 한계`
- 실패 후: 입력을 보존할 수 있는 복구 action, `새 이동 조회`, request ID disclosure

내부 Tool, graph와 model 진단 link는 public Assistant action으로 제공하지 않는다.

## 4. 상태 계층

서로 다른 의미의 상태를 하나의 badge나 진행률로 합치지 않는다.

1. `ServiceAvailability`: 전역 서비스와 dependency의 현재 가용성
2. `ConversationLifecycle`: thread, run, interrupt, resume와 terminal lifecycle
3. `BusinessOutcome`: `SUCCESS | PARTIAL | NEEDS_CLARIFICATION | FAILED`
4. `EvidenceCompleteness`: 확인된 정보, 제한, 미확인 부분과 실패 source
5. `PublicActivity`: 실제 공개 event가 저장된 뒤에만 제공하는 coarse 처리 단계
6. `OpsExecutionDetail`: 인증된 운영자만 보는 node·Tool·model·recovery 진단

`ServiceAvailability=ready`가 개별 결과의 성공을 의미하지 않으며
`run_status=completed`가 `BusinessOutcome=SUCCESS`를 의미하지 않는다. evidence가 일부 있다는
이유로 `PARTIAL`을 성공처럼 표현하지 않는다.

## 5. 데이터 projection

### Public answer projection

현재 사용자 run에 대한 schema v3/v4 public projection만 사용한다. BFF는 호환 기간에 내부
`X-Agent-Response-Schema: 4` header로 v4를 선택한다.

- canonical message
- 공개 context와 presentation
- 업무·실행 상태
- allowlist evidence와 확인 시각
- 제한, 실패 source와 미확인 항목
- 현재 request ID

### Public status projection

raw readiness를 그대로 전달하지 않는다. 외부 행동에 필요한 서비스·기능 가용성, 확인 시각과
안내 문구만 포함한다. 세부 계약은 [공개 서비스 상태](public-status.md)를 따른다.

### Internal ops projection

run event, Tool execution, budget, state diff, model identity, recovery와 evaluation을 포함할 수
있지만 저장된 event와 redacted read model만 사용한다. public answer 또는 status 타입과 공유하지
않는다.

### 모든 projection에서 제외

- secret과 credential
- raw prompt와 model reasoning
- 원본 Tool arguments·payload
- 임의의 사용자 질문·위치·이동 조건
- 저장 event가 없는 추정 timeline

## 6. Assistant reading order

### 질문 전

1. skip link와 public header
2. page heading와 지원 범위
3. 질문 label, composer, 글자 수와 submit
4. 실제 가능한 예시와 정보 한계
5. footer와 접근성 link

### Clarification 중

1. page heading와 새 조회
2. 서버가 확인한 현재 이동 조건
3. 짧은 이전 대화
4. active clarification heading, prompts와 form
5. 잠긴 일반 입력에 대한 설명
6. 대화 관리 action

### 결과 후

1. page heading와 새 조회
2. 서버가 확인한 현재 이동 조건
3. 업무 상태와 canonical message
4. 구조화된 결과, 안전 안내와 다음 행동
5. 판단 근거와 정보 한계
6. 이어서 질문·새 이동 조회
7. 이전 대화와 대화 관리

Desktop 2열은 이 DOM 순서를 CSS로만 배치한다. mobile, 400% 확대와 screen reader에서도 같은
순서를 유지한다. 결과를 composer 뒤로 보내거나 동일 결과를 sidebar에 복제하지 않는다.

## 7. 구현 경계

- Phase 3C-1은 schema v3를 바꾸지 않고 `/assistant` layout과 reading order를 개편한다.
- Phase 3C-2에서 `/`, `/how-it-works`, `/status`, `/accessibility`와 public navigation을 추가했다.
- structured clarification은 ADR-0009의 approved public request 계약과 `mobility-v2` flow로
  구현하며, typed field answer를 frontend나 자연어 parser가 재해석하지 않는다.
- public processing activity는 Phase 4 event ledger와 owner-scoped projection 뒤에 구현한다.
- `/ops`는 Phase 5 OIDC, RBAC, redaction과 audit가 준비된 뒤 구현한다.

현재 계약에 없는 route history, 진행률, 지도, 비용과 운영 집계를 fixture로 production 화면에
노출하지 않는다.

## 8. 검증

- route별 인증·데이터 계약을 integration test로 검증한다.
- public page가 `/internal/ops/v1`을 호출하지 않는지 E2E에서 검사한다.
- Browser가 FastAPI port를 직접 호출하지 않는지 검사한다.
- 모든 상태에서 heading hierarchy, focus order와 live region을 검사한다.
- 320 CSS px, 400% 확대, 한국어 IME, forced-colors와 reduced-motion을 검사한다.
- sitemap과 public navigation에 `/ops`가 포함되지 않는지 검사한다.
