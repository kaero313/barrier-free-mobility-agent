# ADR-0009: 구조화된 clarification 공개 계약과 호환 전환

- 상태: Accepted
- 결정일: 2026-07-14

## 배경

현재 conversation response schema v3의 `ClarificationView`는 서버가 제공한 `choice` 또는
`free_text` control과 하나의 `single_text` 답변을 표현한다. 이 계약은 역 후보 하나를 고르거나
MCP가 요청한 설명을 자연어로 보완하는 경우에는 충분하다. 그러나 deterministic trip workflow는
출발지, 도착지, 계단 이용 가능 여부와 에스컬레이터 이용 가능 여부를 한 active clarification에서
함께 요청할 수 있다. Web이 여러 질문을 하나의 textarea로 받으면 사용자가 빠뜨린 값을 정확히
연결하기 어렵고, field별 오류를 제공할 수도 없다.

반대로 Web이 prompt 문구를 파싱해 field와 boolean을 추론하면 번역이나 문구 변경이 API 의미를
바꾸고 Agent의 판단 책임이 frontend로 이동한다. 특히 MCP 후속 질문의 field는 일부 경로에서
자연어 keyword로 보조 분류되므로 이를 곧바로 typed public contract로 승격해서는 안 된다.

기존 Web은 response schema v3를 strict하게 검증한다. nested object에 optional field만 추가해도
실질적인 breaking change이므로 response와 resume request의 version 전환, stale replay 및
validation 실패 시 checkpoint 보존 정책을 함께 결정해야 한다.

## 결정

### 1. v4 response와 `ClarificationView` v2

conversation response schema v4는 `ClarificationView` v2를 사용한다. v2는 prompt와 입력 계약을
분리하고 `answer_request`를 discriminator로 사용한다.

```text
ClarificationView v2
├─ schema_version: 2
├─ id
├─ prompts[]
│  ├─ id
│  ├─ question
│  └─ fields[]
└─ answer_request
   ├─ mode: single_text
   │  ├─ label
   │  └─ min_length, max_length
   └─ mode: fields
      └─ inputs[]
         ├─ id
         ├─ prompt_id
         ├─ field
         ├─ control: text | tri_state | single_choice
         ├─ label
         ├─ required
         ├─ text: min_length, max_length
         └─ tri_state/single_choice: options[]
```

`field`는 Agent가 소유하는 stable semantic ID다. `prompt_id`는 질문과 control을 연결하고,
input의 `id`는 현재 clarification 안에서 label, 오류와 제출 값을 정확히 연결하는 unique ID다.
Resume의 `input_id`는 이 `id`를 참조한다. Web은 이 값을 만들거나 prompt에서 추론하지 않는다.

초기 structured allowlist는 다음과 같다.

| clarification | field | control |
|---|---|---|
| 출발지 입력 | `origin` | `text` |
| 도착지 입력 | `destination` | `text` |
| 계단 이용 가능 여부 | `can_use_stairs` | `tri_state` |
| 에스컬레이터 이용 가능 여부 | `can_use_escalator` | `tri_state` |
| 출발역·도착역 후보 | `origin` 또는 `destination` | `single_choice` |

`tri_state` wire value는 `yes | no | unknown`이다. option label은 서버가 제공하고 기본 선택은
없다. `single_choice`의 option value는 현재 clarification에만 유효한 opaque token이다. MCP의
station ID나 candidate 원본을 공개하지 않는다.

MCP 후속 질문, `other`, facility 복합 field 또는 allowlist하지 않은 조합은 clarification 전체를
`single_text`로 projection한다. 한 form에서 structured field와 추론된 free text를 혼합하지 않는다.

### 2. Resume request v2

v2 body는 clarification ID와 tagged answer envelope를 사용한다.

```json
{
  "schema_version": "2",
  "clarification_id": "active-id",
  "answer": {
    "mode": "fields",
    "values": [
      {
        "input_id": "can_use_stairs",
        "value": {
          "kind": "tri_state",
          "value": "no"
        }
      }
    ]
  }
}
```

field answer는 active answer request가 이미 고정한 stable field와 control을 반복 전송하지 않는다.
`value.kind`는 `text | choice | tri_state` 중 하나이며 server가 input의 `control`과 대조한다.
`single_text`는 같은 envelope에서 `mode=single_text`와 하나의 문자열 `value`를 사용한다. v2의
`clarification_id`는 필수다. 기존 `{answer: string, clarification_id?: string}` body는 호환
기간 동안 병행 수용한다.

### 3. 단일·복수 answer 정책

- `fields` mode는 field가 하나여도 항상 `values[]`를 사용한다.
- 배열은 순서가 아니라 `input_id`로 대조한다.
- 초기 version의 모든 structured input은 required다.
- 한 요청에는 각 expected `input_id`가 정확히 한 번 있어야 한다.
- 누락, 중복, 추가 input, value kind와 input control 불일치 및 허용되지 않은 option은 요청 전체를 422로 거절한다.
- 여러 field는 하나의 atomic resume이며 부분 적용, server draft와 자동 보완을 제공하지 않는다.
- multi-select, optional field와 conditional field는 이번 계약에 포함하지 않는다.
- Web은 사용자의 draft를 보존할 수 있지만 서버가 일부 답을 checkpoint에 저장한 것처럼 표현하지
  않는다.

### 4. Validation, stale과 replay

구조·타입 및 active answer request와의 semantic validation 실패는 기존
`REQUEST_VALIDATION_ERROR` envelope를 사용한다. semantic issue는 stable `type`과
`location=(body, answer, values, input_id)`로 field에 연결한다.

```text
clarification_answer_missing
clarification_answer_duplicate
clarification_answer_unexpected
clarification_answer_type_mismatch
clarification_answer_invalid_option
clarification_answer_too_long
```

Agent는 thread lock 안에서 최신 run을 다시 읽고 다음 순서로 처리한다.

1. run이 resumable인지 확인한다.
2. 현재 clarification ID와 요청 ID를 비교한다.
3. ID가 다르면 answer 내용을 검사하거나 적용하지 않고 `409 STALE_CLARIFICATION`을 반환한다.
4. ID가 같을 때만 answer request와 values를 검증한다.
5. 모든 값이 유효할 때 graph resume을 정확히 한 번 실행한다.

422와 stale 409는 run, checkpoint와 active clarification을 변경하지 않는다. Web은 draft를 보존하고
409 뒤 최신 run을 GET으로 한 번 조회하지만 field ID가 같아도 자동 재제출하지 않는다. 첫 resume
뒤 같은 POST가 다시 도착하면 새 clarification에서는 stale, terminal run에서는 not-resumable로
거절한다. mutation idempotency와 blind retry는 이 단계에 추가하지 않는다.

### 5. `unknown` 안전 정책

계단 또는 에스컬레이터처럼 outbound mobility profile에 필요한 안전 field가 `unknown`이면 이를
`false`로 바꾸거나 같은 질문을 반복하지 않는다. Agent는 MCP를 호출하지 않고
`run_status=completed`, 업무 `status=FAILED`로 종료하며 확인하지 못한 조건 때문에 이동 가능성을
판단할 수 없음을 canonical message와 공개 limitation으로 알린다.

### 6. Typed Command와 graph state

Agent state schema는 3을 유지한다. application boundary는 lock 안에서 active
`StructuredClarificationView`와 structured answer를 대조한 뒤 검증된 tagged answer를 LangGraph
`Command(resume=...)` payload로 전달한다. 자연어 문장으로 합성하거나 deterministic pre-router로
다시 파싱하지 않는다.

Graph clarification node는 `origin`, `destination`, `can_use_stairs`, `can_use_escalator` 값을 기존
trip/mobility slot에 한 node update로 원자 적용한다. station selection node는 `option-N` token을
현재 checkpoint의 candidate 순서에 직접 매핑한다. option label, station ID 또는 자연어 matcher를
typed 경로의 authority로 사용하지 않는다.

안전 필수 `unknown`도 graph의 명시적인 terminal branch다. pending clarification을 제거하고 공개
업무 `FAILED` outcome을 state에 기록해 checkpoint를 정상 완료하며 MCP node로 진행하지 않는다.
422와 stale 409는 `Command` 호출 전에 끝나므로 graph state를 변경하지 않는다.

Structured resume이 graph topology와 interrupt payload 의미를 바꾸므로 production flow version은
`mobility-v2`로 올린다. serialized Agent state shape는 바뀌지 않아 state schema는 3을 유지한다.
기존 `mobility-v1` thread는 flow mismatch 정책에 따라 새 run 전에 거절하거나 drain하며, v2
checkpoint로 자동 변환하지 않는다.

### 7. schema v3 호환 기간

schema v3 object에 v2 field를 추가하지 않는다. 한 호환 기간 동안 다음 dual projection을 운영한다.

- `X-Agent-Response-Schema: 4`를 보내면 conversation response v4를 반환한다.
- header가 없으면 기존 response v3를 변경 없이 반환한다.
- 같은 저장소의 BFF는 내부 Agent 호출에 header를 추가하고 Browser에는 v4만 중계한다.
- v3 projection은 기존 `choice | free_text`, `single_text` 의미를 유지한다.
- resume endpoint는 legacy body와 v2 body를 모두 받는다.
- 다음 전환에서 기본 response를 v4로 바꾸고 명시적인 v3 요청 기간을 둔 뒤 v3를 제거한다.

schema 선택 header는 Browser가 임의로 Agent에 전달하는 사용자 입력이 아니라 BFF가 고정하는 내부
계약 설정이다. OpenAPI는 response version과 request union을 명시하며 생성 client와 contract drift
검사도 v4 경로를 검증한다.

## 결과

### 장점

- 여러 필수 조건을 field별 native control과 오류로 안전하게 받을 수 있다.
- frontend가 한국어 prompt를 해석하지 않아도 된다.
- structured answer가 자연어 parser를 우회해 typed slot에 원자 적용된다.
- stale·중복 resume이 checkpoint를 오염시키지 않는다.
- 기존 state schema와 v3 client를 한 호환 기간 유지할 수 있다.
- 구조화 근거가 약한 MCP 질문은 free text로 남겨 잘못된 type 승격을 막는다.

### 비용

- v3/v4 projection과 legacy/v2 request를 호환 기간 동안 함께 테스트해야 한다.
- BFF, OpenAPI generated client와 Web form을 같은 release에서 전환해야 한다.
- `mobility-v2` 배포에서 기존 v1 thread의 drain·거절 경계를 운영해야 한다.
- typed slot 적용과 candidate order mapping을 restart test로 유지해야 한다.

## 범위 제외

- multi-select, optional·conditional field와 부분 저장
- MCP가 직접 제공하는 동적 form schema
- state schema 4와 기존 v1 checkpoint의 자동 migration
- POST idempotency, SSE, run/event ledger와 `/ops`
- alternative workflow와 facility 전체 field의 구조화

## 검증 기준

- 출발지·도착지와 계단·에스컬레이터 복수 입력이 하나의 atomic resume으로 완료된다.
- 누락·중복·추가·kind/control·option 오류 뒤 같은 clarification ID와 checkpoint가 유지된다.
- stale 요청은 answer validation과 graph 호출 전에 409로 거절된다.
- `unknown` 안전 field는 MCP 호출 없이 업무 FAILED로 끝난다.
- `unknown` terminal branch는 pending state를 제거한 completed checkpoint를 남긴다.
- station option token은 raw station ID를 노출하지 않고 checkpoint candidate order에 직접 매핑된다.
- typed answer는 자연어 parser를 거치지 않고 trip/mobility slot에 원자 적용된다.
- flow `mobility-v2`, state schema 3 조합이 memory와 PostgreSQL restart에서 검증된다.
- 기본 v3 client와 BFF가 선택한 v4 client가 호환 기간에 함께 contract test를 통과한다.
- Web은 default selection 없이 keyboard, 한국어 IME, 320px reflow와 field별 오류 연결을 통과한다.
