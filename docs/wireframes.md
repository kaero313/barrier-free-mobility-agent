# Barrier-Free Mobility UI Wireframes

## 1. 사용 원칙

이 문서의 wireframe은 layout과 information priority를 고정한다. 시각 스타일의 최종 모양보다 keyboard reading order, API 상태와 현재 구현 범위를 우선한다.

- Phase 2C Assistant는 trip을 기본으로 한다. facility의 새 진입점은 readiness 전체와 MCP dependency가 ready이고 capability가 있을 때만 표시한다.
- 결과 패널은 canonical `user_message`, schema v3 `context`, trip/facility `presentation`과 공개 evidence만 사용한다.
- 현재 조건은 run별 immutable context로 표시한다. 최근 대화 복원, 실제 progress event와 운영 timeline은 해당 API가 준비된 뒤 활성화한다.
- 데스크톱 column은 mobile DOM 순서를 바꾸지 않는 CSS grid로 구현한다.

동일 저장소의 `web/`이 이 화면과 same-origin BFF를 제공하고 `src/barrier_free_mobility_agent/`의 공개 API를 소비한다. MCP만 별도 독립 프로젝트다.

### Capability gate

```text
checking                  시설 진입점을 아직 표시하지 않음
ready + trip only         이동 경로 접근성 범위와 예시만 표시
ready + facility          시설 예시와 새 시설 질문 action 표시
not ready / fetch error   연결 상태 안내, 과거 facility 결과는 유지
```

Capability 배열만 보지 않고 `readiness.status=ready`, `mobility_mcp.status=ready`, `facility_question`을 모두 확인한다. gate는 새 진입점에만 적용한다.

## 2. WF-A01 Assistant desktop — 질문 전

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ [본문으로]  Barrier-Free Mobility   이동 조회  작동 방식  서비스 상태       │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│                    이동 가능한 경로를 확인해 보세요                          │
│        출발지, 목적지와 이동 조건을 자연어로 알려주세요.                    │
│                                                                              │
│        질문                                                                  │
│        ┌──────────────────────────────────────────────────────────┐          │
│        │ 휠체어로 강남역에서 성수역까지 이동할 수 있을까?        │          │
│        └──────────────────────────────────────────────────────────┘          │
│        최대 2,000자                                      [조회 시작]         │
│                                                                              │
│        예시: [휠체어 이동] [계단 이용 불가] [에스컬레이터 이용 가능]         │
│                                                                              │
│        확인할 수 있는 범위 · 정보의 한계                                    │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

질문 전에는 thread, 결과 panel과 정상 readiness card를 만들지 않는다. 예시는 현재 capability로 실제
처리 가능한 질문만 사용한다. 첫 명시적 제출에서 thread를 lazy-create한다.

### WF-A01R Assistant desktop — 결과 후

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Barrier-Free Mobility        이동 조회  작동 방식  서비스 상태   [새 조회] │
├──────────────────────────────────────────────────────────────────────────────┤
│ 확인한 이동 조건: 강남역 → 성수역 · 휠체어 · 계단 이용 불가                 │
├───────────────────────────────────────────────┬──────────────────────────────┤
│ 확인 결과                                      │ 판단 근거와 정보 한계        │
│ [일부 정보만 확인됨]                           │                              │
│                                               │ 확인 시각 <time>             │
│ canonical user_message                        │ 공개 evidence source         │
│                                               │ 제한 사항                    │
│ 출발지 · 목적지 · route summary               │ 확인하지 못한 부분           │
│ 확인된 부분 · 주의 사항                       │ 실패한 정보 출처             │
│                                               │                              │
│ [이어서 질문] [새 이동 조회]                  │ <details>문제 신고용 정보    │
├───────────────────────────────────────────────┴──────────────────────────────┤
│ 이전 질문과 짧은 대화                                                  [펼침]│
└──────────────────────────────────────────────────────────────────────────────┘
```

`ConversationContextView`가 있을 때만 `출발역 입력됨/확인됨`, `도착역 입력됨/확인됨`, 확인된
mobility condition과 provenance를 표시한다. context가 비어 있으면 transcript를 파싱해 채우지
않고 section을 생략한다. 오른쪽은 public schema가 제공하는 근거만 사용하며 Tool timeline이나
운영 지표를 표시하지 않는다.

### DOM과 focus 순서

1. skip link
2. header controls
3. page heading, scope description와 질문 입력 또는 `새 이동 조회`
4. conversation list와 관련 Agent 응답 뒤의 단일 result summary
5. active clarification
6. composer
7. public evidence disclosure
8. 대화 관리 영역의 thread 삭제 action

시각적으로 오른쪽에 놓인 결과도 DOM에서는 관련 Agent 응답 다음에 위치한다. CSS grid area로만 배치한다.

## 3. WF-A02 Assistant mobile

```text
┌──────────────────────────────┐
│ Barrier-Free Mobility   [메뉴]│
├──────────────────────────────┤
│ 이동 가능한 경로를            │
│ 확인해 보세요                 │
│                              │
│ 질문                          │
│ [                           ]│
│ 최대 2,000자                  │
│ [조회 시작]                   │
│                              │
│ 확인할 수 있는 범위           │
└──────────────────────────────┘

결과 후 같은 DOM은 다음 순서로 확장한다.

```text
┌──────────────────────────────┐
│ Barrier-Free Mobility   [메뉴]│
├──────────────────────────────┤
│ 확인한 이동 조건              │
│ 강남역 → 성수역 · 휠체어      │
├──────────────────────────────┤
│ 확인 결과                     │
│ [일부 정보만 확인됨]          │
│ canonical user_message       │
│                              │
│ route summary와 주의 사항     │
│                              │
│ <details>판단 근거와 정보 한계│
├──────────────────────────────┤
│ [이어서 질문]                 │
│ [새 이동 조회]                │
└──────────────────────────────┘
```

- composer는 확대 시 fixed positioning을 해제한다.
- safe-area inset을 적용하되 focus된 control을 가리지 않는다.
- 결과를 bottom sheet에서만 제공하지 않는다.
- active clarification 중에는 일반 질문 composer를 숨기거나 `추가 질문에 먼저 답해 주세요` 사유와 함께 비활성화한다. clarification form과 일반 composer를 동시에 제출 가능하게 두지 않는다.
- 운영 timeline과 진단 정보는 mobile Assistant DOM에 삽입하지 않는다.

## 4. WF-A03 structured 이동 조건 clarification

```text
┌──────────────────────────────────────────┐
│ 추가 정보가 필요합니다                   │
│                                          │
│ 출발역은 어디인가요?                     │
│ 출발역 [                              ]  │
│                                          │
│ 도착역은 어디인가요?                     │
│ 도착역 [                              ]  │
│                                          │
│ 계단을 이용할 수 있나요?                 │
│ ( ) 가능  ( ) 불가능  ( ) 잘 모르겠음   │
│                                          │
│ 에스컬레이터를 이용할 수 있나요?         │
│ ( ) 가능  ( ) 불가능  ( ) 잘 모르겠음   │
│                                          │
│ [입력한 정보로 계속]                     │
└──────────────────────────────────────────┘
```

구현 계약:

- v4 `answer_request.mode=fields`일 때만 structured control을 렌더링한다.
- `prompt_id`로 질문과 control을 연결하고 input `id`를 label, error와 제출 값의 unique key로 사용한다.
- `control`, field, label, option과 constraints는 서버 값을 사용하며 prompt 문구에서 만들지 않는다.
- field가 하나여도 tagged resume의 `values[]`를 사용한다.
- 각 value item은 `input_id`와 `{kind, value}`만 담고 field와 control을 중복 전송하지 않는다.
- 모든 required input을 한 번에 제출하며 누락·중복·추가·kind/control·option 422는 전체 draft를 보존한다.
- 각 radio group은 `<fieldset>`과 `<legend>`를 사용하고 초기 선택을 두지 않는다.
- `unknown`을 false나 이용 불가능으로 번역하지 않는다. 안전 필수 조건이면 판단 불가 결과로 이동한다.
- active clarification 동안 일반 composer를 별도로 제출할 수 없다.

## 4.1 WF-A03A 역 후보 clarification

```text
┌──────────────────────────────────────────┐
│ 추가 정보가 필요합니다                   │
│                                          │
│ 어느 시청역인가요?                       │
│                                          │
│ ( ) 시청역 · 1호선                       │
│ ( ) 시청역 · 2호선                       │
│                                          │
│ [선택한 역으로 계속]                     │
└──────────────────────────────────────────┘
```

구현 계약:

- `<fieldset>`과 `<legend>`를 사용한다.
- v4 `answer_request.mode=fields`, `control=single_choice`, 연결된 prompt와 non-empty options 조합인지
  먼저 검증하고 아니면 contract 오류로 처리한다.
- 화면에는 option `label`만 표시하고 payload에는 현재 clarification에 묶인 opaque `value`를 사용한다.
- resume v2 payload에는 현재 `clarification.id`와 `{input_id, value: {kind: choice, value}}`를 넣고
  stable field와 control은 반복하지 않는다.
- raw MCP station ID와 candidate 원본은 DOM이나 payload에 노출하지 않는다.
- Agent application validation 뒤 graph station node가 `option-N`을 checkpoint candidate order에 직접 매핑한다.
- 첫 option을 선택하지 않는다.
- 제출 시 선택이 없으면 group에 연결된 inline error를 남긴다.
- radio 이동은 native keyboard behavior를 유지한다.

호환 기간의 v3 response는 기존 `control=choice`와 natural-language option value를 유지한다.

## 5. WF-A04 결과 상태

### SUCCESS

```text
[확인 완료] [위험 수준: 주의 필요]
canonical user_message

마지막 확인  2026-07-10 15:30
[근거와 제한 사항 펼치기]
```

`last_checked_at`이 `null`이면 마지막 확인 row 전체를 생략한다. browser 현재 시각으로 대체하지 않는다.

### PARTIAL

```text
[일부 정보만 확인됨] [위험 수준: 확인 불가]
canonical user_message

확인된 정보
• available_partial_info

제한 사항
• limitations

확인하지 못한 부분
• unverified_parts
```

### interrupted

```text
[추가 정보 필요]
user_message

clarification.prompts
[choice radio group 또는 single-text 자유 입력]
```

### facility result

```text
[조회 완료: SUCCESS]      ← 조회 완결성
canonical user_message

강남역 · 2호선
엘리베이터  [MIXED]       ← 유형별 판단
• [AVAILABLE] 1번 출구, 개찰구 밖
• [MAINTENANCE] 3번 출구

장애인 화장실 [NOT_CONFIRMED]
현재 공공데이터에서 확인되지 않았습니다.

[제한·확인 시각·출발 전 안내 펼치기]
```

top-level `SUCCESS`를 시설 `AVAILABLE` badge로 사용하지 않는다. 서로 다른 child status가 있으면 유형별 answer state는 `MIXED`다. detail이 5개를 넘으면 현재 API로는 `전체 N개 중 5개 표시`만 제공하며 pagination이나 detail endpoint가 없으므로 `전체 보기` action을 만들지 않는다. 빈 목록은 `시설 없음`으로 표시하지 않는다. capability gate는 새 시설 진입점만 제어하고 이미 받은 facility card는 유지한다.

### completed + FAILED

```text
[현재 확인하지 못함]
canonical user_message

Agent 실행은 끝났지만 이동 판단 결과를 제공하지 못했습니다.
[질문을 고쳐 이어서 질문]
[조건을 초기화하고 새 이동 조회]
```

이 상태는 `run_status=failed`와 다르다. 같은 run을 resume하지 않지만 같은 thread의 후속 질문은 가능하다.

- `presentation`이 있으면 trip/facility 구조화 값은 `참고 정보`로 표시하되 성공 결과처럼 강조하지 않는다.
- `presentation=null`이면 canonical message와 복구 action만 표시하고 transcript에서 구조화 값을 만들지 않는다.

### failed run

```text
[조회가 중단되었습니다]
현재 run은 이어서 실행할 수 없습니다.

[조건을 초기화하고 새 이동 조회]
[대화 삭제]
```

상태 badge는 색상 외에 완전한 text를 포함한다. `risk_level`이나 `confidence_level`이 `null`이면 해당 badge를 생성하지 않는다.

## 6. WF-A05 오류

```text
┌──────────────────────────────────────────┐
│ 이동 정보를 확인하지 못했습니다          │
│ error.message                            │
│                                          │
│ [조건을 초기화하고 새 이동 조회]         │
│                                          │
│ ▸ 문제 신고용 정보                       │
│   요청 ID: abc... [복사]                  │
└──────────────────────────────────────────┘
```

- 오류 해결 action은 `error.code`, 요청 단계와 현재 `run_status`로 결정한다.
- stack trace와 내부 `detail`은 표시하지 않는다.
- 422는 panel보다 입력 control 가까이에 표시한다.
- `retryable=true`는 action 제공 조건이지 자동 retry 허가가 아니다.

## 7. WF-P01 제품 진입

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Barrier-Free Mobility        이동 조회  작동 방식  서비스 상태              │
├──────────────────────────────────────────────────────────────────────────────┤
│ 교통약자의 이동 가능성을 확인된 정보와 한계를 함께 안내합니다.             │
│                                                                              │
│ [이동 조회 시작]  [작동 방식 확인]                                          │
│                                                                              │
│ 안전한 판단 원칙                                                            │
│ ├─ 부족한 조건은 사용자에게 확인                                            │
│ ├─ 일부 실패와 미확인 정보를 숨기지 않음                                    │
│ └─ 정보 출처와 확인 시각 제공                                               │
│                                                                              │
│ 현재 지원 범위                                            [서비스 상태]      │
└──────────────────────────────────────────────────────────────────────────────┘
```

지원 범위는 readiness 한 번의 실패를 영구 capability처럼 저장하지 않는다. 제품 설명은 현재
활성화되지 않은 시설·대체경로 기능을 사용할 수 있는 것처럼 표현하지 않는다.

## 8. WF-P02 공개 서비스 상태

```text
┌──────────────────────────────────────────────────────────────┐
│ 서비스 상태                                                 │
│ [이동 조회 가능]                                            │
│                                                            │
│ Agent API            준비됨     확인 <time>                │
│ 이동 정보 서비스     준비됨     확인 <time>                │
│ 지원 범위            이동 경로 접근성                       │
│                                                            │
│ [이동 조회로 돌아가기]                                     │
└──────────────────────────────────────────────────────────────┘
```

환경 이름, 내부 URL, schema fingerprint, Tool 목록, 배포 revision과 오류 stack은 공개 상태에
포함하지 않는다. 상태 조회 자체가 실패하면 마지막 상태를 성공으로 추정하지 않는다.

## 9. WF-O01 Ops overview

이 화면은 내구 run repository와 `/internal/ops/v1` read model 이후 구현한다.

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Agent Console                  environment: staging      user: ops.viewer    │
├───────────────┬──────────────────────────────────────────────────────────────┤
│ Overview      │ Service health                                               │
│ Runs          │ [API ready] [MCP ready] [schema fingerprint]                 │
│ MCP & deps    │                                                              │
│ Evaluations   │ Last 24 hours                                                │
│ Releases      │ [success] [partial] [clarification] [failed]                 │
│               │                                                              │
│               │ Latency              Tool errors          Budget exceeded   │
│               │ p50 / p95            rate by capability  rate               │
│               │                                                              │
│               │ Recent failed runs                                           │
│               │ run id | intent | error code | duration | version           │
└───────────────┴──────────────────────────────────────────────────────────────┘
```

실제 사용자 질문과 정확한 위치는 overview에 표시하지 않는다.

## 10. WF-O02 Ops run detail

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ run 7f...  [실행: failed] [업무: FAILED]  duration 12.4s  flow mobility-v2  │
│ request 2a...  thread hash 91...  state schema 3                           │
├──────────────────────────────────────────────────────────────────────────────┤
│ [Overview] [Timeline] [State & policy] [Budget] [Tools] [Recovery]          │
├──────────────────────────────────────────────────────────────────────────────┤
│ Timeline                                                                     │
│ API        accepted ─────────────────────────────────────────── failed       │
│ Graph      route ─ project ─ reserve ─ resolve ─ reserve ─ generate          │
│ Tool                              resolve_station       generate_brief       │
│ Checkpoint      saved ───────────── saved ─────────────── saved               │
│ Recovery                                      interrupt/resume               │
├──────────────────────────────────────────────────────────────────────────────┤
│ Budget                    Tools                                               │
│ turn graph  8 / 12        resolve_station  success  120ms                    │
│ run graph  17 / 36        generate_brief  timeout  45s                       │
│ turn tools  3 / 5                                                            │
│ run tools   6 / 15                                                           │
└──────────────────────────────────────────────────────────────────────────────┘
```

Timeline은 저장된 event만 사용한다. 현재 `AgentState`의 trajectory를 public API로 직접 노출하지 않고 redacted, versioned ops projection을 사용한다.

## 11. Component inventory

| Component | 핵심 상태 |
|---|---|
| `AppShell` | desktop, mobile, zoomed, forced-colors |
| `ProductEntry` | trip-only, facility-ready, readiness unavailable |
| `PublicStatus` | checking, ready, degraded, unavailable, stale |
| `AssistantWorkspace` | empty-centered, clarifying-centered, result-split, failed |
| `ConversationList` | empty, user, agent, pending |
| `CapabilityGate` | checking, trip-only, facility-ready, not-ready, fetch-error |
| `Composer` | idle, composing IME, submitting with aria-disabled, invalid, hidden or explained during active clarification |
| `ConversationContextSummary` | empty, trip provided/resolved, facility, previous-context |
| `ClarificationPanel` | single text, multi text, tri-state, single-choice, atomic validation error, stale, resubmitting, contract-error |
| `RunStatus` | interrupted, completed, failed |
| `BusinessStatus` | success, partial, needs clarification, failed |
| `ResultSummary` | canonical message, presentation null, trip, facility |
| `TripPresentation` | route summary, safety notice, confidence reasons, empty optional fields |
| `FacilityPresentation` | answer-state variants, empty details, truncated without pagination |
| `EvidenceDisclosure` | risk/confidence nullable, partial, failed source |
| `ErrorPanel` | 404, 409, 422, 429, 502, 503, 504, 500, contract-error, transport-unknown |
| `RequestIdDisclosure` | hidden, expanded, copy success |
| `DevPersistenceNotice` | ephemeral only, production hidden |

각 component state를 Storybook과 axe 검사 대상으로 만든다.

## 12. 금지 패턴

- `SUCCESS`를 근거 없이 `이동 가능`으로 재작성
- 질문 문구를 frontend에서 파싱해 mobility boolean 생성
- structured field 일부만 checkpoint에 적용하거나 새 clarification에 draft 자동 replay
- `unknown`을 `no` 또는 이용 불가능으로 변환
- opaque station option 대신 raw MCP station ID 노출
- 실제 event가 없는 progress stepper와 퍼센트
- 색상만으로 위험·오류·선택 상태 표시
- map 안에만 존재하는 route 정보
- first radio option 자동 선택
- Agent 응답 전체를 assertive live region으로 낭독
- 새 메시지마다 focus 강제 이동
- 원본 MCP payload, prompt 또는 chain-of-thought 표시
- 질문 전 빈 결과 panel과 정상 readiness sidebar 고정 표시
- public route에서 internal ops DTO, Tool timeline이나 token ledger 재사용
