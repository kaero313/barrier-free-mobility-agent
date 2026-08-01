# Durable run ledger 운영 runbook

## 문서 상태와 범위

이 문서는 [ADR-0010](../adr/0010-durable-run-ledger-and-crash-semantics.md)에 정의한 Phase 4A
운영 절차다. Phase 4A 구현과 fault-injection 검증은 2026-07-14 완료했다. 이 완료는 crash 후보를
탐지하고 보존된 사실로 수동 분류할 수 있다는 의미이며 자동 복구나 exactly-once 보장이 아니다.

적용 범위는 thread 기반 run과 resume다. 기존 stateless
`POST /v1/accessibility/questions`는 durable run ledger 대상이 아니다. Phase 4B-2의 idempotency key가
있는 PostgreSQL 실행은 별도 worker와 lease-bound Tool/finalization을 사용하므로
[durable execution worker runbook](durable-execution-worker.md)을 함께 따른다. 이 문서의 수동 분류
절차는 header가 없는 호환 동기 경로와 ledger 자체 조사에 계속 적용한다. SSE와 `/ops`는 아직 없다.

## 운영 불변식

장애를 조사하기 전에 다음 불변식을 기준으로 삼는다.

1. 새 run은 graph 전에 run ID, active pointer와 `run.started`를 같은 transaction으로 예약한다.
2. `run_events`의 `(run_id, sequence)`가 commit된 실행 사실의 전체 순서다.
3. Tool transition은 모두 run event로 남고 terminal transaction이 started/terminal sequence를
   참조하는 immutable `tool_executions` summary를 한 번 INSERT한다.
4. public `ConversationRun` snapshot, LangGraph checkpoint와 run ledger는 서로 다른 저장 경계다.
5. 원본 질문, 역명, 이동 조건, Tool argument/result와 exception 문자열은 ledger에 없다.
6. Phase 4A는 open execution을 탐지하지만 자동 terminal 처리, replay와 repair를 하지 않는다.
7. owner-scoped thread 삭제만 event와 Tool transition을 cascade하며 개별 row를 수정·삭제하지 않는다.

## 정상 상태 확인

배포 또는 process 재시작 뒤 다음 순서로 확인한다.

1. liveness가 응답하는지 확인한다.
2. readiness에서 PostgreSQL schema와 DML contract, MCP contract가 모두 ready인지 확인한다.
3. repository migration version과 필수 table/index 검증이 통과했는지 startup log의 stable code로
   확인한다.
4. 새 smoke run 하나가 `run.started` 뒤 `run.interrupted | run.completed | run.failed` 중 하나로
   끝나는지 내부 test/read model로 확인한다.
5. event sequence가 1부터 중복·역순 없이 이어지는지 확인한다.
6. Tool을 사용한 run이라면 `tool.started`마다 terminal transition과 immutable summary가 각각
   최대 하나인지 확인한다.

운영 log나 incident ticket에 event payload, checkpoint 또는 database row 전체를 복사하지 않는다.
`request_id`, `run_id`와 허용된 stable code만 사용한다. thread는 raw 값 대신 운영 정책의 hash를
사용한다.

## 탐지 신호

Phase 4A 내부 조회는 다음 후보를 bounded result로 제공해야 한다. public route나 `/ops`를 만들지
않고 repository contract 또는 승인된 관리 query만 사용한다.

- terminal event가 없는 `running` execution
- 마지막 event 이후 checkpoint가 존재하거나 갱신된 execution
- `tool.started` 뒤 terminal transition이 없는 Tool execution ID
- Tool terminal transition은 있지만 후속 checkpoint/public snapshot이 없는 run
- public snapshot의 phase와 마지막 run event가 일치하지 않는 run
- sequence 중복, 역순 또는 terminal event 뒤 추가 event

활성 graph 실행에 결합된 lease와 heartbeat가 없으므로 시간 기준만으로 live request와 crash를
확정하지 않는다. 탐지 결과는
`stale/open candidate`이며 자동 조치의 입력으로 사용하지 않는다. 단일 replica를 지키고 새
process가 시작되기 전에 이전 process가 종료됐다는 배포 사실과 함께 판단한다.

## Crash window 분류

| 분류 | Ledger | Checkpoint/public snapshot | 의미 |
|---|---|---|---|
| A. graph 입력 전 종료 | `run.started`, terminal 없음 | checkpoint 없음, snapshot 없음 | 예약 뒤 graph 진입 전 종료 가능성 |
| B. checkpoint-ahead | running, terminal 없음 | checkpoint 존재 또는 더 최신 | graph가 진행됐지만 metadata 확정 전 종료 가능성 |
| C. open Tool | `tool.started`만 존재 | 결과 commit 불명 | 외부 호출 전 또는 응답 저장 전 종료 가능성 |
| D. Tool terminal/checkpoint gap | Tool terminal 존재 | 후속 checkpoint 없음 | 외부 결과는 있었지만 graph state commit 불명 |
| E. metadata terminal | terminal run event 존재 | 같은 terminal snapshot 존재 | 정상 완료, orphan 아님 |
| F. legacy snapshot | ledger completeness 없음 | 기존 snapshot만 존재 | Phase 4A 이전 run, event history 추정 금지 |

분류 C와 D에서 MCP 호출 여부를 정확히 증명할 수 없으면 결과는 unknown이다. read-only MCP는 향후
복구 시 at-least-once로 다시 호출될 수 있지만 Phase 4A에서는 자동 호출하지 않는다.

## 장애 대응 절차

### 1. PostgreSQL 또는 ledger write 실패

1. readiness가 not ready인지 확인하고 새 traffic을 중단한다.
2. `request_id`, `run_id`, 마지막 성공 sequence와 stable persistence error code만 기록한다.
3. `tool.started` 저장 전 실패라면 MCP 호출이 0회였는지 test/metric 신호로 확인한다.
4. 외부 응답 뒤 terminal event 저장 실패라면 성공 결과로 확정하거나 수동 event를 추가하지 않는다.
5. DB 연결과 schema를 복구한 뒤 open execution 후보를 다시 조회한다.
6. 자동 resume 없이 사용자에게 새 thread가 필요할 수 있음을 안내한다.

### 2. Agent process 비정상 종료 또는 재시작

1. 이전 process가 완전히 종료됐는지 확인한다. Phase 4A는 multi-replica 실행을 허용하지 않는다.
2. 새 process를 시작하고 schema/readiness 검증이 통과할 때까지 traffic을 열지 않는다.
3. 재시작 시점 이전의 open execution과 open Tool candidate를 조회한다.
4. 각 run을 위 crash window 표의 A~F로 분류한다.
5. checkpoint나 ledger row를 수정하거나 Tool을 수동 재호출하지 않는다.
6. owner가 기존 thread를 삭제하기 전까지 해당 active run이 새 run을 차단하는 것을 정상적인
   fail-closed 동작으로 취급한다.

### 3. sequence 또는 transition 불변식 위반

1. 해당 run에서 새 실행을 중단한다.
2. migration version, binary revision과 repository error code를 기록한다.
3. raw payload를 출력하지 않고 run ID, sequence와 transition code만으로 증거를 보존한다.
4. event UPDATE, 빈 sequence 삽입, terminal row 삭제로 고치지 않는다.
5. 배포 rollback보다 forward-fix migration과 repository 수정 여부를 검토한다.
6. 같은 결함이 memory/PostgreSQL contract test에 재현되기 전에는 재배포하지 않는다.

### 4. redaction 위반 의심

1. 관련 log와 trace의 추가 전송을 중단하고 접근 범위를 제한한다.
2. 질문, 역명, 이동 조건, Tool payload, URL/header/token 또는 exception 원문이 event나 log에
   있는지 sentinel 기준으로 확인한다.
3. 발견한 민감값을 incident ticket에 재복사하지 않고 위치와 stable record ID만 기록한다.
4. event payload DTO allowlist, exception 생성 지점과 formatter를 함께 조사한다.
5. owner-scoped thread 삭제와 플랫폼 log/backup 보존 정책에 따른 삭제 범위를 결정한다.
6. redaction regression test와 저장소 전체 text 검사가 통과한 뒤에만 traffic을 재개한다.

## 사용자 영향과 허용 조치

Phase 4A의 open execution은 자동으로 복구되지 않으며 같은 thread의 새 run을 차단할 수 있다.
허용되는 조치는 다음 두 가지다.

- 사용자가 새 thread에서 질문을 다시 시작하도록 안내한다.
- 인증된 owner의 기존 thread 삭제 경로로 checkpoint, run, event와 Tool transition을 함께
  삭제한다.

운영자가 owner 확인 없이 active pointer만 해제하거나 event를 수동 terminal 처리해서는 안 된다.
원본 질문을 확인해 재실행 여부를 추측하지도 않는다.

## Retention과 삭제

- Phase 4A에는 event 전용 TTL과 background cleanup job이 없다.
- run event와 Tool transition은 parent thread와 같은 lifecycle을 갖는다.
- 개별 event, sequence 범위 또는 Tool transition만 삭제하지 않는다.
- owner-scoped thread 삭제는 관련 checkpoint와 ledger 전체에 cascade해야 한다.
- backup, WAL과 외부 log retention은 데이터베이스·관측 플랫폼 정책으로 별도 관리한다.
- 향후 독립 retention을 추가할 때는 active/open run, audit 요구와 owner 삭제 전파를 별도 ADR로
  검증한다.

## Migration과 배포

1. Agent traffic을 중지하고 단일 migration job으로 repository schema를 확장한다.
2. migration 전 run은 `legacy/incomplete ledger`로 유지하며 가상 event를 backfill하지 않는다.
3. application startup의 exact migration/table/index 검증을 통과시킨다.
4. memory contract, PostgreSQL integration, restart와 crash-window fault injection을 실행한다.
5. 공개 OpenAPI에 ledger DTO, event route와 `/ops`가 추가되지 않았는지 확인한다.
6. 새 binary를 단일 replica로 시작하고 readiness 뒤 traffic을 연다.

schema가 적용된 뒤 이전 binary로 즉시 되돌리기보다 forward-fix를 기본으로 한다. rollback이 꼭
필요하면 이전 binary가 확장된 nullable/internal column과 신규 table을 안전하게 무시하는지 먼저
검증한다. 신규 ledger table을 drop하거나 migration version을 수동으로 낮추지 않는다.

## Release gate

다음 release gate는 2026-07-14 통과했다.

- memory와 PostgreSQL의 reservation·sequence·transition contract가 동일하다.
- graph 전, checkpoint 후, Tool 시작 후와 Tool 응답 후 crash injection이 통과한다.
- PostgreSQL restart가 open candidate를 탐지하지만 MCP를 자동 호출하지 않는다.
- user/Tool/secret sentinel이 event, exception representation과 JSON log에 남지 않는다.
- thread 삭제가 run, checkpoint, event와 Tool transition 전체에 전파된다.
- response schema v3/v4, Web contract와 기존 thread API 의미가 유지된다.
- Phase 4A gate 당시에는 public event route, SSE, `/ops`, worker와 idempotency가 활성화되지 않았다.

검증은 memory와 실제 PostgreSQL repository, graph·service orchestration, PostgreSQL 재시작 뒤
resume과 open candidate 조회, Compose 제품 경로 및 공개 contract drift 검사를 포함했다. 이후
ledger schema나 실행 순서를 변경하면 같은 gate를 다시 통과해야 한다.

## 후속 단계로 넘기는 항목

- Phase 4B-3C: readiness 분리와 core chaos safety 뒤 남은 soak·replica release gate
- 후속 public contract: owner-scoped event projection, SSE와 `Last-Event-ID`
- Phase 5: OIDC, RBAC, audit와 protected Agent Console
- retention 후속: TTL, orphan cleanup, backup 삭제와 비식별화 정책
