# ADR-0001: Python과 LangGraph 기반 서비스 스택

- 상태: Accepted
- 날짜: 2026-07-10

## Context

서비스는 자연어 모델 호출, MCP Streamable HTTP, 여러 Tool의 제한된 조합, 대화 checkpoint, interrupt/resume, 스트리밍과 운영 관측을 함께 처리해야 한다. Barrier-Free Mobility MCP는 별도 Python/FastMCP 서비스이며 Agent와는 네트워크 계약으로 연결된다.

단일 서비스 안에 Java/Kotlin 백엔드와 Python Agent runtime을 동시에 두면 배포·trace·오류 처리 경계가 늘어나지만 현재 요구에는 별도 JVM 도메인 서비스가 없다. LangGraph Python은 Graph API, persistence, retry, interrupt, streaming과 MCP adapter를 직접 제공한다.

## Decision

- Agent API와 runtime을 Python 3.13 async 서비스로 구현한다.
- dependency와 Python 설치는 uv로 관리하고 uv.lock을 commit한다.
- orchestration은 LangGraph Graph API를 사용한다.
- LangChain은 model/message abstraction과 MCP integration에 한정한다.
- MCP 연결은 langchain-mcp-adapters의 HTTP transport를 사용한다.
- 외부 API는 FastAPI/Pydantic v2, streaming은 SSE를 사용한다.
- checkpoint와 장기 store의 운영 backend는 PostgreSQL로 시작한다.
- LangGraph Agent Server가 아닌 자체 FastAPI/run ledger/worker를 기본 운영 경로로 둔다.
- graph runner는 application port 뒤에 두어 향후 Agent Server나 다른 실행 backend로 교체할 수 있게 한다.
- 호환 minor 상한은 manifest에 두고 실제 버전은 lockfile로 고정한다.
- MCP Python SDK는 v2가 안정화될 때까지 1.x로 제한한다.

초기 dependency 기준:

    python >=3.13,<3.14
    langgraph >=1.2,<1.3
    langchain >=1.3,<1.4
    langchain-mcp-adapters >=0.3,<0.4
    mcp >=1.27,<2
    fastapi >=0.139,<0.140
    langgraph-checkpoint-postgres >=3.1,<4

버전 조합은 첫 scaffold에서 uv lock과 integration test로 최종 확정한다.

## Consequences

### Positive

- graph, API, MCP와 observability가 하나의 async runtime과 type system을 공유한다.
- node/edge와 상태 전이가 코드와 trace에 명시적으로 드러난다.
- 별도 protocol translation 없이 MCP structured artifact를 사용할 수 있다.
- API/worker, checkpoint와 복구 메커니즘을 서비스 요구에 맞게 제어할 수 있다.
- 모델 provider와 MCP server는 adapter 경계 뒤에서 교체 가능하다.

### Negative

- 자체 run queue, lease, SSE event replay와 schema migration을 구현·운영해야 한다.
- Python의 CPU-bound 작업은 process/thread 격리가 필요하다. 현재 workload는 네트워크 I/O 중심이다.
- Agent Server가 제공하는 관리 기능을 자동으로 얻지 못한다.
- 형제 MCP가 선언한 최소 Python은 3.12이므로 두 저장소의 runtime matrix를 CI에서 별도로 검증해야 한다.

## Alternatives

### TypeScript + LangGraph.js

클라이언트와 타입 공유에는 유리하지만 현재 persistence/timeout/오류 처리 기능과 Python MCP 생태계를 우선했다.

### Kotlin/Spring API + Python Agent worker

대규모 조직 경계나 기존 JVM 플랫폼이 있으면 타당하지만 현재는 네트워크 hop과 운영 복잡도만 늘어난다.

### LangSmith Agent Server

durable task queue와 API를 빠르게 확보할 수 있다. 그러나 기본 경로에서는 실행 ledger와 복구 정책을 직접 소유하기로 했다. hosted option으로 다시 평가할 수 있다.

### Redis checkpointer

캐시와 pub/sub에는 유용하지만 초기 source of truth를 PostgreSQL 하나로 줄이는 편이 backup, query, run ledger와 일관성 관리에 유리하다.
