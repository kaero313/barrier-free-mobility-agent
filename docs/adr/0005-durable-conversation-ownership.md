# ADR-0005: Durable conversation과 owner 경계

- 상태: Accepted
- 날짜: 2026-07-11
- 범위: thread/run metadata, LangGraph checkpoint, local/demo Web session과 hosted hardening 전제

## Context

LangGraph checkpointer만 PostgreSQL로 바꾸면 graph state는 남지만 공개 `run_id` 조회, active
clarification과 thread owner mapping은 process memory에서 사라진다. 반대로 run metadata만
저장하면 interrupt 위치가 없어 resume할 수 없다. 두 저장 계층을 함께 전환해야 재시작 후
동일한 API 계약을 유지할 수 있다.

same-origin BFF도 그 자체로 object authorization을 제공하지 않는다. public thread/run ID를
아는 다른 browser session이 Agent에 접근할 수 있으면 다른 사용자의 이동 조건과 결과가
노출될 수 있다. 다만 현재 단계에는 OIDC end-user identity provider가 없다.

## Decision

### 저장 계층

- local unit/native 기본값은 `InMemorySaver`와 `MemoryConversationRepository`다.
- 통합 Compose와 hosted 설정은 `AsyncPostgresSaver`와
  `PostgresConversationRepository`를 같은 application lifespan에서 연다.
- `agent_threads`는 owner, active run, run count, flow/state version을 저장한다.
- `agent_runs`는 공개 run snapshot의 `outcome`과 `context`를 JSONB로 저장하고
  `payload_schema_version`을 decode 전에 확인한다. 지원하지 않는 version은 호환되지 않는
  Agent state로 거부한다.
- checkpoint와 metadata migration을 startup에서 실행할 수 있다. hosted application role에
  DDL 권한을 주지 않는 배포는 별도 migration job 이후 `DATABASE_AUTO_SETUP=false`로
  실행한다. application startup은 repository/checkpointer의 exact migration version,
  필수 relation과 index validity를 검증하고 readiness는 data table의 DML 권한을 확인한다.
- checkpoint serializer는 pickle fallback과 임의 module 역직렬화를 끈 strict msgpack/JSON을
  사용한다. serializer를 통과하는 payload는 선택적으로 AES로 보호하고 key fingerprint가
  DB와 다르면 startup에 실패한다. saver의 scalar/metadata와 run JSONB까지 전부
  application-level 암호화되는 것은 아니다.
- staging/production은 AES key, `sslmode=verify-full` DB transport와 운영 플랫폼의 at-rest
  encryption 확인을 모두 요구하며 하나라도 충족하지 않으면 startup 설정 검증에 실패한다.
- readiness는 MCP와 persistence가 모두 ready일 때만 전체 ready다.

### owner namespace

- Web은 32-byte random nonce를 HMAC-SHA256으로 서명한 HttpOnly, SameSite=Lax
  browser-session cookie를 사용한다. staging/production cookie에는 Secure와 `__Host-`
  prefix를 적용하고 canonical public Origin을 mutation마다 확인한다.
- cookie nonce에서 별도 HMAC domain으로 64자리 opaque owner ID를 파생한다. owner ID와
  BFF shared secret은 browser에 노출하지 않는다.
- BFF는 Agent로 중계하는 요청에 `X-Agent-Owner-ID`와 `X-Agent-Internal-Token`을
  덮어써 전달한다. browser가 보낸 같은 이름의 header는 신뢰하지 않는다.
- Agent는 private network의 BFF만 shared-token으로 신뢰한다. owner header 누락, 중복,
  형식 오류와 token 오류는 같은 401을 반환한다.
- repository 조회는 owner를 SQL 조건에 포함한다. 다른 owner의 객체는 존재하지 않는 객체와
  같은 thread/run 404를 반환한다.
- LangGraph 내부 checkpoint thread key는 `<owner_id>:<public_thread_id>`다. public API에는
  32자리 thread ID만 노출한다.
- delete는 owner metadata를 먼저 확인한 뒤 해당 owner checkpoint와 metadata를 삭제한다.

### 공개 계약

- 기존 thread/run path와 response schema 3은 유지한다.
- thread 생성 response의 `persistence`는 `ephemeral | durable`이다.
- readiness dependency에 `persistence.status`와 `persistence.backend`를 추가한다.
- 내부 owner/token header는 OpenAPI parameter에 노출하지 않는다.
- `API_OWNER_MODE=trusted_header`에서는 stateful API 5종뿐 아니라 one-shot
  `/v1/accessibility/questions`도 내부 owner/token 인증 없이는 401이다.

## Consequences

### Positive

- process 재시작 뒤 기존 run을 조회하고 같은 clarification을 resume할 수 있다.
- API metadata와 graph checkpoint가 같은 owner 경계를 사용한다.
- memory와 PostgreSQL adapter가 같은 application contract를 구현해 unit test와 제품 실행의
  의미 차이를 제한한다.
- strict serializer, secret validation, no-store BFF와 404 정책을 자동 테스트할 수 있다.

### Negative와 제한

- checkpoint write와 metadata write는 하나의 DB transaction이 아니다. graph 완료 뒤
  metadata 저장 전 process가 죽는 crash window가 남는다.
- per-thread lock은 현재 Agent process 내부 lock이다. Phase 4 lease 전에는 Agent replica를
  하나만 실행한다.
- anonymous owner는 browser session 격리 수단일 뿐 실제 사용자 인증이나 account recovery를
  제공하지 않는다.
- owner별 quota 외에 전체 thread capacity도 원자적으로 제한하지만, cookie 재발급을 이용한
  요청 rate 우회는 ingress/BFF rate limit과 TTL cleanup 전까지 남는다.
- anonymous-session secret을 즉시 교체하면 기존 cookie의 owner를 복원할 수 없어 이전
  thread가 orphan될 수 있다. hosted 전에는 key ring/rotation과 TTL cleanup이 필요하다.
- 현재 Web은 thread history/detail API가 없어 page refresh만으로 persisted transcript를
  복원하지 못한다.
- BFF server session과 TTL cleanup은 아직 없다. 삭제 실패 ID를 refresh 뒤 복구하거나
  orphan thread를 자동 정리한다고 보장하지 않는다.
- 장기 memory, Tool artifact retention, idempotency와 SSE replay는 이 ADR 범위가 아니다.

## Rejected alternatives

### Checkpointer만 PostgreSQL로 전환

공개 run ID와 active run registry가 재시작 때 사라져 API resume이 불가능하므로 선택하지
않았다.

### Browser가 owner header를 직접 보냄

변조와 객체 탈취를 막을 인증 근거가 없으므로 선택하지 않았다.

### public thread ID를 checkpoint key로 그대로 사용

repository authorization 하나에만 의존하고 저장 namespace의 방어 계층이 없어 선택하지
않았다.

### Redis를 conversation source of truth로 사용

초기 backup, relation constraint, migration과 run 조회 요구를 PostgreSQL 하나로 해결하기로
했으므로 선택하지 않았다.

## Follow-up

1. OIDC authorization-code session subject를 현재 opaque owner repository에 연결한다.
2. thread history/detail과 active run recovery API를 추가한다.
3. Phase 4에서 실행 전 run ID, idempotency key, DB lease와 recovery sweeper를 구현한다.
4. encryption key rotation과 data migration runbook을 추가한다.
