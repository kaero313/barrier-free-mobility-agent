# ADR-0008: 제품 신뢰 surface와 운영 surface 경계

- 상태: Accepted
- 결정일: 2026-07-13

## 배경

Barrier-Free Mobility Agent는 자연어 대화를 입력으로 사용하지만 최종 목적은 채팅 자체가
아니라 이동 가능성에 관한 확인 결과, 근거와 한계를 안전하게 전달하는 것이다. 초기
`/assistant` vertical slice는 서비스 상태, 대화와 결과를 고정 3열에 배치해 API 상태 조합을
검증했다. 이 구조는 결과가 없는 시점에도 빈 결과 영역을 차지하고 사용자 작업과 운영 정보를
한 화면에 섞는다.

공개 response schema v3는 canonical `user_message`, 구조화된 `context`, `presentation`과
allowlist evidence를 제공한다. 반면 graph node, Tool attempt, token ledger, state diff와 복구
이력은 아직 공개 계약이 아니며 위치·이동 조건과 내부 정책을 포함할 수 있다. 저장된 event가
없는 진행 단계나 운영 지표를 화면에서 추정해서도 안 된다.

따라서 사용자에게 필요한 신뢰 정보와 인증된 운영자가 필요한 진단 정보를 서로 다른 계약과
route로 분리해야 한다.

## 결정

### 1. 제품 route

동일 저장소와 동일 제품 release 안에서 다음 route를 사용한다.

```text
/
├─ /assistant
├─ /how-it-works
├─ /status
├─ /accessibility
└─ /ops                 protected internal surface
   └─ /ops/runs/{run_id}
```

- `/`는 해결하는 문제, 지원 범위, 안전 원칙과 주요 진입점을 설명한다.
- `/assistant`는 이동 질문, clarification, 결과와 공개 근거에 집중한다.
- `/how-it-works`는 사용자 관점의 처리 방식, 불확실성 정책과 한계를 설명한다.
- `/status`는 민감정보가 제거된 readiness와 확인 시각만 제공한다.
- `/accessibility`는 화면 사용법, 지원 기준과 피드백 경로를 제공한다.
- `/ops`는 public navigation에 노출하지 않고 hosted 환경에서 OIDC와 역할 권한을 요구한다.

Next.js UI와 BFF, FastAPI/LangGraph runtime은 계속 하나의 제품 경계로 관리한다. MCP만 외부
독립 서비스다.

### 2. Assistant의 상태 적응형 layout

Assistant는 항상 같은 3열을 유지하지 않는다.

- 질문 전: 최대 880px의 단일 중심 column에 범위 설명, 질문 입력과 실제 지원 예시를 둔다.
- clarification 중: 서버가 확인한 현재 조건, Agent 질문과 답변 control을 같은 작업 흐름에 둔다.
- 실행 중: 실제 run event가 없으면 indeterminate 상태만 표시한다.
- 결과 후: desktop에서 결과 본문 8 column과 공개 근거 4 column으로 확장한다.
- 실패·결과 미확정: 빈 결과 panel 대신 오류, 보존된 입력과 안전한 복구 action을 표시한다.

서비스 readiness가 정상일 때 고정 sidebar를 차지하지 않는다. 지원 범위 또는 연결 상태가
사용자 행동에 영향을 줄 때만 header status와 inline notice로 표시한다. 결과가 생기기 전에는
`판단 대기 중` panel을 렌더링하지 않는다.

### 3. 공개 신뢰 정보

Assistant와 `/how-it-works`가 사용할 수 있는 데이터는 public DTO로 제한한다.

- canonical `user_message`
- 업무 상태와 실행 상태의 안전한 표현
- `ConversationContextView`
- `AnswerPresentation`
- 공개 evidence source, 확인 시각, 제한·미확인 항목
- 사용자가 소유한 현재 run의 request ID와 복구 action

Tool 이름, 원본 인자·결과, prompt, 내부 state, 모델 reasoning과 추정 timeline은 공개하지
않는다. 공개 실행 설명이 추가로 필요하면 public schema에 별도의 allowlist projection을 먼저
정의하고 객체 소유권, redaction과 크기 제한을 검증한다. 내부 ops DTO를 재사용하지 않는다.

### 4. 운영 정보

`/ops`는 `/internal/ops/v1`의 versioned read model만 사용한다. run event, Tool execution,
budget, model identity, contract version, recovery와 evaluation은 실제 저장된 event 또는
projection에서만 만든다. public status endpoint는 ops overview의 축약판이 아니라 별도
allowlist 계약이다.

production 운영 화면은 외부에 공개하지 않는다. 검증 환경에서 예제 실행이 필요하면 synthetic
data와 실제 운영 data를 route, credential과 시각 표시로 분리한다.

### 5. 문서 서술 원칙

공개 문서와 화면은 다음 항목을 중심으로 작성한다.

- 해결하려는 사용자 문제와 지원 범위
- 확인 가능한 현재 동작
- 설계 제약과 선택한 trade-off
- 실패·복구·보안·개인정보 정책
- 재현 가능한 실행, 테스트와 평가 방법
- 아직 구현하지 않은 기능과 운영상 한계

개인 배경이나 경력 설명을 제품 요구사항과 섞지 않는다. 구현되지 않은 성능, 비용 절감,
품질이나 운영 성과를 주장하지 않는다.

## 데이터 경계

| 정보 | Assistant·공개 문서 | `/status` | `/ops` |
|---|---|---|---|
| canonical 사용자 응답 | 현재 사용자 run만 | 제외 | 권한·masking 적용 |
| 이동 조건·역 | 확인된 현재 context만 | 제외 | 기본 masking |
| 공개 evidence와 확인 시각 | 포함 | dependency 시각만 | 포함 |
| Tool attempt·latency | 제외 | 제외 | 저장된 event만 포함 |
| token·model usage | 제외 | 제외 | 저장된 ledger만 포함 |
| 원본 Tool payload·prompt·reasoning | 제외 | 제외 | 제외 |
| readiness | 행동에 영향이 있을 때 요약 | allowlist 상세 | 운영 상세 |
| request·trace correlation | 현재 request ID만 | 제외 | 권한에 따라 포함 |

## 결과

### 장점

- 첫 화면이 실제 이동 질문에 집중한다.
- 결과와 근거가 대화 말풍선보다 높은 정보 우선순위를 갖는다.
- 사용자 신뢰 정보와 운영 진단 정보의 보안 경계가 명확해진다.
- 저장되지 않은 timeline이나 지표를 화면에서 만들 가능성을 줄인다.
- 향후 run ledger와 운영 Console을 public API 변경 없이 확장할 수 있다.

### 비용

- 기존 3열 Assistant CSS와 component 구성을 상태 적응형 layout으로 재구성하는 비용이 발생했다.
- `/`, `/how-it-works`, `/status`에 대한 route와 콘텐츠 계약이 추가된다.
- public evidence projection과 internal ops projection의 중복 필드를 의도적으로 따로 관리한다.
- 구조화된 여러 clarification 답변은 별도 API 계약 결정 전까지 제공할 수 없다.

## 구현 순서

1. 완료: schema v3를 유지한 채 Assistant empty·clarification·result layout을 재구성한다.
2. `/`와 `/how-it-works`의 제품 설명을 현재 capability에서 파생하도록 구현한다.
3. `/status`용 최소 allowlist DTO와 cache·오류 정책을 정의한다.
4. 완료: structured clarification request와 public schema 전환을 [ADR-0009](0009-structured-clarification-contract.md)로 분리했다.
5. Phase 4 run/event ledger 이후 보호된 `/ops`를 구현한다.

## 검증 기준

- 질문 전 화면에 빈 결과·운영 panel이 없다.
- 결과 후 canonical message, 구조화 결과, 제한과 확인 시각이 순서대로 읽힌다.
- browser가 Agent API를 직접 호출하지 않는다.
- public route에서 내부 Tool·state·prompt·reasoning을 조회할 수 없다.
- `/status` 실패가 Assistant의 기존 결과를 숨기지 않는다.
- `/ops` DTO와 public DTO가 타입과 endpoint 수준에서 분리된다.
- 320 CSS px reflow, 400% 확대, keyboard, screen reader, forced-colors와 reduced-motion을 통과한다.
