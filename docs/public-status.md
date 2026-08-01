# 공개 서비스 상태 계약

구현 상태: Phase 3C-2 완료 (2026-07-14)

## 1. 목적

`/status`는 사용자가 지금 이동 조회를 시작할 수 있는지와 어떤 질문 범위를 지원하는지
확인하는 public trust surface다. 내부 readiness, 운영 overview나 장애 진단 화면이 아니다.

현재 `GET /health/ready`에는 schema fingerprint, Tool count, persistence backend와 내부 error
code처럼 공개할 필요가 없는 값이 포함된다. Browser에 이 응답을 그대로 전달하지 않고
same-origin BFF가 별도의 allowlist DTO로 변환한다.

## 2. Route와 cache

```text
Browser
  GET /api/agent/public-status
    -> BFF
      -> GET Agent /health/ready
```

- Browser는 Agent port를 직접 호출하지 않는다.
- 응답은 성공·실패 모두 `Cache-Control: no-store`를 사용한다.
- page 진입 시 한 번 조회하고 자동 polling은 초기 범위에서 제외한다.
- 사용자가 선택하는 `상태 새로 확인` action은 GET만 다시 호출한다.
- readiness 조회 실패를 마지막 성공 상태로 대체하지 않는다.

## 3. Public DTO

초기 schema는 다음 의미를 갖는다.

```text
PublicServiceStatus
├─ schema_version: 1
├─ status: operational | degraded | unavailable | unknown
├─ checked_at: datetime | null
├─ message: localized safe message
├─ services[]
│  ├─ id: agent | mobility_information
│  ├─ label
│  └─ status: available | unavailable | unknown
└─ features[]
   ├─ id: trip_accessibility | station_facility
   ├─ label
   └─ status: available | not_supported | temporarily_unavailable | unknown
```

`checked_at`은 MCP dependency snapshot의 실제 확인 시각이다. 확인 시각이 없으면 현재 시각을
대신 넣지 않고 `null`로 유지한다. BFF 응답 생성 시각이 필요하면 별도 `generated_at`을
추가하되 dependency 확인 시각과 같은 의미로 사용하지 않는다.

## 4. Mapping

### 전체 상태

| Agent readiness | MCP readiness | trip capability | public status |
|---|---|---|---|
| ready | ready | 있음 | `operational` |
| ready | ready | 없음 | `degraded` |
| not ready | 임의 | 임의 | `unavailable` |
| 조회 실패·계약 불일치 | 알 수 없음 | 알 수 없음 | `unknown` |

facility capability 부재만으로 전체 서비스를 `degraded`로 바꾸지 않는다. 시설 질문은 독립
feature status `not_supported`로 표시한다. MCP가 일시적으로 준비되지 않았지만 capability
snapshot이 존재해도 현재 새 시설 질문은 `temporarily_unavailable`이다.

### 기능 상태

- trip은 readiness 전체, MCP dependency와 trip capability를 모두 만족할 때 `available`이다.
- facility는 위 조건과 `facility_question` capability를 모두 만족할 때 `available`이다.
- dependency가 ready이고 capability가 없으면 `not_supported`다.
- dependency가 not ready이면 이전 capability와 관계없이 `temporarily_unavailable`이다.
- status 요청 실패나 응답 계약 오류이면 `unknown`이다.

## 5. 공개하지 않는 값

- 내부 URL, host와 port
- 환경 이름과 container 이름
- application version과 배포 revision
- schema fingerprint와 Tool count·이름
- persistence backend와 database 상태 상세
- 내부 error code, exception과 stack trace
- credential, request header와 trace detail
- 사용자 run, 질문, 역과 이동 조건

상세 값은 인증된 `/ops`와 server-side log에서만 사용한다.

## 6. 문구와 접근성

- 상태는 색상, icon과 완전한 text를 함께 사용한다.
- `operational`은 `이동 경로 조회 가능`, `unknown`은 `현재 상태를 확인하지 못했습니다`처럼
  사용자가 취할 행동을 설명한다.
- `not_supported`와 `temporarily_unavailable`을 같은 문구로 합치지 않는다.
- 확인 시각은 `<time datetime>`과 현지화된 text로 표시한다.
- 새로 확인한 결과는 작은 `aria-live="polite"` 영역에 요약만 알린다.
- 자동 새로고침, countdown과 움직이는 상태 표시를 초기 범위에서 사용하지 않는다.

## 7. 오류 정책

- Agent readiness의 503 response body도 예상된 readiness schema로 검증한 뒤 mapping한다.
- timeout, invalid JSON과 schema drift는 모두 public `unknown`으로 축약하고 내부 request ID만
  server log에 남긴다.
- 공개 response에 upstream error message를 포함하지 않는다.
- `/status` 오류가 `/assistant`의 저장된 transcript, 결과와 capability snapshot을 지우지 않는다.
- GET 실패를 POST 재시도로 복구하지 않으며 사용자 질문을 자동 제출하지 않는다.

## 8. 테스트

- ready trip-only, ready trip+facility, not-ready, upstream 503, timeout과 schema drift mapping
- raw readiness의 fingerprint, Tool count, backend와 error code 비노출
- `no-store`, same-origin과 Agent 직접 호출 금지
- 확인 시각 null·유효 datetime 표현
- keyboard, screen reader announcement, 320px reflow와 forced-colors
- `/ops` 권한이나 internal DTO import가 public handler에 필요하지 않음

## 9. 소유권과 후속 확장

- 초기 `PublicServiceStatus`는 Web BFF 전용 schema가 소유한다. Agent의 raw readiness public
  schema를 확대하거나 의미를 바꾸지 않는다.
- incident history와 예정 maintenance는 실제 incident source가 생길 때 별도 계약으로 추가한다.
- freshness threshold는 Agent contract refresh 주기와 함께 측정한 뒤 설정한다. 그 전에는
  임의의 `stale` 상태를 만들지 않고 실제 `checked_at`을 표시한다.
