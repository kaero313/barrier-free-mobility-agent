# ADR-0011: 단계형 durable execution 조정 계약

- 상태: Accepted
- 결정일: 2026-07-15
- 최근 갱신: 2026-07-16 — Phase 4B-3C Worker replica release-gate 자동화 계약
- 범위: Phase 4B idempotency reservation, durable queue, lease·heartbeat·fencing과 단계형 활성화

## 배경

Phase 4A는 graph 실행 전에 run ID를 예약하고 `run_events`와 `tool_executions`에 실행 사실을
남긴다. 이 기반으로 process crash 뒤 open execution과 open Tool attempt를 찾을 수 있지만,
현재 API process가 graph를 직접 실행하므로 다음 문제는 아직 해결되지 않는다.

- 같은 mutation이 전송 결과 불명 상태에서 다시 도착하면 중복 run을 만들 수 있다.
- PostgreSQL checkpoint는 graph state를 복구하지만 중단된 HTTP request 자체를 다시 실행할
  durable queue가 아니다.
- 실행 owner를 증명하는 lease와 fencing token이 없어 stale process와 새 process의 동시 write를
  안전하게 차단할 수 없다.
- 질문 또는 clarification 답변을 worker로 넘기려면 private input을 잠시 저장해야 하지만,
  `run_events`의 redacted allowlist 계약에 원문을 섞을 수 없다.

idempotency, queue, worker 분리와 자동 복구를 한 release에서 동시에 켜면 API 응답 의미,
checkpoint crash window와 다중 replica 안전성을 한 번에 바꾸게 된다. 특히 현재 public
`ConversationRun` schema v3/v4에는 processing 상태가 없고 Web은 동기 응답을 기준으로 동작한다.
따라서 조정 자료구조와 불변식을 먼저 검증하고 실행 경로를 단계적으로 전환한다.

## 결정

### 1. Phase 4B를 세 단계로 활성화한다

#### Phase 4B-1 — coordination substrate

구현·검증을 완료한 첫 단위다.

- repository 내부에 atomic idempotency reservation, invocation별 durable job, lease claim,
  heartbeat, 만료 판정과 fencing compare-and-set 계약을 구현·검증한다.
- 이 coordination substrate는 repository contract와 PostgreSQL migration에서만 사용한다. public
  route는 아직 `Idempotency-Key`를 수용하거나 job을 예약하지 않는다.
- API process는 기존처럼 graph를 직접 실행하고 같은 request에서 최종 또는 clarification
  `ConversationRun`을 반환한다. 현재 동기 path는 새 durable job을 만들지 않는다.
- Agent replica는 계속 하나로 제한한다. lease가 존재한다는 이유만으로 자동 recovery나 수평 확장을
  주장하지 않는다.

#### Phase 4B-2 — worker execution과 bounded wait

구현·검증을 완료한 단위다.

- start와 resume route가 `Idempotency-Key`를 수용하고 API는 run, idempotency record와 job을
  원자적으로 예약한다. 별도 worker가 job을 lease로 claim한다.
- API는 정해진 시간 동안 같은 run의 public snapshot 확정을 기다린다. 시간 안에 끝나면 기존
  schema v3/v4 응답을 그대로 반환한다.
- bounded wait가 끝나도 API가 graph를 대신 실행하거나 POST를 blind retry하지 않는다. 같은
  idempotency key의 후속 요청은 기존 job에 다시 결합한다.
- checkpoint cursor와 ledger를 함께 분류한 recovery policy를 활성화한다. lease 만료만으로
  checkpoint를 지우거나 처음부터 replay하지 않는다.
- queue lease 확인과 기존 `finalize_run`을 두 번의 호출로 나누지 않는다. Tool event append와 run
  finalization이 같은 transaction 안에서 현재 lease token·generation을 검증하는 lease-bound
  writer/finalization command를 먼저 도입한다.
- API container와 worker container는 각각 하나로 시작한다.

호환 전환 동안 Agent API의 `Idempotency-Key`는 optional이다. 같은 제품의 BFF는 start/resume
mutation마다 UUID key를 만들고 전달하며, header와 durable coordinator가 모두 있을 때만 새 worker
경로를 사용한다. header가 없는 native client와 memory backend는 기존 동기 경로를 유지한다. 공개
`ConversationRun`에 처리 중인 것처럼 보이는 임시 snapshot은 만들지 않는다. bounded wait가 끝나면
retryable `409 EXECUTION_IN_PROGRESS`를 반환하고, 같은 key가 다시 도착하면 기존 job에 결합한다.

processing receipt, `202` response, public processing status가 필요해지면 기존 v3/v4에 임시 field를
추가하지 않고 별도 versioned public 계약으로 결정한다. Phase 4B-2의 선결 조건은 아니다.

#### Phase 4B-3 — multi-replica gate

Phase 4B-3A worker readiness·admission safety와 Phase 4B-3B core multi-replica chaos safety를
구현·검증했다. PostgreSQL schema v9의 fenced Worker registry, idle heartbeat, 독립 healthcheck와
redacted queue health를 사용한다. 신규 durable reservation은 idempotency replay를 확인한 뒤
ready·unexpired Worker row를 같은 transaction에서 잠그며, Worker가 없으면
`EXECUTION_WORKER_UNAVAILABLE`로 전체 mutation을 rollback한다. 공개 `/health/ready`는
API/MCP/persistence readiness로 유지해 Worker probe와 결합하지 않는다.

Phase 4B-3B는 다음 correctness 경계를 추가한다.

- `EXECUTION_WORKER_ID=auto`는 container hostname에서 replica별 ID를 만들고, process-local
  mode-0600 state의 token·generation과 PostgreSQL registry row가 정확히 일치해야 Worker health가
  성공한다.
- job claim은 worker ID 문자열이 아니라 ready·fresh한 exact registration의 ID·token·generation에
  결합한다. registration 교체와 claim은 같은 PostgreSQL transaction에서 직렬화한다.
- queue heartbeat·terminal transition, Tool event와 run finalization에 더해 LangGraph checkpoint
  write도 current execution lease row로 fence한다. checkpoint는 correlation, execution job ID와 lease
  generation provenance를 보존한다.
- checkpointer adapter는 모든 async saver access를 process-local outer lock으로 직렬화한다. write는
  outer saver lock을 먼저 얻고 lease row guard를 나중에 얻는 단일 lock order를 강제해 saver 내부
  lock과 PostgreSQL pool/lease lock의 inversion을 막는다. 이 outer lock은 ordering 장치이며
  cross-process correctness authority는 계속 PostgreSQL lease row다.
- shutdown grace를 넘긴 active task는 lease를 compare-and-set으로 revoke한 뒤 분리한다. 취소를
  무시한 task의 늦은 checkpoint·Tool·finalize write는 stale lease로 거부된다.
- production 설정으로 열리지 않는 constructor-only deterministic fault injector를 두고 실제
  PostgreSQL 경쟁·stale delayed write·checkpoint lock과 subprocess hard-kill recovery를 검증한다.

Phase 4B-3B core gate가 완료됐다는 사실은 일반 multi-replica 운영 승인이 아니다. Phase 4B-3C는
local/test Compose에서 반복 workload·rolling restart·PostgreSQL 일시 장애와 queue/recovery budget을
검증하는 자동화를 제공한다. 이 gate의 고정 scope는 `Agent = 1, Worker = 2`다. key 없는 호환 동기
경로가 process-local인 동안 Agent replica 확대는 3C 승인 범위에 포함하지 않고 계속 1개로 제한한다.
smoke profile은 release 승인이 아니다. long-lived exact-registration/queue watcher를 적용한 smoke
재검증은 대기 중이고 정식 4시간 release profile도 미실행·미승인이므로 일반 운영 기본값은 Agent
1개·Worker 1개다.

### 2. idempotency는 owner와 mutation target 범위에서 원자적으로 예약한다

idempotency scope는 다음 값으로 구성한다.

- owner authorization scope
- operation: `start_run | resume_run`
- target: start의 `thread_id` 또는 resume의 `run_id`
- client가 보낸 key

repository에는 이 네 값을 그대로 저장하지 않는다. owner는 별도 column으로 두고,
`operation + target + client key`를 domain-separated HMAC으로 계산한 64자리
`idempotency_digest`만 저장한다. 따라서 같은 client key라도 operation 또는 target이 다르면 다른
digest가 된다. key 원문은 저장하거나 log label로 사용하지 않는다. 정규화·검증이 끝난 request
payload도 canonical 형태의 server-secret 기반 fingerprint로 만들며, 질문과 답변처럼 추측 가능한
문자열에 일반 hash를 사용하지 않는다.

같은 scope에서 다음 의미를 적용한다.

- 같은 key와 같은 fingerprint: 새 run이나 job을 만들지 않고 기존 record를 반환한다. public
  snapshot이 확정됐다면 같은 결과를 projection한다.
- 같은 key와 다른 fingerprint: `409` idempotency conflict로 거부하며 기존 job은 바꾸지 않는다.
- 기존 job이 queued 또는 leased이고 public snapshot이 없다면 Phase 4B-2 API는 새 실행을 시작하지
  않고 in-progress conflict로 끝낸다. processing 상태를 가짜 `ConversationRun`으로 만들지 않는다.
- owner 또는 target이 다르면 같은 문자열 key를 사용해도 별도 scope다. 다른 owner의 record 존재
  여부는 노출하지 않는다.
- repository가 같은 scoped digest를 다른 operation·target과 함께 받으면 caller의 scope 계산 오류나
  digest 충돌로 간주해 방어적으로 conflict 처리한다.

start 예약 transaction은 기존 run ID, thread active pointer, run count와 `run.started` event에
idempotency record와 첫 job을 함께 묶는다. resume 예약도 기존 execution attempt와
`run.resume_started` event, idempotency record와 resume job을 함께 묶는다. 어느 write든 실패하면
graph와 MCP 호출은 0회다.

### 3. queue item은 run이 아니라 invocation 단위다

하나의 run에는 initial invocation과 여러 clarification resume invocation이 생길 수 있다. 따라서
queue item은 run당 하나가 아니라 invocation·execution attempt당 하나다. 최소한 다음 정보를
분리해 가진다.

- stable job ID, run/thread correlation, request ID와 execution attempt
- `start | resume` operation과 payload schema version
- Phase 4B-1의 `queued | leased | completed | failed` coordination state. `recovery_required`는 Phase
  4B-2에서 checkpoint·ledger 분류 결과로 추가할 recovery disposition이며 현재 queue state가 아니다.
- available time, lease owner, lease expiry, last heartbeat와 monotonically increasing fencing token
- idempotency scope digest와 request fingerprint
- private JSONB job payload 또는 terminal scrub marker

`run_events`는 계속 commit된 실행 사실과 순서의 authority이고, queue item은 누가 다음 실행권을
갖는지에 대한 authority다. LangGraph checkpoint는 업무 state와 interrupt 위치의 authority다.
어느 한 계층의 값으로 다른 계층을 silent repair하지 않는다.

### 4. lease에는 fencing이 반드시 동반된다

claim은 PostgreSQL row lock과 compare-and-set으로 정확히 한 claimant에게 새 lease와 증가한 fencing
token을 발급한다. heartbeat는 아직 만료되지 않은 동일 owner·token만 연장할 수 있다.

- claim caller는 현재 Worker registration 전체를 제출한다. repository는 같은 transaction에서
  worker ID·registration token·registration generation·ready 상태와 DB clock 기준 freshness를
  확인하므로, 교체된 process나 만료된 presence는 job lease를 받을 수 없다.
- lease owner 문자열만 비교해서는 안 된다. 같은 process identity가 재사용될 수 있으므로 매 claim의
  증가한 fencing token을 함께 검사한다.
- lease 만료 뒤 새 claimant가 더 큰 token을 받으면 이전 token의 heartbeat, terminal queue write와
  run finalization은 모두 거부된다.
- heartbeat 중단은 recovery 후보 신호이지 외부 Tool 호출이 실행되지 않았다는 증명이 아니다.
- system clock 의존성을 줄이기 위해 lease 판정은 PostgreSQL server time을 기준으로 한다.
- Phase 4B-1에서는 lease coordination API를 검증하되 기존 동기 graph path의 crash를 자동 claim해
  재실행하지 않는다.

Phase 4B-3B부터 LangGraph checkpoint write도 같은 fencing 경계에 포함한다. checkpointer adapter는
read·list·write·delete를 하나의 process-local outer lock으로 직렬화한다. `aput`과 `aput_writes`는 이
outer lock을 먼저 얻은 뒤 current lease row를 잠그고 DB clock·token·generation을 검증한 transaction을
유지한 채 실제 saver write를 수행한다. 따라서 saver access와 lease guard의 lock order가 일정하고,
checkpoint write와 다음 claimant의 lease 교체도 직렬화된다. 교체가 먼저 commit되면 stale writer는
saver를 호출하기 전에 거부된다. 실제 2-worker vertical smoke에서 드러난 saver-lock/pool-lock inversion
경로를 회귀 테스트로 고정했다. 이 구조는 guard가 보유한 connection과 saver connection이 동시에
필요하므로 PostgreSQL pool max size는 최소 2다.

### 5. recovery는 checkpoint cursor가 준비된 뒤에만 활성화한다

Phase 4B-2 worker는 lease가 만료된 job을 바로 처음부터 실행하지 않는다. 다음 자료를 함께 읽어
recovery disposition을 결정한다.

- job의 last safe checkpoint cursor와 fencing token
- `agent_runs` execution phase와 public snapshot 존재 여부
- 마지막 `run_events` sequence
- open Tool attempt와 terminal Tool summary
- LangGraph checkpoint 존재·version·interrupt 위치

terminal public snapshot이 있으면 job을 다시 실행하지 않는다. Tool 호출 결과와 후속 checkpoint의
관계가 불명확하면 결과를 exactly-once로 간주하지 않는다. 현재 MCP Tool은 모두 읽기 전용이므로
정책상 허용된 recovery에서는 같은 logical call이 다시 호출될 수 있으며 외부 의미는
**at-least-once**다. duplicate 가능성은 attempt와 event로 남긴다.

쓰기 Tool은 provider-side idempotency와 사용자 승인 계약 없이 등록하지 않는다. Agent의
`Idempotency-Key`는 중복 run 생성을 막는 계약이지 MCP side effect의 exactly-once 보장이 아니다.

Phase 4B-2 single-worker 정책은 다음처럼 보수적으로 고정한다.

- terminal run event와 public snapshot이 모두 있으면 graph를 다시 호출하지 않고 job만 수렴시킨다.
- 교체 claim에 cursor가 없고 같은 attempt의 checkpoint와 open Tool도 없을 때만 입력부터 재실행한다.
- 저장한 cursor, run/thread/attempt correlation과 실제 checkpoint가 모두 일치할 때만 그 checkpoint를
  재개한다. checkpoint의 execution job ID가 같고 저장 generation이 새 lease generation보다 작아야
  교체 claim의 recovery cursor로 인정한다.
- `tool.started` 뒤 terminal Tool event가 없는 모호한 구간은 자동 재호출하지 않는다. job을
  `WORKER_RECOVERY_AMBIGUOUS`로 종료하고 API는 `EXECUTION_RECOVERY_REQUIRED`를 반환한다. 내부 run과
  ledger는 조사·삭제를 위해 보존한다.

### 6. private job payload를 운영 event와 분리하고 terminal에서 scrub한다

worker가 start 또는 resume을 수행하려면 정규화된 질문이나 clarification 답변이 필요하다. 이 값은
다음 조건을 만족하는 private job payload에만 저장한다.

- `run_events`, `tool_executions`, 일반 JSON log, metric label과 오류 envelope에는 복제하지 않는다.
- Phase 4B-1은 schema와 크기가 제한된 private JSONB로 저장한다. hosted PostgreSQL은 verified TLS와
  database at-rest encryption 확인이 없으면 coordination substrate를 시작하지 않는다.
- application-level authenticated encryption과 별도 key rotation 계약은 job을 실제 API/worker
  경계로 전달하는 Phase 4B-2의 활성화 선결 조건이다.
- job이 completed 또는 failed로 확정되거나 clarification snapshot으로 안전하게 전환되면 같은
  terminal transaction에서 private payload를 scrub한다. idempotency 판별에 필요한 keyed fingerprint와
  최소 coordination metadata만 parent thread lifecycle까지 남긴다.
- thread 삭제는 schema v8 deletion fence를 먼저 예약한다. queued/leased job이 있으면 거부하고,
  fence가 설정된 thread의 새 start/resume reservation도 거부한다. 그 뒤 checkpoint를 정리하고 같은
  token으로 metadata를 삭제해 별도 worker가 checkpoint를 다시 쓰는 orphan race를 막는다.
  idempotency record, job metadata와 남아 있는 private payload는 owner-scoped cascade한다.

memory backend는 contract unit test용일 뿐 durable queue나 restart recovery를 제공한다고 표시하지
않는다. hosted worker mode는 PostgreSQL에서만 허용한다.

Phase 4B-2는 private payload를 AES-256-GCM으로 암호화한다. AAD에는 job, owner, correlation,
operation과 payload schema를 포함해 다른 row나 scope로 ciphertext를 옮길 수 없게 한다. active key와
decrypt-only previous keyring을 지원하고, queued/leased job의 key fingerprint를 startup에 검사한다.
schema v7 전환 시 암호화 없이 남아 있던 기존 active job은 자동 실행하지 않고 명시적 migration
failure code로 종료·scrub한다. completed/failed 전환에서는 run snapshot, terminal event, job 상태와
암호문 제거를 같은 transaction으로 확정한다.

claim 시 ciphertext 인증 또는 payload schema 검증에 실패한 poison job은 안전한 failure code로
terminal 전환하고 ciphertext를 같은 transaction에서 scrub한다. worker는 다음 job을 계속 claim하며
복호화 오류 detail이나 payload를 log·public error에 노출하지 않는다.

### 7. 공개 계약과 기존 실행 경로를 보존한다

Phase 4B-1은 기존 path와 response schema v3/v4를 유지하며 public header 계약도 바꾸지 않는다.

- `POST /v1/threads/{thread_id}/runs`: 기존 request를 동기 실행하며 durable job을 만들지 않는다.
- `POST /v1/runs/{run_id}/resume`: 기존 resume 경로를 그대로 사용한다.
- `GET /v1/runs/{run_id}`: 마지막 확정 public snapshot 의미를 유지한다.
- public `run_status`에 `queued`, `running` 또는 `recovering`을 추가하지 않는다.
- 내부 job, lease, fencing token과 private payload field를 OpenAPI response나 Web state에 노출하지
  않는다.

Phase 4B-2에서 BFF가 mutation별 key를 생성·전달하고 transport ambiguity 동안 동일 key를 Web state에
보존하는 호환 release를 구현했다. Web은 POST를 자동 재전송하지 않고 가능한 경우 GET으로 확정
snapshot만 조정한다. Agent API header는 호환 기간 동안 optional이며 key 없는 기존 client의 폐기와
key 필수화는 별도 API version 결정 뒤에만 수행한다.

## 단계별 release gate

### Phase 4B-1 gate

- 동시 같은-key 요청 N개가 run, `run.started`, idempotency record와 job을 각각 하나만 만든다.
- 같은 key의 다른 payload는 graph/MCP 호출 없이 409로 끝나고 owner scope를 넘지 않는다.
- reservation 중 어느 write가 실패해도 thread pointer, run count, run, event, job과 idempotency
  record가 모두 rollback된다.
- concurrent claim은 한 lease만 성공하고 fencing token은 단조 증가한다.
- 만료되거나 교체된 token의 queue heartbeat·complete·fail·release compare-and-set이 거부된다.
- private JSONB가 closed schema와 크기 제한을 통과하고 terminal 및 thread 삭제 뒤 scrub된다. key,
  질문, 답변과 Tool payload가 event·log·error에 남지 않는다.
- 기존 start → clarification → resume → result의 schema v3/v4, BFF와 Web E2E가 그대로
  통과한다.
- 단일 process·단일 replica 제한과 자동 recovery 비활성 상태가 Compose와 운영 문서에 일치한다.

### Phase 4B-2 gate

- API restart 뒤에도 같은 key가 기존 job에 결합하고 새 run을 만들지 않는다.
- API bounded wait와 worker 처리 결과가 기존 public snapshot으로 수렴한다.
- worker를 graph 전, checkpoint 전후, `tool.started` 후, MCP 응답 후와 finalize 전후에 종료하는
  fault injection을 통과한다.
- lease expiry와 checkpoint cursor 분류 뒤 같은 run/checkpoint에서만 재개하고 terminal event와
  public snapshot을 중복 확정하지 않는다.
- unknown Tool window의 재호출 가능성과 at-least-once 의미가 test assertion과 runbook에 남는다.
- MCP 내부 retry와 Agent recovery가 무제한으로 증폭되지 않는다.

### Phase 4B-3B core chaos gate

- 실제 PostgreSQL 복수 worker 경쟁에서 하나의 claim만 성공한다.
- lease generation 교체 뒤 이전 claimant의 heartbeat·release·Tool append·run finalize가 모두
  거부되고 DB row/event count가 바뀌지 않는다.
- checkpoint write가 current lease row를 잠그는 동안 reclaim이 먼저 진행되지 않으며, 교체 뒤 stale
  checkpoint write는 delegate saver를 호출하기 전에 거부된다.
- 별도 subprocess를 `after_claim` 경계에서 hard-kill하면 lease expiry 뒤 generation 2 claimant가
  checkpoint·open Tool이 없는 START 안전 구간만 입력부터 재실행한다. terminal snapshot은 한 번만
  확정되고 private payload는 scrub된다.
- auto identity Worker 두 개가 서로 다른 exact registration으로 동시에 ready가 된다.
- 두 Worker가 ready인 실제 vertical 질문은 최초 claim 하나로
  `generation = claim_count = 1`에 완료되고 private payload가 scrub된다.
- Worker 하나의 단발 stop/start 동안 다른 Worker는 healthy·ready를 유지한다. Agent container만
  recreate할 때 두 Worker container ID는 유지되고, 중단된 run 조회와 기존 clarification resume이
  완료된다.
- open Tool이 있는 모호한 구간은 성공으로 수렴시키거나 자동 replay하지 않고 기존
  `WORKER_RECOVERY_AMBIGUOUS` fail-closed 계약을 유지한다.

### Phase 4B-3C soak 자동화와 replica release gate

`scripts/run_replica_release_gate.py`와 PowerShell wrapper가 local/test Compose를 대상으로 다음 계약을
자동화한다.

- preflight는 Agent·Worker·PostgreSQL·Web이 각각 1개이고 healthy인지, Web public status가
  operational인지, container가 현재 Compose project와 working directory 소유인지 확인한다.
- Agent는 전체 실행 동안 1개로 고정하고 Worker만 2개로 확장한다.
- 두 Worker를 모두 graceful stop한 상태의 신규 mutation이 run/event/job을 남기지 않고 정확히
  retryable `503 EXECUTION_WORKER_UNAVAILABLE`로 끝나는지 확인한 뒤 Worker 2개를 복구한다.
- workload는 독립 session lane에서 trip 질문을 반복하며 매 다섯 번째 workflow의 structured
  clarification·resume을 포함한다. `completed`만 성공 workflow로 집계한다.
- Worker rolling은 workload와 겹친다. peer Worker container 안의 long-lived watcher가 process-local
  token·generation과 PostgreSQL registry의 exact fenced registration을 DB clock 기준으로 250ms마다
  확인한다. Agent recreate에는 두 Worker를 같은 방식으로 감시한다. rolling 중 최소 ready Worker 1개,
  steady 구간 ready Worker 2개를 요구한다.
- Agent recreate는 저장된 clarification을 GET·resume할 수 있고 두 Worker container가 교체되지
  않는지 확인한다.
- PostgreSQL은 기본 20초 동안 중지한다. outage 중 POST는 자동 재전송하지 않고 정확히 retryable
  `503 PERSISTENCE_UNAVAILABLE`이어야 하며, 복구 뒤 DB·Worker·Web과 queue가 다시 수렴해야 한다.
- Agent container 안의 long-lived `execution_health_watch` stream이 DB-clock queue aggregate를 smoke
  2초, release 5초 간격으로 표본화한다. Agent recreate 뒤에는 stream을 재시작하고 판정마다 fresh
  표본을 요구한다. 실행 창 전체의 job, Worker registry, recovery, terminal payload/event 불변식도
  closed database evidence schema로 수집한다.
- cleanup은 먼저 PostgreSQL을 복구하고 Worker를 시작 시점의 1개로 축소한 뒤 제품 health·registry를
  확인하고 생성한 thread를 각 owner session으로 삭제한다. cleanup 실패는 전체 gate 실패다.

profile은 versioned code 상수로 고정한다.

| profile | 관측 시간 | 예정 workflow / 평가 하한 | workload | Worker roll | Agent recreate | PostgreSQL outage | release eligible |
|---|---:|---:|---|---:|---:|---:|---|
| `smoke` | 600초 | 240 / 30 | 동시 2, 5초 간격 | 3 | 2 | 1 | 항상 아니요 |
| `release` | 14,400초 | 2,880 / 600 | 동시 4, 20초 간격 | 12 | 6 | 3 | 모든 gate 통과 시 |

공통 budget은 queue warning 15초, queue failure 60초, stuck 120초, recovery convergence 90초,
final drain 120초다. queue failure breach, expired lease, stuck observation, 예상하지 않은 recovery,
non-database readiness loss, terminal ciphertext 잔존, 중복 terminal, ambiguous recovery 또는 drain 후
queued/leased job은 허용하지 않는다. release profile의 시간·workflow·장애 횟수 하한은 낮출 수 없고
image build 생략과 20초 미만 PostgreSQL outage도 허용하지 않는다.

report는 profile contract, Agent 1·Worker 2 scope, Compose SHA-256과 image build provenance, aggregate
workload/scenario/observer/database evidence, stable violation code와 cleanup 결과만
`artifacts/release-gates/phase-4b-3c-<UTC>.json`에 atomic write한다. ID, 질문·답변, user message,
payload·ciphertext, token, command output과 raw log는 recursive denylist로 거부한다.

smoke 통과는 runner의 짧은 통합 검증일 뿐 `release_eligible=false`다. Worker 기본 replica를 2개로
변경하려면 정식 4시간 release profile의 통과 report와 수동 evidence 검토가 모두 필요하다. 새 watcher
경로의 smoke 재검증은 대기 중이고 정식 release profile도 미실행·미승인이므로 현재 Compose 기본값은
Agent 1개·Worker 1개다.

## 비범위

- SSE, `Last-Event-ID`, public processing timeline과 `/ops`
- cancel API와 cancellation propagation
- OIDC, RBAC와 운영자 audit surface
- LLM runtime 활성화, context reduction과 장기 memory
- 원본 MCP Tool artifact 저장 또는 Tool 결과 재사용
- Redis, Celery, Kafka나 별도 queue 제품
- Kubernetes, autoscaling과 multi-region execution
- stateless `/v1/accessibility/questions`의 durable ledger 전환
- end-to-end exactly-once 주장
- 사용자 UI의 새 기능 또는 processing 화면

## 결과

### 장점

- 공개 응답과 사용자 흐름을 바꾸지 않고 중복 방지와 실행 소유권 불변식을 먼저 검증할 수 있다.
- PostgreSQL run ledger, durable job과 checkpoint의 권위를 분리해 crash window를 숨기지 않는다.
- worker 활성화, multi-replica correctness와 replica 운영 승인을 서로 다른 release gate로 분리해
  stale write와 성급한 수평 확장 위험을 줄인다.
- private input의 저장 위치와 삭제 시점을 명시해 운영 event의 redaction 계약을 유지한다.

### 비용과 제한

- worker가 비활성인 Phase 4B-1에서도 queue·idempotency row와 lease coordination 코드가 추가된다.
- BFF durable 요청은 transport-level 중복 방지를 받지만 key 없는 호환 동기 요청은 받지 못한다.
- legacy `finalize_run`은 queue fencing token을 받지 않는다. worker는 lease-bound Tool writer와
  `finalize_execution`만 사용하며 key 없는 호환 동기 경로와 replica release 제한을 유지한다.
- application encryption은 active/previous keyring rotation과 migration 절차를 지속적으로 운영해야
  한다.
- fencing은 PostgreSQL metadata write를 보호하지만 MCP 호출을 transaction으로 만들지 못한다.
- checkpoint fence는 lease row transaction을 유지한 채 saver connection을 하나 더 사용하므로
  PostgreSQL pool max size를 2 이상으로 유지해야 한다.

## 기각한 대안

### queue·worker·multi-replica를 한 번에 활성화

API 응답, recovery와 실행 소유권이 동시에 바뀌어 실패 원인과 rollback 경계를 검증하기 어렵기
때문에 선택하지 않았다.

### public `ConversationRun`에 processing 상태 추가

사용자 결과와 내부 실행 상태를 섞고 schema v3/v4 및 현재 Web을 깨므로 선택하지 않았다.

### checkpoint를 durable queue로 사용

checkpoint는 중단된 HTTP mutation, idempotency scope, lease owner와 fencing token을 제공하지 않으므로
선택하지 않았다.

### lease owner ID만으로 stale process 차단

process identity 재사용과 delayed write를 막지 못하므로 증가하는 fencing token을 필수로 한다.

### raw input을 `run_events` payload에 저장

Phase 4A의 strict allowlist·redaction 계약과 충돌하고 event retention을 private input lifecycle과
결합하므로 선택하지 않았다.

## 후속 작업

1. 새 long-lived watcher 경로로 3C smoke profile을 재검증한 뒤 정식 4시간 release profile을 실행하고
   redacted report와 수동 복구 절차를 검토한다.
2. release profile이 통과한 뒤에만 Worker 기본 replica를 2개로 바꾸는 별도 배포 변경을 결정한다.
3. key 없는 호환 동기 경로를 제거하거나 durable 경로로 통합한 뒤 Agent replica 확대를 별도 gate와
   ADR로 결정한다.
4. SSE와 public processing contract는 durable worker 안정화 뒤 별도 ADR에서 결정한다.
