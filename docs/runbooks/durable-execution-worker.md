# Durable execution worker 운영 runbook

## 1. 적용 범위

이 문서는 Phase 4B-3B core safety contract를 사용하는 단일 Agent API·단일 worker 일반 운영 구성과
Phase 4B-3C에서 Worker 2개의 release 적합성을 검증하는 제한된 Compose gate에 적용한다. API는
start/resume mutation을
PostgreSQL job으로 원자적으로 예약하고, 별도 `mobility-agent worker` process가 lease를 claim해
LangGraph와 MCP를 실행한다. API는 확정된 public `ConversationRun`만 bounded wait한다.

Worker readiness는 public API가 아니라 PostgreSQL registry, `mobility-agent worker-health`와 Docker
healthcheck로 API readiness와 분리해 확인한다. 3C gate의 고정 검증 scope는 Agent 1개·Worker 2개지만
새 long-lived watcher 경로의 smoke 재검증은 대기 중이고 정식 4시간 release profile도
미실행·미승인이다. 따라서 일반 운영 기본값은 Agent 1개·Worker 1개다. 현재 범위에는
public processing status, `202`, cancel, SSE와 `/ops`가 없다. MCP Tool은 읽기 전용이지만 crash
경계에서 호출 의미는 at-least-once다. end-to-end exactly-once로 해석하지 않는다.

## 2. 기동 전 조건

- PostgreSQL repository와 LangGraph checkpoint migration이 최신이어야 한다.
- API와 worker에 같은 `EXECUTION_PAYLOAD_ENCRYPTION_KEY` keyring을 주입한다.
- active payload key는 정확히 32 UTF-8 bytes다.
- `IDEMPOTENCY_HMAC_KEY`는 최소 32 bytes이며 payload/checkpoint key와 다른 값을 사용한다.
- 이전 payload key가 있으면 JSON array 형태의
  `EXECUTION_PAYLOAD_PREVIOUS_ENCRYPTION_KEYS`에 decrypt-only로 둔다.
- worker heartbeat의 두 배가 lease보다 짧아야 한다.
- worker heartbeat의 두 배가 `EXECUTION_WORKER_READINESS_TIMEOUT_SECONDS`보다 짧아야 한다.
- lease guard가 connection 하나를 유지한 채 LangGraph saver가 별도 connection을 사용하므로
  PostgreSQL `DATABASE_POOL_MAX_SIZE`는 최소 2다.
- Phase 4B-3C runner 밖에서는 정식 release 승인 전까지 API와 worker를 각각 하나만 실행한다.

로컬 통합 제품은 다음처럼 시작한다.

```powershell
docker compose config --quiet
docker compose up --build
```

API의 HTTP readiness와 Worker readiness는 합치지 않는다. Compose Worker의 기본
`EXECUTION_WORKER_ID=auto`는 container hostname에서 replica별 ID를 만들며, 명시적으로 설정한 ID는
그대로 유지한다. 전용 read-only probe는 Settings, LangGraph, MCP나 migration을 로드하지 않고
`/tmp/mobility-agent-worker-registration.json`의 mode-0600 process-local token·generation과 PostgreSQL
registry row가 정확히 일치하며 `ready`이고 DB clock 기준 만료 전인지 확인한다. stale process는 다른
process의 같은 worker ID row만으로 healthy가 될 수 없다. `mobility-agent worker-health`는 같은 조건의
수동 점검 명령이다.

```powershell
docker compose ps agent worker postgres web
docker compose exec worker mobility-agent worker-health
docker compose logs --tail 100 worker
```

정상 worker log에는 `Durable execution worker ready`가 한 번 나타난다. 질문·답변, idempotency key
원문, 암호문이나 Tool payload를 log 검색 조건으로 사용하지 않는다.

Worker registration이 교체되거나 내부 loop가 예상치 않게 끝나면 process는 state file을 제거하고
non-zero로 종료한다. orchestrator는 이 종료를 새 Worker process로 복구해야 하며, 같은 container에서
stale loop를 대기 상태로 유지하지 않는다. 수동으로 `EXECUTION_WORKER_ID`를 주입해 여러 replica를
실행할 때는 반드시 replica마다 다른 값을 사용한다. 자동 ID와 core fencing이 scale-safe하더라도
정식 4시간 release profile 승인 전의 일반 운영 Worker replica 제한은 계속 1개다. Agent replica는
key 없는 호환 동기 경로가 process-local인 동안 3C 승인 대상이 아니며 계속 1개다.

로컬 Compose는 이 fail-closed 종료를 방치하지 않도록 Worker에 bounded `on-failure:5` 재시작을
적용한다. 재시작 뒤에도 process-local token·generation과 PostgreSQL registration이 일치해야만
healthcheck를 통과하므로 restart policy가 stale process를 ready로 되돌리지는 않는다. 실제 배포에서는
같은 책임을 배포 orchestrator의 restart budget과 backoff로 옮긴다.

## 3. 정상 실행 의미

1. BFF는 start/resume 제출마다 opaque UUID key를 한 번 만든다.
2. Worker는 idle 상태에서도 fenced registry heartbeat를 갱신한다.
3. API는 replay를 먼저 확인하고, 신규 예약이면 같은 transaction에서 ready Worker row를 잠근 뒤
   owner·operation·target 범위의 HMAC digest, request fingerprint, run/event/job을 예약한다.
4. worker는 자기 process-local token·generation과 같은 exact ready·fresh registration으로만 claim한다.
   repository는 registration row lock과 PostgreSQL server clock을 사용해 교체된 process를 거부한다.
5. claim은 증가하는 lease generation을 발급하고 job heartbeat가 lease를 연장한다. 같은 attempt의
   latest checkpoint cursor만 기록한다.
6. checkpointer adapter는 모든 async saver access를 process-local outer lock으로 직렬화한다. write는
   outer lock을 먼저 얻고 current lease row를 잠근 transaction 안에서 실행되며 checkpoint에는
   correlation, execution job ID와 lease generation provenance가 남는다. 이 lock order는 saver 내부
   lock과 PostgreSQL pool/lease lock의 inversion을 막고, cross-process fencing은 DB guard가 담당한다.
7. Tool event append는 현재 token·generation과 만료 전 lease를 transaction 안에서 다시 확인한다.
8. run snapshot/event, job terminal 상태와 encrypted payload scrub은 한 transaction으로 확정된다.
9. 같은 key·같은 request는 기존 job/result에 결합하고, 다른 request는
   `IDEMPOTENCY_KEY_REUSED`로 거부된다.

ready Worker가 없으면 신규 start/resume은 retryable `EXECUTION_WORKER_UNAVAILABLE` 503으로 끝난다.
이 응답은 reservation transaction이 rollback됐다는 확정 결과이므로 POST 결과 불명으로 분류하거나
GET 조정을 시작하지 않는다. Worker가 사라진 뒤에도 이미 존재하는 같은 key의 replay는 기존
job/result에 결합할 수 있다.

bounded wait를 넘으면 API는 retryable `EXECUTION_IN_PROGRESS`를 반환한다. 이는 job 실패나 취소가
아니다. 기본 wait는 60초로 MCP Tool timeout 45초와 graph 정리 시간을 포함하면서 BFF의 90초
deadline보다 짧게 둔다. BFF는 POST를 자동 재전송하지 않으며, 같은 mutation key를 보존한다.
Web은 run ID를 이미 아는 resume이면 제한된 횟수의 GET으로 snapshot을 조정한다. start처럼 run ID를
모르는 경우에는 자동 POST를 하지 않고 사용자가 `접수한 요청 결과 확인`을 선택했을 때만 같은
mutation key와 같은 payload로 기존 예약에 다시 결합한다. 새 key로 우회하거나 무조건 composer를
해제하지 않는다.

## 4. 최소 안전 점검

운영 점검에서는 private payload column을 조회하지 않는다. 상태와 age만 집계한다.

우선 low-cardinality aggregate CLI를 사용한다. 두 명령은 PostgreSQL을 읽기 전용으로 조회하며 ID,
owner, 질문·답변, payload, token을 출력하지 않는다. release evidence의 시작 시각은 timezone이 포함된
ISO 8601 값이어야 한다.

```powershell
docker compose exec -T agent mobility-agent execution-health --stuck-after-seconds 120
docker compose exec -T agent mobility-agent replica-release-evidence --window-started-at 2026-07-16T00:00:00+00:00
```

아래 SQL은 장애 조사에서 개별 lease 상관관계가 꼭 필요할 때만 제한된 운영자 권한으로 사용한다.

```sql
SELECT
    worker_id,
    state,
    generation,
    heartbeat_at,
    expires_at,
    state = 'ready' AND expires_at > clock_timestamp() AS fresh_ready
FROM execution_worker_registry
ORDER BY worker_id;

SELECT state, count(*)
FROM run_execution_jobs
GROUP BY state
ORDER BY state;

SELECT
    job_id,
    run_id,
    operation,
    state,
    lease_generation,
    claim_count,
    available_at,
    lease_expires_at,
    heartbeat_at,
    failure_code
FROM run_execution_jobs
WHERE state IN ('queued', 'leased', 'failed')
ORDER BY updated_at ASC
LIMIT 100;
```

다음 신호를 함께 본다.

- `docker compose ps worker`가 unhealthy이거나 `mobility-agent worker-health`가 non-zero다.
- `execution_worker_registry`에 ready·unexpired row가 없고 신규 mutation의
  `EXECUTION_WORKER_UNAVAILABLE` 비율이 증가한다.
- queued age가 bounded wait와 정상 Tool latency를 계속 초과한다.
- leased row의 heartbeat가 멈추고 `lease_expires_at`이 PostgreSQL 현재 시각보다 과거다.
- `WORKER_EXECUTION_FAILED` 또는 `WORKER_RECOVERY_AMBIGUOUS`가 증가한다.
- API의 `EXECUTION_IN_PROGRESS` 비율이 지속적으로 증가한다.
- worker loop의 `WORKER_LOOP_ERROR`, 저장소 readiness 실패 또는 MCP contract 초기화 실패가 있다.

## 5. 장애 분류와 조치

### worker process 종료

1. API와 PostgreSQL은 그대로 두고 Worker Docker health와 구조화 log의 마지막
   job/run/attempt correlation을 확인한다. public `/health/ready`만으로 Worker 생존을 판단하지 않는다.
2. 신규 mutation은 ready heartbeat가 사라진 뒤 503으로 fail closed하며 새 run/job을 만들지 않는다.
3. 새 worker 하나만 시작하고 `mobility-agent worker-health`가 성공할 때까지 traffic 복구를 확정하지
   않는다.
4. 기존 lease가 만료되기 전에는 stale worker와 새 worker가 동시에 write할 수 없다. 수동으로
   `lease_expires_at`, token이나 generation을 수정하지 않는다.
5. 새 claim은 terminal snapshot, checkpoint cursor와 open Tool attempt를 분류한다.

cursor가 없고 같은 attempt의 checkpoint·open Tool이 모두 없으면 START 안전 구간으로 분류해
입력부터 재실행할 수 있다. cursor, correlation, execution job ID가 일치하고 checkpoint의 lease
generation이 현재 교체 claim보다 작은 경우에만 해당 checkpoint를 재개한다. terminal snapshot이
이미 있으면 graph를 재호출하지 않고 job만 terminal로 수렴시킨다.

### `WORKER_RECOVERY_AMBIGUOUS`

`tool.started` 뒤 terminal Tool event가 없는 상태다. 외부 MCP 호출이 수행됐는지 증명할 수 없으므로
worker는 자동 replay하지 않는다. job만 failed/scrub하고 run ledger와 active thread는 조사 대상으로
보존한다. API는 `EXECUTION_RECOVERY_REQUIRED`를 반환한다.

1. run event와 MCP 측 request/correlation log를 확인하되 사용자 질문이나 payload를 복호화해
   일반 log에 남기지 않는다.
2. 현재 Tool이 읽기 전용인지와 실제 side effect 부재를 확인한다.
3. 자동 requeue나 DB row 직접 수정은 하지 않는다.
4. local/demo에서는 사용자가 해당 대화를 삭제한 뒤 새 질문으로 제출하도록 안내한다.
5. 반복되면 worker를 drain하고 crash 지점과 heartbeat/Tool timeout을 먼저 수정한다.

쓰기 Tool은 provider-side idempotency와 사용자 승인 계약을 별도로 구현하기 전에는 이 recovery
경로에 등록하지 않는다.

### `PAYLOAD_ENCRYPTION_MIGRATION_REQUIRED`

schema v7 이전에 평문으로 남아 있던 queued/leased job을 migration이 자동 실행하지 않고 failed로
전환·scrub한 상태다. 원문 payload는 복구하지 않는다. 관련 thread/run을 확인한 뒤 대화를 삭제하고
새 mutation으로 다시 제출한다.

### `EXECUTION_PAYLOAD_INCOMPATIBLE`

claim 시 AES-GCM 인증, AAD 또는 payload schema 검증에 실패한 job이다. worker는 해당 job을
terminal failed로 격리하고 ciphertext를 scrub한 뒤 다음 job을 계속 처리한다. 원문이나 복호화 오류
detail을 복구·출력하지 않는다. 반복 발생하면 keyring 불일치, 저장 장치 손상 또는 비정상 DB 수정을
조사하고 새 요청을 받기 전에 원인을 제거한다.

### payload keyring 불일치

queued/leased job의 fingerprint를 현재 keyring으로 해독할 수 없으면 API/worker startup이
fail closed한다. 임의의 새 key로 우회하지 않는다. secret manager의 직전 active key를
`EXECUTION_PAYLOAD_PREVIOUS_ENCRYPTION_KEYS`에 복구하고 다시 시작한다.

### thread 삭제가 `THREAD_BUSY`인 경우

queued/leased job이 있는 thread는 checkpoint orphan을 막기 위해 삭제를 시작하지 않는다. job이
terminal이 된 뒤 다시 삭제한다. 삭제 준비가 시작되면 schema v8 fence가 새 start/resume reservation을
막고 checkpoint cleanup 뒤 같은 token으로 metadata를 cascade한다. checkpoint cleanup이 실패하면
fence를 유지하므로 같은 owner의 삭제 요청을 재시도한다. DB에서 deletion token을 수동으로 지우거나
thread/job row를 직접 삭제하지 않는다.

## 6. key rotation

1. 새 32-byte active key를 생성한다.
2. 기존 active key를 previous keyring에 넣어 API와 worker에 함께 배포한다.
3. 새 active key로 API와 worker를 각각 하나씩 재시작한다.
4. startup keyring 검증과 queued/leased job 처리가 정상인지 확인한다.
5. 이전 fingerprint를 사용하는 active job이 0개이고 배포 rollback window가 끝난 뒤 previous key를
   제거한다. terminal job에는 ciphertext가 남지 않는다.

HMAC key rotation은 payload key rotation과 별개다. active idempotency record가 남아 있는 동안 HMAC
root를 바꾸면 같은 client key가 기존 job에 결합하지 못하므로 별도 dual-digest migration 계약 없이
즉시 교체하지 않는다.

## 7. graceful shutdown

worker에 `SIGTERM`을 보내면 새 claim을 멈추고 현재 job을
`EXECUTION_WORKER_SHUTDOWN_GRACE_SECONDS` 동안 drain한다. 제한을 넘기면 process task를 취소하고
active lease를 current token·generation compare-and-set으로 revoke한다. 취소를 무시한 task가 늦게
checkpoint, Tool event나 finalize write를 시도해도 revoked lease fence에서 거부된다. 후속 worker는
새 generation과 recovery evidence를 기준으로만 이어간다. lease revoke 자체가 실패하면
`WORKER_LEASE_REVOKE_FAILED`로 기록하고 정상 handoff로 간주하지 않는다.

```powershell
docker compose stop -t 40 worker
```

강제 종료는 fault-injection에서만 사용한다. `ExecutionWorkerFaultInjector`는 Worker constructor에만
주입할 수 있고 Settings나 환경 변수로 활성화할 수 없다. deterministic 경계는 `after_claim`,
`before_graph`, `before_finalize`, `after_finalize`이며 production composition은 injector를 전달하지
않는다. 검증 행렬은 graph 시작 전, checkpoint write/reclaim 경계, Tool window와 finalize 전후를
구분하며 각 지점에서 다음 불변식을 확인한다.

- public terminal snapshot은 한 번만 확정된다.
- stale token의 heartbeat, Tool append와 finalize는 거부된다.
- terminal transaction 뒤 private ciphertext가 없다.
- 모호한 Tool window는 자동 성공이나 자동 replay로 바뀌지 않는다.

Phase 4B-3B core gate에서는 실제 PostgreSQL의 복수 claim 경쟁, generation 교체 뒤 stale
heartbeat·release·Tool append·finalize, checkpoint row-lock과 reclaim 직렬화를 검증했다. 별도
subprocess를 `after_claim`에서 hard-kill한 시나리오는 generation 2가 checkpoint와 open Tool이 없는
START 구간에서만 재실행해 terminal snapshot을 한 번 확정하고 private payload를 scrub했다. Compose
scale에서는 서로 다른 hostname auto identity의 Worker 두 개가 동시에 exact health proof를
통과하는지 확인했다. checkpointer의 outer saver lock → lease row guard 순서는 실제 2-worker vertical
smoke에서 관측한 saver-lock/pool-lock inversion을 제거했고 회귀 테스트로 고정했다. 같은 smoke의
질문은 `generation = claim_count = 1`로 완료되며 private payload가 scrub됐다.

단발 rolling 검증에서는 Worker 하나를 stop/start하는 동안 다른 Worker가 healthy·ready를 유지했다.
Agent container만 recreate했을 때 두 Worker container ID는 바뀌지 않았고, 중단된 run은 계속 조회할 수
있었으며 기존 clarification에 대한 resume도 완료됐다. 이 결과를 장시간 안정성, 반복 rolling 승인이나
exactly-once 보장으로 확대 해석하지 않는다.

## 8. Phase 4B-3C Worker replica release gate

### 실행 전 확인

runner는 장애를 주입하므로 local/test 환경에서만 실행한다. Web origin은 loopback HTTP여야 하고,
현재 Compose project의 Agent·Worker·PostgreSQL·Web이 각각 1개이며 모두 healthy여야 한다. runner는
container의 Compose project, service와 working-directory label을 확인한 뒤에만 stop/restart/recreate를
수행한다. 다른 project container나 hosted 환경에는 사용하지 않는다.

```powershell
docker compose config --quiet
docker compose ps agent worker postgres web
.\scripts\run_replica_release_gate.ps1 -Profile smoke
```

smoke가 runner와 짧은 통합 경로를 확인하더라도 release 승인은 아니다. 정식 판정 명령은 다음과
같지만 4시간 동안 workload와 장애 주입을 지속하므로 유지보수 창과 evidence 검토 담당자를 먼저
확정해야 한다.

현재 새 long-lived Worker/queue watcher를 적용한 smoke 재검증도 완료되지 않았다. 먼저 smoke report와
cleanup을 확인한 뒤에만 정식 판정으로 진행한다.

```powershell
.\scripts\run_replica_release_gate.ps1 -Profile release
```

release profile은 현재 Agent/Worker image를 반드시 다시 build하며 `-SkipBuild`를 허용하지 않는다.
`-PostgresOutageSeconds`를 변경하더라도 release에서는 20초보다 짧게 설정할 수 없다.

### 고정 profile과 budget

| profile | 관측 시간 | 예정 workflow / 평가 하한 | workload | Worker roll | Agent recreate | PostgreSQL outage | release eligible |
|---|---:|---:|---|---:|---:|---:|---|
| `smoke` | 600초 | 240 / 30 | 동시 2, 5초 간격 | 3 | 2 | 1 | 항상 아니요 |
| `release` | 14,400초 | 2,880 / 600 | 동시 4, 20초 간격 | 12 | 6 | 3 | 모든 조건 통과 시 |

공통 threshold는 다음과 같다.

- queued age 15초 초과는 warning, 60초 초과는 failure다.
- stuck 기준은 120초, 장애 후 recovery convergence는 90초, 최종 drain은 120초다.
- steady 구간에는 ready Worker 2개, Worker rolling 중에는 최소 1개를 유지한다.
- expired lease, stuck observation, 예상하지 않은 recovery attempt, non-database readiness loss,
  terminal ciphertext 잔존, terminal event 위반과 ambiguous recovery는 0이어야 한다.
- drain 뒤 queued·leased·expired·stuck job은 0이고 ready Worker는 2개여야 한다.

Agent는 gate 전체에서 1개로 유지한다. gate가 Worker를 2개로 scale하더라도 Agent replica 안전성을
검증하거나 승인하지 않는다.

### 시나리오 의미

1. Worker 2개를 모두 graceful stop하고 ready Worker가 0개인 상태에서 신규 mutation이 정확히
   `503 EXECUTION_WORKER_UNAVAILABLE`인지 확인한다. 이 요청은 run/event/job을 만들지 않는다.
2. 독립 session lane의 trip workflow를 전체 관측 시간 동안 반복하고 매 다섯 번째 workflow는
   structured clarification을 resume한다. 업무 `failed`는 완료로 세지 않는다.
3. Worker rolling은 workload와 겹쳐 수행한다. 대상이 아닌 Worker container 안에서 long-lived
   readiness watcher를 실행해 mode-0600 process-local token·generation과 PostgreSQL registry의 같은
   exact registration이 `ready`이고 DB clock 기준 만료 전인지 250ms마다 확인한다. Agent recreate에는
   두 Worker를 같은 방식으로 감시한다. Docker health polling 결과만으로 이 invariant를 통과시키지 않는다.
4. Agent recreate 전에 clarification을 저장하고 recreate 뒤 같은 run을 조회·resume한다. 두 Worker
   container ID가 바뀌면 실패한다.
5. PostgreSQL을 중지한 동안 POST는 자동 재전송하지 않는다. timeout이나 다른 오류가 아니라 정확히
   retryable `503 PERSISTENCE_UNAVAILABLE`이어야 한다. DB 시작 뒤 Worker 2개, Web operational 상태와
   빈 queue가 90초 안에 수렴해야 한다.

queue 관측은 gate 시작 뒤 Agent container 내부에 long-lived `execution_health_watch` stream 하나를 열어
smoke 2초·release 5초마다 DB-clock aggregate를 받는다. Agent recreate 구간에는 기존 stream 종료를
예상된 gap으로 취급하고 새 Agent에서 다시 연다. 수렴·readiness·최종 drain 판정은 각 호출 뒤 도착한
fresh 표본만 사용하며, stream의 예기치 않은 종료·protocol 오류·cadence 위반은 fail closed한다.

단일 Agent와 단일 PostgreSQL을 의도적으로 내리는 구간에는 새 workload를 먼저 drain한다. 따라서 이
gate는 Agent 또는 DB 중단 중 in-flight exactly-once를 증명하지 않는다. 별도 3B hard-kill lease gate가
active lease fencing을 검증하며 MCP 호출 의미는 계속 at-least-once다.

### report와 판정

report는 다음 경로에 atomic JSON으로 기록된다.

```text
artifacts/release-gates/phase-4b-3c-<UTC>.json
```

report에는 profile contract, Agent 1·Worker 2 scope, Compose SHA-256, image build 여부, aggregate
workload·scenario·observer·database evidence, stable failure/violation code와 cleanup 결과만 들어간다.
job/run/thread/owner/Worker/request ID, 질문·답변, user message, payload·ciphertext, token,
idempotency key, command output과 raw log key는 recursive redaction guard가 거부한다. report에 개별
식별자를 수동으로 추가하지 않는다.

release 승인은 다음을 모두 만족해야 한다.

1. `profile=release`, `result=pass`, `release_eligible=true`다.
2. `failure_codes`와 decision violation code가 비어 있다.
3. database evidence의 failed/active job, expired/draining registration, unowned active lease,
   recovery attempt, terminal payload/event violation과 ambiguity가 모두 0이다.
4. `cleanup.status=pass`이고 복구된 토폴로지가 Agent 1개·Worker 1개이며 PostgreSQL과 Web이 healthy다.
5. 운영자가 report의 profile, Compose digest와 image provenance가 의도한 배포 후보인지 수동 검토한다.

smoke report는 `result=pass`여도 `release_eligible=false`이므로 승인 근거로 사용하지 않는다. 새 watcher
경로의 smoke 재검증은 대기 중이고 정식 4시간 release profile도 미실행·미승인이다.

### 실패·중단 뒤 복구

runner의 `finally` cleanup은 PostgreSQL이 중지돼 있으면 먼저 시작하고 healthy까지 기다린다. 다음으로
Worker를 시작 시점의 1개로 축소하고 queue·registry 수렴, Agent/PostgreSQL/Web health와 Web public
status를 확인한 뒤 생성한 thread를 각 owner session으로 삭제한다. 어떤 cleanup 단계든 실패하면
report의 `cleanup.status=fail`과 stable failure code를 남긴다.

runner가 비정상 종료되어 cleanup을 완료하지 못했으면 새 gate를 바로 재실행하지 말고 다음 순서로
수동 복구한다.

1. `docker compose ps --all`에서 현재 project의 서비스만 식별하고 PostgreSQL을 먼저 시작한다.
2. `docker compose up -d --no-deps --scale worker=1 worker`로 Worker를 기본 1개로 복구한다.
3. Agent·PostgreSQL·Web과 Worker health를 확인한다.
4. `mobility-agent execution-health --stuck-after-seconds 120`에서 ready Worker 1개와 queued/leased/
   expired/stuck 0개를 확인한다.
5. recovery ambiguity나 active job이 남으면 DB row를 직접 수정하지 말고 이 runbook의 장애 분류에
   따라 조사한다. 생성 thread cleanup이 불확실하면 report와 application log의 식별자를 일반 문서에
   복제하지 말고 제한된 운영 경로에서 owner-scoped 삭제를 수행한다.

## 9. 현재 확장 제한

worker process가 별도이고 Phase 4B-3B core chaos gate를 통과했으며 향후 3C smoke를 통과하더라도 수평
확장을 허용하지 않는다. 정식 4시간 release profile 통과와 수동 evidence 검토 뒤에만 Worker 기본값을
2개로 바꾸는 별도 배포 변경을 수행한다. Agent는 key 없는 호환 동기 경로가 process-local인 동안
계속 1개로 유지하며, Agent replica 확대는 별도 계약·gate 대상이다.
