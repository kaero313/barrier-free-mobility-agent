# Barrier-Free Mobility 제품 디자인

## 1. 문서 상태

- 상태: Phase 3A 구현 기준과 Phase 3C 제품 신뢰 surface의 승인된 목표 설계
- 대상: 사용자용 이동 도우미와 운영자용 Agent Console
- 기준: 신뢰 가능한 이동 정보, 접근성 우선, 내부 실행 정보의 통제된 공개

이 문서는 화면이 현재 API에서 실제로 받을 수 있는 정보와 후속 단계에서 추가할 정보를 구분한다. 프론트엔드는 자연어 응답을 다시 파싱해 출발지, 목적지, 위험도나 이동 가능성을 추론하지 않는다.

제품 source는 하나의 `barrier_free_mobility_agent` 저장소에서 관리한다. Next.js UI와 BFF는 root `web/`, FastAPI/LangGraph runtime은 `src/barrier_free_mobility_agent/`에 두며 두 계층을 하나의 웹 기반 Agent 제품과 release gate로 다룬다. Barrier-Free Mobility MCP만 별도 독립 프로젝트다.

## 2. 경험 원칙

1. **대화는 입력 방식이고 결과는 구조화한다.** 긴 결과를 채팅 말풍선 하나에 넣지 않는다.
2. **확정성보다 근거를 우선한다.** `PARTIAL`, `UNKNOWN`, 제한 사항과 확인 시각을 주요 결과와 함께 보여준다.
3. **색상만으로 상태를 전달하지 않는다.** 상태명, 아이콘과 설명을 항상 함께 제공한다.
4. **지도는 보조 수단이다.** 경로와 주의 사항은 텍스트 목록만으로도 완전히 이해할 수 있어야 한다.
5. **기술 용어는 surface별로 분리한다.** 사용자 화면에는 MCP, LangGraph, Tool 이름을 노출하지 않고 운영 화면에서만 제공한다.
6. **실제 event만 진행 상태로 표시한다.** SSE가 없는 Phase 2C에서는 `조회 중`만 사용하고 가상의 node 진행률을 만들지 않는다.
7. **불확실한 입력을 대신 결정하지 않는다.** 이동 조건과 역 후보는 기본 선택 없이 사용자가 확인한다.
8. **첫 화면은 사용자 작업에 집중한다.** 결과가 생기기 전에는 빈 결과 panel이나 운영 정보를 고정 배치하지 않는다.
9. **신뢰 정보와 진단 정보의 계약을 분리한다.** 사용자는 공개 근거와 한계를 보고, 인증된 운영자는 별도 read model로 실행을 진단한다.
10. **문서는 현재 동작과 한계를 증명한다.** 개인 배경보다 문제, 제약, trade-off와 재현 가능한 검증 방법을 기록한다.

## 3. 제품 surface

### 3.1 제품 진입과 신뢰 정보

기본 경로 `/`는 지원하는 이동 질문, 정보의 한계와 `/assistant` 진입점을 설명한다. 제품
navigation은 `이동 조회`, `작동 방식`, `서비스 상태`를 기본으로 하며 기술 진단 route를
사용자 작업과 같은 수준에 배치하지 않는다.

- `/how-it-works`: 자연어 질문, clarification, 정보 조회와 근거 기반 응답 원칙
- `/status`: 민감정보가 제거된 readiness, dependency 상태와 확인 시각
- `/accessibility`: keyboard·screen reader 사용법, 지원 기준과 피드백 경로

`/status`는 `/internal/ops/v1` 응답을 browser에 전달하지 않는다. public allowlist DTO를
별도로 사용하고 상태 조회 실패가 이미 받은 Assistant 결과를 숨기지 않게 한다.

### 3.2 사용자용 Assistant

기본 경로는 `/assistant`다.

Phase 2C의 conversational thread API는 이동 경로 접근성 질의를 기본으로 지원하고, 역 엘리베이터·장애인 화장실의 새 진입점은 BFF가 확인한 `readiness.status=ready`, `mobility_mcp.status=ready`, `facility_question` capability를 모두 만족할 때만 활성화한다. 현재 CI-pinned MCP SHA에는 이 extension이 없으므로 일반 배포 화면은 계속 `출발역에서 도착역까지의 이동 확인` 범위로 표시한다. readiness가 나빠져도 이미 응답으로 받은 facility result card는 숨기지 않는다. 대체경로 대화는 아직 제공하지 않으며 frontend가 자연어 intent를 판별해 endpoint를 선택하지 않는다.

- 새 이동 조회 시작
- 자연어 질문 제출
- 이동 조건 명확화
- 모호한 역 후보 선택
- capability가 확인된 환경의 역 시설 상태·위치·설치 여부 조회
- 결과, 위험, 제한 사항과 확인 시각 확인
- 실패한 조회를 새 run으로 다시 제출
- 현재 thread 삭제

사용자 화면은 하나의 active thread에 집중한다. Phase 2C에는 thread 목록 API가 없으므로 서버에 저장된 최근 대화 목록을 제공하는 것처럼 표현하지 않는다.

### 3.3 공개 판단 근거와 처리 내역

현재 schema v3와 v4에서는 canonical `user_message`, `ConversationContextView`,
`AnswerPresentation`, 공개 evidence, 제한·미확인 항목과 확인 시각만 사용자에게 제공한다.
이 정보는 결과 카드의 `판단 근거와 정보 한계` disclosure에서 읽을 수 있어야 한다.

graph node, Tool attempt, token ledger와 recovery event를 공개 처리 내역으로 추가하려면 먼저
owner-scoped allowlist projection, redaction과 크기 제한을 가진 public API 계약이 필요하다.
내부 ops DTO, 원본 `AgentState`, prompt, reasoning이나 Tool payload를 재사용하지 않는다.

### 3.4 운영자용 Agent Console

기본 경로는 `/ops`이며 사용자용 navigation에서는 노출하지 않는다. hosted 환경에서는 별도 권한을 요구한다.

- 서비스와 MCP contract readiness
- run 상태와 오류 분포
- graph·Tool execution timeline
- budget과 누적 ledger
- schema fingerprint와 flow/state version
- 정제된 checkpoint diff
- 향후 token, latency, 평가와 비용 지표

현재 API는 readiness를 제외한 운영 조회 endpoint를 제공하지 않는다. Console 전체 구현은 read-only 운영 API와 내구 ledger 이후 활성화한다.

## 4. 정보 구조

```text
Web
├─ /
│  ├─ 지원 범위와 안전 원칙
│  └─ Assistant 진입
├─ /assistant
│  ├─ Phase 2C: trip + capability-gated station facility
│  ├─ 현재 이동 조건 요약
│  ├─ 대화와 clarification
│  ├─ 이동 판단 결과
│  └─ 근거와 제한 사항
├─ /how-it-works
│  ├─ 사용자 관점 처리 방식
│  └─ 불확실성·한계 정책
├─ /status
│  └─ public readiness projection
├─ /accessibility
│  ├─ 화면 사용법
│  └─ 접근성 지원과 피드백
├─ /ops
│  ├─ overview
│  ├─ runs
│  │  └─ /ops/runs/{run_id}
│  ├─ contracts
│  └─ evaluations
```

`/ops`는 public navigation에 노출하지 않으며 hosted 환경에서 인증과 역할 권한을 요구한다.
결정 근거와 route별 데이터 경계는
[ADR-0008](adr/0008-product-trust-and-operations-surfaces.md)을 따른다.

## 5. 현재 기능과 목표 기능

| 기능 | Phase 2C UI | 목표 UI | 필요한 계약 |
|---|---|---|---|
| 제품 진입 화면 | redirect만 제공 | 지원 범위·안전 원칙·Assistant 진입 | 현재 capability와 정적 제품 계약 |
| 작동 방식 | 제공하지 않음 | 사용자 관점 처리·불확실성 설명 | 정적 콘텐츠와 공개 schema 설명 |
| 공개 서비스 상태 | Assistant 내부 readiness | 독립 `/status` | public allowlist readiness DTO |
| 새 thread | 가능 | 가능 | `POST /v1/threads` |
| 질문·resume | 가능 | 가능 | start/resume API |
| 역 시설 대화 | capability 조건부 | 통합 Assistant | readiness `facility_question`, structured facility graph |
| 대체경로 대화 | 제공하지 않음 | 통합 Assistant | alternative subgraph 필요 |
| 역 후보 선택 | 가능 | typed single-choice | v4 `answer_request.inputs`와 v3 legacy choice projection |
| 결과와 근거 | 가능 | 구조화된 결과 카드 | `AnswerPresentation`과 `evidence` |
| 현재 출발지·도착지·이동 조건 | 가능 | 서버 확인 상태 표시 | run별 immutable `ConversationContextView` |
| 최근 thread 목록 | 제공하지 않음 | 계정별 목록 | thread list API 필요 |
| 실제 실행 진행률 | indeterminate loading만 사용 | event timeline | SSE run events 필요 |
| run 취소 | 제공하지 않음 | 취소 가능 | cancel API 필요 |
| refresh 후 대화 복원 | 제공하지 않음 | active thread 복원 | thread 조회·메시지/active run API 필요 |
| 운영 run 검색·상세 | 제공하지 않음 | Console에서 제공 | run/ledger admin API 필요 |
| 지도 | 제공하지 않음 | 선택적 보조 view | 구조화된 route/geometry 계약 필요 |
| 공개 처리 단계 | indeterminate loading만 사용 | 저장된 안전한 처리 event | owner-scoped public event projection 필요 |
| 운영 실행 상세 | 제공하지 않음 | 보호된 Console | run/event ledger와 internal ops API 필요 |

Capability gate는 `checking | ready_trip_only | ready_trip_and_facility | not_ready | fetch_error`를 구분한다. 확인 전에는 시설 UI를 잠깐 노출했다가 숨기는 flash를 만들지 않는다. `not_ready`와 `fetch_error`를 capability 부재로 영구 저장하지 않으며, gate는 새 질문 예시와 action에만 적용하고 과거 facility transcript와 presentation 렌더링에는 적용하지 않는다.

### 5.1 Agent-owned UI 계약

Run 응답은 MCP 원본과 내부 `AgentState`를 노출하지 않는 공개 projection을 제공한다. 호환 기간의
기본 응답은 schema v3이고, 동일 제품의 BFF는 `X-Agent-Response-Schema: 4`로 schema v4를
선택한다.

```text
ConversationContextView
├─ intent discriminator
├─ origin, destination: query, resolved station, line, status
├─ confirmed mobility conditions and public provenance
├─ facility target, requested types and provenance
└─ active clarification: id, prompt IDs, fields

ClarificationView v2 | null
├─ id
├─ prompts[]: id, question, fields[]
└─ answer_request
   ├─ single_text: min/max constraints
   └─ inputs[]
      ├─ id, prompt_id, stable field
      ├─ control: text | tri_state | single_choice
      ├─ label, required
      ├─ options[]: opaque value, label
      └─ constraints

AnswerPresentation = TripAnswerPresentation | FacilityAnswerPresentation | null
├─ trip: origin, destination, route_summary
├─ facility: station, question kind, typed item/detail states
├─ safety_notice
├─ confidence_reasons[]
└─ allowlisted evidence source summaries
```

Response schema v4의 `ClarificationView` v2는 `answer_request`를 UI authority로 사용한다.
input의 `id`는 현재 form의 control·오류·제출을 연결하고, `prompt_id`는 질문과 control의 관계를,
`field`는 Agent-owned semantic ID를 나타낸다. Resume의 `input_id`는 이 `id`만 참조한다. Web은
prompt, label이나 option text에서 이 값을
추론하지 않는다. Web은 질문 문구를 해석해 field나 극성을 만들지 않는다. 출발지·도착지,
계단·에스컬레이터와 역 후보만 초기 structured allowlist다.
MCP 후속 질문, `other`, facility 복합 field 또는 allowlist하지 않은 조합은 form 전체를
`single_text`로 유지한다.

Resume v2는 `schema_version=2`, 필수 `clarification_id`와
`answer.mode=single_text | fields`인 tagged body를 사용한다. `fields`는 하나의 field도
`values[]`로 제출한다. 각 item은 `input_id`와 `{kind: text | choice | tri_state, value}`만 담고
stable field와 control을 중복 전송하지 않는다. 모든 required input을 atomic하게 적용하며
누락·중복·추가·kind/control·option
오류는 422이며 일부 값만 checkpoint에 저장하지 않는다. 기존 `{answer: string,
clarification_id?: string}` body는 response v3와 함께 한 호환 기간 유지한다.

Agent는 lock 안에서 최신 active ID를 먼저 비교한다. 오래된 ID는 answer validation보다 먼저
`409 STALE_CLARIFICATION`으로 거절하고 run을 변경하지 않는다. Web은 draft를 보존하고 최신 run을
GET으로 한 번 읽되 field가 같아도 자동 재제출하지 않는다. 계단·에스컬레이터에 사용자가
`unknown`을 선택하면 false로 추정하지 않고 MCP 호출 없이 업무 `FAILED`로 종료한다.

Application은 active answer request를 검증한 뒤 structured answer를 LangGraph
`Command(resume=...)` payload로 전달한다. Graph clarification node가 기존 trip/mobility slot에
값을 한 번에 적용하며 자연어 parser를 거치지 않는다. station token은 현재 checkpoint의 candidate
순서에 직접 매핑한다. `unknown`은 graph terminal branch에서 pending clarification을 제거하고
업무 실패 checkpoint를 정상 완료한다. 이 topology 변경으로 flow는 `mobility-v2`, state schema는
계속 3이다.

평면 `questions[]`와 `clarification_options[]`는 schema v3 migration용 deprecated field로 남는다.
v3는 기존 `choice | free_text`, `single_text` 형태를 변경하지 않고, BFF가 선택한 v4 Web은 nested
v2 계약만 사용한다.

`ConversationContextView`는 run 생성 시 snapshot으로 저장되어 후속 run이 이전 run의 GET 결과를 바꾸지 않는다. `intent`에 따라 trip context 또는 facility context 하나만 active하게 공개하며 retained trip 조건을 시설 조회 조건처럼 표시하지 않는다. `unknown` 또는 보수적으로 파생한 mobility 값은 확인된 조건으로 표시하지 않는다. `current_input`, MCP profile, station 후보 원본, Tool ledger와 trajectory는 포함하지 않는다.

`AnswerPresentation`은 typed trip/facility result에서 직접 만들며 canonical message를 파싱하지 않는다. facility detail은 유형별 최대 5개로 제한하고 `total_count`, `truncated`를 함께 준다. `facility_id`, `raw_status_text`는 제외한다. evidence source는 `public_api | cache`의 `display_name`, `checked_at`, `success`만 포함하고 내부 source name, note, fixture와 internal metadata는 제거한다. stateless 질문 응답은 presentation과 clarification을 제공할 수 있지만 thread context snapshot은 제공하지 않는다.

Assistant의 다중 턴 trip·facility 질문은 thread/run API를 사용한다. stateless `/v1/accessibility/questions`는 one-shot 호환 surface이며 `run_id`, context snapshot과 resume을 제공하지 않는다. stateless clarification은 같은 run으로 이어가지 않고 전체 질문을 보완해 다시 제출해야 한다.

## 6. 사용자 workspace

### 6.1 데스크톱

Phase 3C-1부터 Assistant는 API 상태 조합을 유지하면서 다음 상태 적응형 layout을 사용한다.

#### 질문 전

- 최대 880px의 단일 중심 column
- page heading, 현재 지원 범위, 자연어 composer와 실제 가능한 예시
- readiness가 정상일 때 별도 service card를 렌더링하지 않음
- 최근 결과나 `판단 대기 중` placeholder를 렌더링하지 않음
- thread는 첫 질문 제출 시 계속 lazy-create

#### clarification 중

- 최대 960px의 단일 작업 column
- server가 확인한 `ConversationContextView`를 `확인한 이동 조건`으로 먼저 표시
- 짧은 transcript, Agent가 제공한 prompt와 하나의 active clarification form
- active clarification 동안 일반 composer를 숨기거나 잠긴 이유를 명시
- `answer_request=fields`이면 서버가 제공한 `inputs[]`로 native control을 렌더링
- `answer_request=single_text`이면 모든 prompt와 하나의 명시적 textarea를 렌더링

여러 control은 frontend가 prompt를 해석해서 만들지 않는다. field control, label, option과 constraints가
불완전하거나 허용되지 않은 조합이면 임의 form으로 보정하지 않고 contract 오류로 처리한다.

#### 결과 후

- 12-column grid, 결과 본문 8-column과 공개 근거 4-column
- 결과 본문: 상태, canonical message, 구조화 presentation, 후속 action
- 공개 근거: 확인 시각, evidence, 제한·미확인 항목과 request ID disclosure
- 운영 timeline이나 Tool 이름은 이 panel에 표시하지 않음

`ResultSummary`는 DOM에 한 번만 두고 관련 Agent 응답 다음에 위치시킨다. desktop에서는 CSS
grid로 결과와 근거를 나눠도 mobile에서는 같은 DOM 순서를 유지한다. 대화 안에 두 번째 결과를
복제하지 않으며 필요한 경우 `확인 결과로 이동` 링크만 제공한다.

최대 제품 content width는 token의 1440px를 유지하되 결과 본문의 문장 폭은
`readingMaxWidth=720px`로 제어한다. 입력창은
viewport 아래에 고정할 수 있지만 200% 확대와 mobile reflow에서는 일반 문서 흐름으로 전환한다.

### 6.2 모바일

한 열에서 다음 순서를 유지한다.

1. header와 현재 route
2. 페이지 제목, 지원 범위와 새 조회
3. 현재 조건 요약
4. 대화와 clarification 또는 일반 입력창
5. 결과 카드
6. 근거·제한 사항 disclosure
7. 이어서 질문·새 이동 조회 action

결과와 근거를 bottom sheet에만 가두지 않는다. sheet는 빠른 이동용 보조 view로만 사용할 수 있다.
운영 Console 정보는 Assistant mobile DOM에 삽입하지 않는다.

### 6.3 Visual language

방향은 장식적인 AI chat보다 차분하고 정밀한 이동 서비스와 운영 제품에 가깝게 잡는다.
승인된 시각 방향의 이름은 `Modern Mobility`다. 생성형 디자인 결과물은 기능·정보 구조의
source가 아니라 시각 참고 자료로만 사용하며, 실제 제품 계약과 접근성 기준을 우선한다.

- cool neutral canvas와 white surface, deep indigo primary를 사용한다.
- restrained teal은 이동 맥락의 보조 accent와 성공 상태에 제한적으로 사용한다.
- 확인, 부분 확인, 실패와 미확인은 green·amber·red·slate를 사용하되 text와 icon을 항상 병기한다.
- 한국어 본문은 `Pretendard Variable`과 system fallback, 기본 16px·line-height 1.6을 사용한다.
- 대화는 짧은 발화만 message 형태로 표시하고 긴 결과는 heading이 있는 document card로 렌더링한다.
- station과 구간은 지도보다 먼저 text와 연결선으로 표현하되 구조화된 route segment가 없으면 장식용 경로를 만들지 않는다.
- 사용자 화면은 넉넉한 spacing을 유지하고 운영 Console의 고밀도 표·timeline visual language를 가져오지 않는다.
- 주요 card radius는 16px, control radius는 14px로 두고 shadow는 낮게 유지해 정보 hierarchy를 border와 spacing으로 먼저 만든다.
- neon gradient, glassmorphism, typing animation, mascot과 자동 타이핑 효과를 사용하지 않는다.
- map은 구조화된 route 계약 이후 선택적 보조 view로만 추가한다.
- 초기에는 dark theme보다 forced-colors와 별도 high-contrast theme를 우선한다.

구체 값은 [design-tokens.json](design-tokens.json)을 단일 기준으로 사용한다.

## 7. API 상태와 화면 상태

| `run_status` | `status` | 화면 제목 | 기본 행동 |
|---|---|---|---|
| `interrupted` | `NEEDS_CLARIFICATION` | 추가 정보가 필요합니다 | 질문·선택지를 표시하고 같은 run을 resume |
| `completed` | `SUCCESS` | 확인이 완료되었습니다 | canonical `user_message`와 근거 표시 |
| `completed` | `PARTIAL` | 일부 정보만 확인되었습니다 | 확인 정보와 미확인 항목을 같은 중요도로 표시 |
| `completed` | `FAILED` | 현재 확인하지 못했습니다 | 결과를 확정하지 않고 새 조회 action 제공 |
| `failed` | `FAILED` | 조회가 중단되었습니다 | 같은 run resume을 제공하지 않고 새 run 제출 |

`SUCCESS`를 자동으로 `이동 가능`으로 번역하지 않는다. 화면의 주요 답변은 API의 canonical `user_message`다.

`completed + FAILED`도 presentation 유무를 나눈다. MCP가 구조화 결과를 반환한 업무 실패는 trip/facility presentation을 참고 정보로 표시하되 성공 판단처럼 강조하지 않는다. capability 부재 등 Agent가 직접 만든 실패는 `presentation=null`일 수 있으므로 canonical message와 복구 action만 표시한다. 두 경우 모두 technical `run_status=failed`와 다른 상태다.

### 7.1 결과 정보 순서

1. 업무 상태명과 canonical `user_message`
2. safety notice와 사용자가 취할 다음 행동
3. 구조화된 경로·시설 결과와 확인된 부분
4. 판단에 사용된 현재 이동 조건과 provenance
5. 위험·신뢰 수준과 공개 evidence
6. 제한 사항, 검증하지 못한 부분과 실패한 정보 출처
7. 마지막 확인 시각
8. `문제 신고용 정보` 안의 request ID

저장된 public event가 생기기 전에는 처리 단계 section을 추가하지 않는다. 이후에도 처리 내역은
실제 coarse event만 결과와 한계 사이에 표시하고 Tool·graph·model 진단은 `/ops`에 둔다.

`failed_sources`, `limitations`, `unverified_parts`가 비어 있을 때 빈 section을 렌더링하지 않는다.

Phase 2C의 presentation union은 trip의 출발지·목적지·route summary와 facility의 역·질문 종류·유형별 answer state·개별 status를 구분해 제공한다. 결과 패널은 이 구조와 canonical `user_message`, `available_partial_info`, `evidence`를 직접 사용하고 문장에서 누락 필드를 추출하지 않는다. 내부 source name, note와 비공개 원본은 화면에 표시하지 않는다.

### 7.2 시설 결과 정보 순서

facility presentation은 다음 세 상태 층을 합치지 않는다.

1. 조회 완결성: top-level `SUCCESS | PARTIAL | FAILED`
2. 시설 유형 판단: `AVAILABLE | MIXED | MAINTENANCE | UNAVAILABLE | NOT_CONFIRMED | UNKNOWN`
3. 개별 시설 상태: `AVAILABLE | MAINTENANCE | UNAVAILABLE | UNKNOWN`

`SUCCESS`를 `AVAILABLE`로 번역하지 않는다. 빈 detail 배열도 `시설 없음`으로 바꾸지 않으며 MCP의 `NOT_FOUND`는 공개 projection에서 `NOT_CONFIRMED`로 표시한다. `PARTIAL`에서는 확인된 유형과 확인하지 못한 유형을 모두 보여주고 trip 전용 risk/confidence section은 렌더링하지 않는다.

시설 결과 순서는 canonical `user_message` → 조회 완결성 → 역·호선과 질문 종류 → 유형별 answer state → 개별 상태·위치 → 제한·미확인 정보 → 확인 시각·safety notice다.

### 7.3 clarification 패턴

- 질문은 heading으로 제공한다.
- `answer_request.mode=fields`이면 `prompt_id`별로 control을 연결하고 각 input `id`를 unique form key로 사용한다.
- `control=text`이면 서버 min/max를 적용한 명시적 label의 native text control을 사용한다.
- `control=tri_state`이면 `yes | no | unknown`을 서버 option label과 함께 native radio group으로 제공한다.
- `control=single_choice`이면 opaque option value와 서버 label을 사용하는 native radio group을 제공한다.
- `answer_request.mode=single_text`이면 모든 prompt를 먼저 읽을 수 있게 표시하고 하나의 명시적 label을 가진 textarea를 사용한다.
- 선택 즉시 제출하지 않고 `입력한 정보로 계속` 버튼을 둔다.
- submit payload에는 현재 `clarification.id`를 포함한다.
- field가 하나여도 v2 `fields.values[]`를 사용하고 item에는 `input_id`와 tagged value만 담으며 required field 전체를 한 번에 제출한다.
- 누락·중복·추가·kind/control·option 422는 관련 `input_id`에 오류를 연결하고 모든 draft를 보존한다.
- stale 409는 최신 run을 한 번 조회하되 draft를 자동으로 새 clarification에 적용하지 않는다.
- active clarification이 있는 동안 일반 질문 composer는 숨기거나 disabled 사유를 명시하고 clarification form만 제출 가능하게 한다.
- default selection과 추정값은 사용하지 않는다.
- 안전 필수 field의 `unknown`은 이용 불가능으로 번역하지 않고 판단 불가 업무 결과로 이어진다.

### 7.4 null과 empty 상태

- `presentation=null`은 canonical message만 있는 유효한 Agent 응답일 수 있다.
- context location의 `provided`를 resolved station으로 표현하지 않고, active domain context가 비면 transcript에서 조건을 추출하지 않는다.
- facility detail이 비고 answer state가 `NOT_CONFIRMED | UNKNOWN`이면 `시설 없음`으로 번역하지 않는다.
- `evidence_sources`, `confidence_reasons`, `available_partial_info`가 비면 빈 section을 렌더링하지 않는다.
- nullable line, location, inside-gate, open-time, emergency-bell과 확인 시각은 값이 있을 때만 표시한다.
- 빈 문자열인 safety notice나 route summary를 임의 문구로 보충하지 않고 canonical message를 유지한다.

## 8. 오류와 복구

| 오류 범주 | 사용자 행동 |
|---|---|
| 입력 검증 422 | 입력 위치에 오류를 연결하고 focus 이동; active run 유지 |
| thread/run 404 | 만료 안내 후 새 thread 생성 |
| thread busy 409 | 먼저 현재 clarification을 완료하도록 안내 |
| run not resumable 409 | 같은 run resume 제거, 입력을 보존해 새 run 시작 제공 |
| stale clarification 409 | draft 보존, 최신 run GET, 자동 재제출 금지 |
| budget 429 | 질문 범위를 줄이고 새 run 또는 새 thread로 제출 |
| capacity 429 | 불필요한 thread 정리 안내; `Retry-After`가 있을 때만 재시도 시각 표시 |
| MCP 502/503/504 | 결과를 추정하지 않고 새 조회 action과 request ID 제공 |
| 전송 결과를 모르는 network disconnect | 자동 재제출 금지, 상태 확인 또는 새 thread 복구 안내 |
| 예상하지 못한 500 | 일반 오류 메시지와 request ID 제공 |

`retryable=true`여도 mutation을 자동 반복 호출하지 않는다. 명시적인 start 오류는 새 start 요청으로 재시도할 수 있지만 응답을 받지 못한 network disconnect는 성공 여부를 알 수 없어 자동 재제출하지 않는다. resume의 교정 가능한 422만 기존 run과 clarification form을 그대로 유지한다. 명시적인 non-422 응답은 해당 resume 작업이 거절되거나 실패한 것으로 다루되 아래 상태 조회로 현재 run을 조정하고, transport disconnect는 실행 결과 자체가 불명확한 별도 상태로 다룬다.

resume 오류 envelope에는 갱신된 `run_status`가 없다. 명시적인 non-422 HTTP 응답 뒤에는 `GET /v1/runs/{run_id}`를 한 번 호출해 terminal 상태를 조정한다. transport disconnect 뒤 즉시 조회한 `interrupted`는 resume 요청 전 checkpoint일 수 있으므로 재개 가능하다는 증거로 사용하지 않는다. 이 경우 조회한 `completed`와 `failed`만 확정 상태로 반영하며, `interrupted` 또는 조회 실패는 `결과를 확인할 수 없음`으로 표시하고 같은 run의 resume을 잠근다. 기본 복구는 입력 draft를 보존한 새 thread이며 blind retry는 하지 않는다. start 요청은 성공 응답 전 run ID가 없어 같은 방식으로 복구할 수 없고, `THREAD_BUSY`가 이어지면 현재 입력을 보존하고 새 thread 생성을 안내한다.

run 오류 화면 분기는 번역될 수 있는 `error.message` 문자열이 아니라 안정적인 `error.code`를 사용한다. Browser의 상태 화면과 capability gate는 raw `/health/ready`를 받지 않고 BFF가 200·503을 모두 검증해 만든 `PublicServiceStatus`만 사용한다.

현재 OpenAPI 입력 DTO의 최대 길이는 20,000자지만 기본 runtime limit은 2,000자다. UI는 현재 배포 capability가 공개되기 전까지 2,000자 counter를 사용하고 서버 422를 최종 기준으로 삼는다. 장기적으로는 API schema와 runtime limit을 일치시키거나 capability endpoint를 제공한다.

### 8.1 문맥을 유지하는 action과 초기화 action

- `이어서 질문`: completed run 뒤 같은 thread에 새 run을 제출하며 이전 trip·mobility context를 유지한다.
- `이전 문맥으로 다시 조회`: `ConversationContextView`에 표시된 같은 thread context를 재사용한다는 설명과 확인 뒤에만 제공한다. 사용자가 확인하지 않은 조건은 재사용한다고 표시하지 않는다.
- `새 이동 조회`: 새 thread를 먼저 생성해 trip·mobility context를 완전히 초기화하고 active ID를 전환한다. 그 뒤 이전 thread를 best-effort 삭제한다. 현재는 BFF server session과 TTL cleanup이 없으므로 삭제 실패 ID의 refresh 이후 복구를 보장하지 않는다. Phase 4+에서는 실패한 ID를 server session cleanup 대상으로 보존한다. 이전 thread 삭제 실패가 새 조회 시작을 막아서는 안 되며 browser unload 삭제에 의존하지 않는다.

resume terminal 실패의 기본 복구는 `조건을 초기화하고 새 이동 조회`다. 직전 입력을 draft로 복사할 수 있지만 자동 제출하지 않으며 이전 clarification 답변만 보내지 않는다.

## 9. 접근성 기준

기본 release 기준은 WCAG 2.2 AA다.

- semantic HTML과 landmark를 우선하고 ARIA는 native semantics로 표현할 수 없을 때만 사용한다.
- document `lang="ko"`, 고유한 page title, `header`·`main`·`aside`와 본문 skip link를 제공한다.
- 모든 기능을 keyboard만으로 수행할 수 있어야 한다.
- focus order는 시각적 순서와 일치하며 sticky UI에 가려지지 않는다.
- loading·완료 상태는 `aria-live="polite"`, 즉시 조치가 필요한 오류는 `role="alert"`를 사용한다.
- 일반 Agent 응답은 짧게 알리되 focus를 강제로 이동하지 않는다. clarification이 시작될 때만 `tabindex="-1"`인 clarification heading으로 focus를 옮긴다.
- 사용자가 이전 내용을 읽는 중이면 자동 scroll하지 않고 `새 답변으로 이동` action을 제공한다.
- textarea의 Enter는 줄바꿈으로 유지한다. 전송 shortcut을 제공하면 화면에 안내하고 한국어 IME `isComposing` 중에는 실행하지 않는다.
- `aria-busy`는 갱신되는 결과 section에만 적용하고 전체 application navigation을 잠그지 않는다.
- body text는 4.5:1 이상, large text와 UI boundary는 해당 WCAG 기준을 만족한다.
- 의미 있는 상태는 색상 외에 text와 icon으로 중복 표현한다.
- interactive control 경계에는 `border.control`만 사용하고 `border.subtle`은 장식과 section divider로 제한한다.
- visible focus를 모든 interactive element에 유지하며 `outline: none`만으로 제거하지 않는다. brand surface에서는 별도 고대비 focus token을 사용한다.
- 최소 target은 내부 기준 44×44 CSS px로 잡는다.
- 200% text zoom과 320 CSS px에 해당하는 400% browser zoom에서 핵심 기능과 reading order를 유지한다.
- WCAG text-spacing override와 portrait·landscape orientation 모두에서 정보와 action이 손실되지 않는다.
- `prefers-reduced-motion`에서는 skeleton shimmer, panel transition과 자동 scroll animation을 제거한다.
- Windows High Contrast와 forced-colors mode를 검사한다.
- 날짜는 기계 판독 가능한 `time` 요소와 현지화된 표시를 함께 사용한다.

근거 문서:

- [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
- [ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/)
- [WCAG 2.2 Use of Color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color)

## 10. 개인정보와 신뢰

- 화면에는 원본 MCP payload, 내부 chain-of-thought와 secret을 표시하지 않는다.
- `failed_sources.source_name`은 stable public code이고 `display_name`은 bounded 제품 표시명이다.
  MCP 내부 이름과 reason은 Agent response를 만들기 전에 제거하며 UI 은닉에 의존하지 않는다.
- `ErrorResponse` v2는 빈 detail 또는 bounded request-validation `issues`만 허용한다. Web BFF도
  strict 검증 뒤 오류 객체를 다시 직렬화해 upstream 원문 bytes를 Browser로 전달하지 않는다.
- failed source는 Agent가 소유한 stable public code와 표시명만 제공하고 connector·Tool의
  내부 이름을 전달하지 않는다.
- 오류 detail은 error code별 공개 schema로 제한하며 임의 `AgentError.context` dictionary를
  response에 복사하지 않는다.
- 운영 Console의 state는 allowlist projection만 제공하고 개인 이동 조건은 기본 masking한다.
- ephemeral 환경에서는 개발용 banner로 재시작 시 대화가 사라질 수 있음을 표시한다.
- hosted 환경의 장기 기억은 기본 비활성화하고 동의·조회·삭제 UI를 함께 제공할 때만 켠다.
- 오류 화면에는 stack trace 대신 request ID를 제공한다.
- 같은 저장소의 `web/`은 CORS가 없는 Agent API를 browser에서 직접 호출하지 않고 same-origin BFF를 사용한다.
- 통합 Compose의 local/demo 경로는 signed anonymous session, BFF shared-token과 owner-aware thread/run 검사를 적용한다. native 기본값은 한 local owner와 memory persistence를 사용할 수 있다. 익명 session 격리는 사용자 인증이 아니므로 hosted UI는 delegated OIDC token 검증과 인증 subject 기반 owner 전환 이후 활성화한다.
- staging/production Web 설정은 정확한 HTTPS `WEB_PUBLIC_ORIGIN`과 mutation Origin을 확인한다. 내부 Agent HTTP는 private network가 별도로 보호된 경우에만 `AGENT_ALLOW_PRIVATE_HTTP=true`로 허용하며, 이 hardening은 end-user 인증을 대체하지 않는다.

## 11. UI release 순서

1. 완료: 동일 저장소 `web/`, schema v3 BFF, Assistant vertical slice와 design tokens
2. 완료: context, clarification, trip/facility 결과·근거와 오류·복구 상태
3. 완료: Phase 3C-0 failed source·오류 public projection hardening
4. 완료: Phase 3C-1 고정 3열을 empty·clarification·result 상태 적응형 workspace로 교체
5. 완료: Phase 3C-2 `/`, `/how-it-works`, `/status`와 `/accessibility` 제품 route 추가
6. 진행 중: Phase 3C-3 structured clarification 계약 승인과 runtime·BFF·Web 구현
7. 완료: Phase 3C-4 `Modern Mobility` 토큰과 상태 적응형 Assistant 시각 체계 통합
8. PostgreSQL run/event ledger, owner-scoped public processing projection 검토
9. 인증된 thread history와 refresh 복원
10. 저장된 event 기반 사용자 진행 상태와 SSE
11. 인증·redaction·audit가 적용된 Agent Console

## 12. 완료 조건

- 모든 API 상태 조합이 명시된 UI 상태로 매핑된다.
- frontend가 `user_message`를 파싱해 도메인 값을 만들지 않는다.
- keyboard, screen reader, zoom, reduced motion과 forced colors 검증이 자동·수동 테스트에 포함된다.
- mobile에서 desktop의 핵심 정보가 사라지지 않는다.
- `PARTIAL`, `UNKNOWN`, `FAILED`를 성공 상태처럼 표현하지 않는다.
- 현재 API에 없는 history, progress, map과 운영 데이터는 가짜 fixture를 production behavior로 노출하지 않는다.
- 질문 전에는 빈 결과 panel을 렌더링하지 않고 결과 후에만 결과·공개 근거 layout으로 확장한다.
- public navigation과 Assistant가 internal ops DTO나 진단 route에 의존하지 않는다.
- 공개 문서는 문제, 현재 동작, 제약, trade-off, 검증 방법과 한계를 중심으로 유지한다.
