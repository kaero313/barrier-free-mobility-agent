# ADR-0010: Durable run ledger와 crash window 의미론

- 상태: Accepted
- 날짜: 2026-07-14
- 범위: Phase 4A thread run, 내부 실행 원장, Tool transition, crash 탐지와 retention

## Context

현재 `AsyncPostgresSaver`는 LangGraph state와 interrupt 위치를, conversation repository는
owner-scoped thread와 공개 `ConversationRun` snapshot을 저장한다. 저장이 끝난 clarification은
process 재시작 뒤 조회하고 resume할 수 있지만, 새 run의 공개 ID와 metadata row는 graph가
반환된 뒤 만들어진다. 따라서 graph 실행 중 process가 종료되면 어떤 run이 실행 중이었는지,
어느 Tool 호출까지 시도했는지, checkpoint와 공개 snapshot 중 어느 쪽이 앞서는지 안정적으로
판별할 사실 원장이 없다.

LangGraph checkpoint transaction과 Agent metadata transaction은 같은 transaction이 아니다.
MCP 호출도 PostgreSQL transaction에 포함할 수 없다. 이 경계에서 exactly-once를 주장하거나
checkpoint의 `tool_ledger`를 운영 원장으로 재해석하면 외부 호출과 저장 사실이 서로 다른
상태를 숨기게 된다.

Phase 4 전체 목표에는 idempotency, API/worker 분리, lease, recovery sweeper, SSE replay가
포함된다. Phase 4A는 그 기능을 한 번에 활성화하지 않고 먼저 실행과 Tool attempt의 사실
원장을 만든다. 기존 response schema v3/v4, 동기 thread/run API와 Web 동작은 유지한다.

## Decision

### 1. 범위와 비범위

Phase 4A는 thread 기반 `POST /v1/threads/{thread_id}/runs`와 같은 run의 resume에만 적용한다.
기존 stateless `POST /v1/accessibility/questions`는 ledger 범위에 포함하지 않는다.

다음 기능은 Phase 4A에 포함하지 않는다.

- 새 public event route, SSE와 `Last-Event-ID`
- `/ops`와 public processing timeline
- idempotency key와 중복 request 결과 재사용
- API/worker 분리, DB lease, heartbeat와 recovery sweeper
- process restart 뒤 자동 replay 또는 자동 repair
- 원본 Tool artifact 저장과 Tool 결과 재사용

내부 ledger DTO를 public response, OpenAPI 또는 Web contract에 추가하지 않는다.

### 2. 공개 snapshot과 내부 실행 상태를 분리한다

`ConversationRun`은 계속 기존 public response로 projection 가능한 immutable snapshot이다.
실행 전·실행 중 상태는 별도의 내부 run execution record가 소유한다. public `run_status`에
`running`을 추가하거나 임시 실패 결과를 만들어 실행 중 row를 표현하지 않는다.

내부 execution phase의 초기 allowlist는 다음과 같다.

- `running`: start 또는 resume invocation이 실행 중이다.
- `interrupted`: 저장된 public clarification snapshot에서 resume할 수 있다.
- `completed`: graph가 정상 종료해 public snapshot이 확정됐다.
- `failed`: 기술 실패가 확정되고 더 이상 resume하지 않는다.

새 run의 `running` record는 아직 public snapshot이 없을 수 있다. resume invocation이
`running`인 동안에는 직전 `interrupted` snapshot을 유지하지만, 새 resume은 내부 phase 검사로
차단한다. business status가 `FAILED`여도 graph가 정상 종료했다면 execution phase와 run event는
`completed`이고, `failed`는 기술 실패에만 사용한다.

### 3. run을 graph 전에 원자적으로 예약한다

새 thread run은 graph를 호출하기 전에 하나의 repository transaction에서 다음 변경을 모두
commit한다.

1. public run ID를 선할당한다.
2. owner와 thread 호환성, run capacity와 busy 상태를 확인한다.
3. thread의 `active_run_id`와 `run_count`를 갱신한다.
4. 내부 `running` execution record를 저장한다.
5. sequence 1의 `run.started` event를 저장한다.

어느 단계든 실패하면 모두 rollback하며 graph와 MCP는 호출하지 않는다. `active_run_id`는
interrupted run뿐 아니라 running run도 가리켜 같은 thread의 동시 실행을 막는다.

graph 결과를 public snapshot으로 확정하는 변경과 해당 `run.interrupted`, `run.completed` 또는
`run.failed` event도 하나의 repository transaction에 저장한다. checkpoint commit은 이
transaction에 포함되지 않는다.

resume는 같은 run ID와 event sequence를 이어 쓴다. 각 resume invocation은 새 request ID와
`run.resume_started` event를 갖고, 내부 phase를 `running`으로 바꾼 뒤 graph를 호출한다.

### 4. `run_events`가 실행 순서의 source of truth다

`run_events`는 run 안에서 commit된 사실의 전체 순서를 제공하는 append-only 원장이다.

- event key는 `(run_id, sequence)`이며 sequence는 1부터 단조 증가한다.
- sequence 할당과 event insert는 같은 transaction이다.
- `max(sequence) + 1` 계산에 의존하지 않고 run aggregate를 잠근 상태에서 다음 값을 할당한다.
- transaction rollback으로 저장되지 않은 event는 sequence를 소비하지 않는다.
- payload는 schema version이 있는 closed DTO이며 임의 `dict[str, Any]`를 받지 않는다.
- terminal execution 뒤에는 새 event를 추가할 수 없다.

초기 내부 event type은 `run.started`, `run.resume_started`, `run.interrupted`,
`run.completed`, `run.failed`, `run.orphaned`, `tool.started`, `tool.completed`,
`tool.failed`, `tool.timeout`으로 제한한다. `run.orphaned`는 후속 명시적 recovery/audit를 위해
예약하며 Phase 4A의 read-only 탐지가 자동으로 append하지 않는다. 이것은 향후 public SSE event
계약과 같은 저장 원장을 사용할 수 있다는 뜻이지, 저장 event를 그대로 사용자에게 공개한다는
뜻은 아니다. public event는 별도 owner-scoped allowlist projection과 접근 제어가 준비된 뒤
정의한다.

### 5. `tool_executions`는 immutable terminal summary다

`tool.started`와 `tool.completed | tool.failed | tool.timeout` transition은 모두
`run_events`에 기록한다. terminal transition을 저장하는 같은 transaction에서
`tool_executions` summary를 한 번 INSERT한다. 이 summary는 같은 `tool_execution_id`의
`started_sequence`와 `terminal_sequence`를 참조하며 저장 뒤 UPDATE하지 않는다.

`tool_execution_id`는 같은 attempt의 event를 묶고 다음 불변식을 적용한다.

- terminal event에는 앞선 `tool.started` event가 정확히 하나 있어야 한다.
- attempt당 terminal event와 `tool_executions` summary는 각각 최대 하나다.
- summary의 started/terminal sequence는 같은 run과 execution ID의 실제 event를 참조한다.
- `completed | failed | timeout` 저장 뒤 같은 attempt를 수정하지 않는다.
- 호출 전 `tool.started` 저장이 실패하면 외부 Tool을 호출하지 않는다.
- 외부 응답 뒤 terminal event와 summary 저장이 실패하면 성공을 확정하지 않는다.

`tool.started` 뒤 process가 종료되면 summary가 없는 open attempt로 탐지한다. 시작 시 빈 summary
row를 만들고 terminal 상태로 UPDATE하는 방식은 사용하지 않는다.

ledger는 원본 Tool 결과를 저장하지 않는다. `tool.completed`가 있어도 해당 결과가 checkpoint에
commit되지 않았다면 재사용할 artifact가 아니며, 향후 복구에서 Tool이 다시 호출될 수 있다.

### 6. invocation별 correlation을 전달한다

start와 resume마다 immutable invocation context를 만들고 다음 값을 service에서 graph와 Tool
recorder까지 전달한다.

- `request_id`
- `run_id`
- `thread_id`
- owner authorization context
- `flow_version`, `state_schema_version`

request ID는 event마다 기록해 같은 run의 여러 resume request를 구분한다. request ID는
idempotency key가 아니며 uniqueness를 가정하지 않는다. owner ID와 사용자 입력은 event payload나
일반 log label로 복제하지 않는다. 외부 log에는 raw thread ID 대신 기존 정책의 bounded hash를
사용한다.

### 7. checkpoint와 ledger의 crash window를 숨기지 않는다

checkpoint, run ledger와 외부 MCP 호출은 하나의 transaction이 아니다. Phase 4A는 다음 상태를
탐지하되 자동으로 수정하지 않는다.

| 관측 상태 | 의미 | Phase 4A 처리 |
|---|---|---|
| `running`, checkpoint 없음 | graph 입력 checkpoint 전 종료 가능성 | open execution 후보로 보고 새 run 차단 |
| `running`, checkpoint 존재 | checkpoint가 public snapshot보다 앞설 수 있음 | checkpoint-ahead 후보로 보고 자동 resume 금지 |
| `tool.started`만 존재 | 호출 전 또는 호출 결과 저장 전 종료 가능성 | 결과 unknown, 자동 재호출 금지 |
| Tool terminal event, 후속 checkpoint 없음 | 외부 호출 결과는 있었지만 state commit 여부 불명 | 저장 결과 재사용 금지 |
| terminal snapshot과 terminal event 일치 | 완료된 metadata transaction | orphan 대상에서 제외 |

lease와 heartbeat가 없으므로 단순 시간 초과만으로 live process와 crash를 완전히 구분할 수 없다.
따라서 Phase 4A 내부 조회는 `stale/open execution candidate`를 반환하며 자동 terminal 전환,
event 보정, checkpoint 삭제 또는 replay를 수행하지 않는다. 단일 Agent replica 제한도 유지한다.

현재 MCP Tool은 읽기 전용이므로 향후 명시적 복구에서 checkpoint 전에 완료를 증명하지 못한
호출은 at-least-once로 다시 실행될 수 있다. end-to-end exactly-once를 주장하지 않는다. 쓰기
Tool은 MCP 자체 idempotency 계약 없이는 등록하지 않는다.

### 8. checkpoint와 ledger의 권위를 분리한다

- LangGraph checkpoint는 업무 state, interrupt 위치와 budget의 authority다.
- `run_events`와 `tool_executions`는 실행 사실과 순서의 authority다.
- public `ConversationRun`은 마지막 성공 metadata transaction의 사용자 응답 snapshot이다.
- `agent_runs.usage`는 계속 운영 read model이며 exact billing ledger가 아니다.
- checkpoint의 `tool_ledger`와 `run_tool_ledger`는 budget용 bounded 이름 목록이며 durable Tool
  attempt ledger가 아니다.

한 계층이 다른 계층보다 앞선 상태를 허용하고 내부 projection에서 감지한다. silent backfill이나
한쪽 값으로 다른 쪽을 자동 덮어쓰지 않는다.

### 9. strict allowlist와 redaction을 적용한다

event와 Tool transition에는 다음과 같은 bounded code만 저장한다.

- event/tool schema version, event type와 sequence
- capability code, logical call ID, attempt와 transition
- stable Agent-owned outcome/error code와 bounded duration
- correlation ID와 execution sequence
- `resolve_station`의 matched 여부, 최대 20개 candidate count와 clarification 여부
- 이동 brief의 response status, clarification 여부와 bounded evidence/limitation count
- 시설 질문의 response status, clarification 여부와 bounded evidence count

다음 값은 저장하지 않는다.

- 자연어 질문, clarification 답변과 canonical `user_message`
- 출발지·목적지·역명과 mobility/facility 조건
- MCP Tool argument, 원본 request/response와 structured artifact
- exception message, stack trace, URL, header, token과 credential
- raw `AgentState`, prompt, model wire와 owner ID

payload DTO는 extra field, 문자열/배열 크기와 closed enum을 검증한다. 오류 객체의 `repr`, 일반
log와 trace도 동일한 sentinel redaction test를 통과해야 한다. private payload를 exception
message에 넣은 뒤 formatter에서 숨기는 방식에 의존하지 않는다.

### 10. append-only와 삭제·retention 경계를 함께 지킨다

append-only는 살아 있는 run의 event를 UPDATE하거나 개별 DELETE하지 않는다는 뜻이다. 사용자
요청에 따른 owner-scoped thread 삭제는 기존 계약대로 run, event와 Tool transition에 cascade한다.
이 삭제는 append-only 위반이 아니라 데이터 lifecycle 경계다.

Phase 4A에서는 event 전용 TTL, latest-N pruning과 background cleanup job을 만들지 않는다.
event와 Tool transition은 parent thread와 같은 lifecycle을 갖는다. 별도 보존 기간과 비식별화
정책은 인증 subject, audit 요구와 운영 저장량을 측정한 뒤 후속 ADR에서 정한다.

### 11. legacy row를 완전한 history처럼 만들지 않는다

Phase 4A migration 전에 저장된 `agent_runs`에는 실제 실행 event가 없다. 기존 snapshot을 이용해
가상의 `tool.started`나 `run.completed` history를 만들지 않는다. 내부 projection은 이 run을
`legacy/incomplete ledger`로 구분하고 orphan 검사와 event completeness 계산에서 제외한다.

Memory와 PostgreSQL adapter는 같은 transition·sequence·owner contract를 구현한다. memory backend는
process-local 검증 경로일 뿐 재시작 내구성을 보장하지 않는다.

## Consequences

### Positive

- graph 실행 전에 안정적인 run ID와 request/run/thread correlation이 생긴다.
- checkpoint와 metadata 사이 crash window를 숨기지 않고 운영 신호로 조회할 수 있다.
- Tool attempt의 시작과 terminal 사실을 원본 payload 없이 추적할 수 있다.
- 향후 worker lease, idempotency와 SSE projection이 같은 ordering source 위에 추가될 수 있다.
- public response schema와 Web 동작을 바꾸지 않고 내부 운영 기반선을 확장한다.

### Negative와 제한

- checkpoint, external I/O와 ledger의 exactly-once transaction은 제공하지 않는다.
- Phase 4A만으로 process crash 뒤 run을 자동 완료하거나 기존 thread를 해제할 수 없다.
- run reservation과 event append가 추가 DB transaction과 row contention을 만든다.
- 원본 Tool 결과를 저장하지 않으므로 terminal Tool event만으로 graph를 재구성할 수 없다.
- worker lease 전까지 Agent replica 한 개 제한을 유지해야 한다.

## Rejected alternatives

### public `run_status`에 `running` 추가

기존 schema v3/v4와 Web 상태 모델을 변경하고 내부 실행 상태를 사용자 응답 의미와 섞으므로
선택하지 않았다.

### graph 반환 뒤 run ID와 ledger를 한 번에 생성

현재 crash window를 그대로 남겨 실행 중 사실과 Tool attempt를 상관시킬 수 없으므로 선택하지
않았다.

### Tool attempt row를 시작 후 UPDATE

최종 값만 보면 중간 crash와 잘못된 overwrite를 구분하기 어렵고 append-only 결정과 충돌하므로
Tool transition은 `run_events`에 남기고 terminal에서 immutable summary를 한 번 INSERT한다.

### checkpoint trajectory를 운영 ledger로 사용

checkpoint는 실행 중 앞서거나 뒤설 수 있고 owner-scoped public projection, request correlation과
Tool outcome을 제공하지 않으므로 선택하지 않았다.

### Phase 4A에서 자동 replay

lease, attempt claim, idempotency와 artifact가 없는 상태에서 duplicate 외부 호출과 동시 실행을
안전하게 제어할 수 없으므로 선택하지 않았다.

## Validation and rollout

- memory와 PostgreSQL에서 reservation, sequence, transition과 owner contract를 동일하게 검증한다.
- transaction fault injection으로 graph 전, checkpoint 후, Tool 시작 후와 Tool 응답 후 crash
  window를 각각 만든다.
- PostgreSQL restart 뒤 open execution과 open Tool attempt를 탐지하되 gateway 호출이 자동으로
  발생하지 않는지 검증한다.
- event table, DTO, 오류와 JSON log에 사용자·Tool sentinel이 남지 않는지 검증한다.
- public response schema v3/v4와 OpenAPI에 ledger field나 새 route가 생기지 않는지 검증한다.
- migration과 운영 판단 절차는 [durable run ledger runbook](../runbooks/durable-run-ledger.md)을
  따른다.

## Follow-up

1. Phase 4B에서 idempotency key, durable queue와 DB lease를 별도 계약으로 추가한다.
2. recovery sweeper는 lease 만료와 checkpoint 검사 뒤에만 자동 resume을 허용한다.
3. owner-scoped public event projection과 SSE replay 계약을 별도 version으로 정의한다.
4. OIDC·RBAC·audit가 준비된 뒤 redacted internal ops projection을 `/ops`에 연결한다.
5. 쓰기 Tool은 provider-side idempotency와 사용자 승인 계약이 준비된 뒤에만 등록한다.
