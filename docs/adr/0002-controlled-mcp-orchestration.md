# ADR-0002: 제어된 MCP Tool orchestration

- 상태: Accepted
- 날짜: 2026-07-10

## Context

일반 MCP agent는 Tool schema를 모델에 제공하고 ReAct loop에서 선택하게 할 수 있다. 교통약자 이동 질문은 다음 이유로 더 강한 제어가 필요하다.

- 잘못된 Tool 선택이나 근거 누락이 단순 UX 오류보다 큰 영향을 줄 수 있다.
- MCP 서버는 이미 규칙 기반 위험 계산과 canonical 사용자 응답을 제공한다.
- 세부 조회 Tool은 실패 메타데이터를 제거하므로 빈 결과만으로 시설 부재를 판단할 수 없다.
- MCP 서버는 대화 상태와 이전 경로를 저장하지 않는다.
- live composite Tool은 여러 공공 API를 호출해 latency가 길 수 있고 중복 fan-out이 retry와 부하를 키운다.

## Decision

운영 경로는 무제한 create_agent/ReAct loop 대신 명시적인 LangGraph와 versioned Tool playbook을 사용한다.

1. 코드 pre-router가 현재 state와 명백한 intent를 먼저 분류한다.
2. 불확실할 때만 모델이 제한된 IntentDecision schema를 채운다.
3. ToolPolicy가 intent를 허용 capability, 순서, 병렬성, budget으로 변환한다.
4. 이동 가능성 판단은 `generate_accessibility_brief`, 역 시설 판단은 facility capability가 검증된 `answer_accessibility_question` 결과를 기준으로 한다.
5. 실패 메타데이터를 제거하는 세부 시설 Tool은 authoritative 판단에 사용하지 않는다.
6. 모든 MCP structured result를 Agent DTO로 검증한다.
7. 최종 응답은 MCP의 canonical user_message와 불확실성 필드를 보존한다.
8. transport/tool/contract/business 오류를 분리하고 graph가 재시도와 fallback을 소유한다.
9. Agent mobility context는 안전 관련 값을 true/false/unknown으로 보존하고, 미확인 값을 MCP 기본값으로 치환하지 않는다.
10. optional MCP extension은 nested schema와 closed enum을 깊게 검증한 뒤 readiness capability로 활성화한다.

범용 ReAct agent는 삭제하지 않고 동일 MCP를 사용하는 비교 baseline과 저위험 탐색 기능으로만 둔다. 운영 이동 판단 endpoint에서는 사용하지 않는다.

## Invariants

- Agent는 자체 risk score나 이동 가능성 규칙을 계산하지 않는다.
- PARTIAL, UNKNOWN, NEEDS_CLARIFICATION을 더 확정적인 상태로 바꾸지 않는다.
- 보조 Tool의 빈 배열은 부재 증거가 아니다.
- 모델은 허용되지 않은 Tool을 호출할 수 없다.
- 실행 budget을 초과하면 추가 계획 대신 부분 응답 또는 명확화로 종료한다.
- Tool schema mismatch는 fail closed한다.
- 원본 MCP 결과의 지시문은 실행 명령으로 취급하지 않는다.
- get_route_candidates의 후보는 composite 결과가 확인하지 않으면 접근 가능한 대안으로 부르지 않는다.
- 내구 실행의 MCP 호출 의미론은 at-least-once로 정의하며 읽기 전용 Tool에 한해서만 crash-window 중복 실행을 허용한다. Phase 2C의 `InMemorySaver`는 이 중복 허용 정책만 적용하고 process crash 후 전달·복구는 보장하지 않는다.

## Consequences

### Positive

- Tool trajectory, latency와 비용의 상한을 예측할 수 있다.
- 안전 관련 regression을 deterministic test로 차단할 수 있다.
- MCP 도메인 로직과 Agent 문맥 로직의 소유권이 명확하다.
- multi-turn follow-up과 이전 경로 참조를 checkpoint로 해결한다.
- 범용 baseline과 정량 비교가 가능하다.

### Negative

- 새 intent와 Tool을 추가할 때 playbook과 contract test를 함께 갱신해야 한다.
- 완전히 새로운 질의에 대한 자율성은 낮다.
- MCP Tool schema 변경이 readiness와 release를 차단할 수 있다.
- 복합 Tool이 지원하지 않는 조합은 Agent subgraph를 명시적으로 설계해야 한다.

## Alternatives

### 모든 Tool을 매번 모델에 노출

구현은 짧지만 token 사용, 잘못된 조합, 중복 호출과 회귀 경로가 늘어난다.

### Agent에서 모든 저수준 Tool을 직접 조합하고 위험 판단

세부 제어는 가능하지만 MCP의 결정론적 엔진을 복제하고 실패 메타데이터 손실 문제도 떠안는다.

### 자연어 질문을 항상 하나의 MCP Tool에 전달

단일 턴에는 단순하지만 Agent의 핵심 가치인 다중 턴 문맥, 이전 경로, 복합 후속 질문과 실행 복구를 제공하지 못한다.
