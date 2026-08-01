# ADR-0004: MCP extension의 capability-gated 활성화

- 상태: Accepted
- 날짜: 2026-07-10
- 범위: optional MCP output extension과 Agent subgraph release

## Context

Agent의 기본 trip workflow는 CI에 고정된 MCP SHA의 공개 계약만으로 실행할 수 있다. facility 질문 결과는 더 최신 MCP 작업본에 존재하지만 현재 고정 SHA에는 `facility_result`가 없다. provider 작업본의 존재만 보고 Agent 기능을 항상 활성화하면 local에서는 통과하고 CI·배포에서는 실패하는 dependency drift가 생긴다.

`facility_result`의 존재만 확인하는 것도 충분하지 않다. UI와 Agent가 사용하는 parsed target, item answer state, 개별 facility status, evidence와 nullable field가 호환되지 않으면 schema validation이 요청 중간에 실패하거나 잘못된 상태를 표시할 수 있다.

## Decision

MCP 계약을 기본 capability와 optional extension capability로 나눈다.

1. startup은 trip에 필요한 Tool과 schema를 항상 검증한다.
2. `facility_result`가 없으면 MCP를 trip-only `READY`로 유지한다.
3. `facility_result`가 있으면 다음 전체 계약을 deep validate한다.
   - parsed facility target, line, type, question kind와 missing fields
   - nullable facility result와 question kind
   - item facility type, answer state와 child detail
   - child status, 위치·개찰구·운영시간·비상벨 nullable field
   - evidence source, failed source, 확인 시각과 closed enum
4. 검증을 모두 통과한 경우에만 snapshot과 readiness에 `facility_question`을 기록한다.
5. router가 facility intent를 찾더라도 capability가 없으면 MCP Tool을 호출하지 않는다.
6. capability 부재 run은 HTTP transport 성공과 business `FAILED`로 지원 한계를 명시한다.
7. Web은 BFF가 확인한 capability에 따라 facility 진입점과 예시를 표시한다.
8. provider extension을 일반 배포에 활성화하려면 MCP 변경을 먼저 커밋·검증한 뒤 Agent CI pin을 명시적으로 갱신한다.

capability는 Tool 이름만으로 결정하지 않는다. 동일 `answer_accessibility_question` Tool의 output extension도 소비하는 nested schema 전체를 통과해야 한다.

## Invariants

- capability가 없는 MCP에서도 trip readiness와 trip workflow는 유지된다.
- capability가 없을 때 facility Tool 호출 수는 0이다.
- provider enum 확장처럼 Agent DTO가 이해하지 못하는 변경은 capability 활성화를 차단한다.
- readiness capability와 실제 graph 분기는 같은 `McpContractSnapshot`을 기준으로 한다.
- facility 결과는 top-level 조회 완결성, 유형별 answer state와 개별 status를 구분한다.
- optional extension 실패를 LLM 추측이나 세부 Tool 빈 배열로 우회하지 않는다.

## Consequences

### Positive

- MCP와 Agent를 독립적으로 배포하면서도 미완성 extension의 조기 활성화를 막는다.
- 고정 dependency SHA와 local 작업본의 차이가 사용자 기능 drift로 이어지지 않는다.
- Web, readiness와 graph가 동일한 기능 가용성 계약을 사용한다.
- extension별 contract mutation test와 upgrade gate를 둘 수 있다.

### Cost

- output schema 검증 코드와 fixture가 더 깊어진다.
- capability별 UI와 API 상태 조합 테스트가 필요하다.
- provider 기능이 준비되어도 pin upgrade 전에는 일반 배포에서 보이지 않는다.

## Alternatives

### Facility extension을 즉시 필수 계약으로 전환

현재 CI-pinned MCP가 extension을 제공하지 않아 기본 trip release까지 불필요하게 중단된다. provider 커밋과 pin upgrade가 완료된 뒤에는 선택할 수 있지만 현재 순서에는 맞지 않는다.

### Schema field 존재만 확인

nested item, enum과 nullable drift를 요청 시점까지 발견하지 못하므로 선택하지 않았다.

### Facility 요청을 기존 세부 Tool 배열로 대체

실패 출처·제한·신선도가 제거되어 빈 배열의 의미를 판단할 수 없으므로 선택하지 않았다.
