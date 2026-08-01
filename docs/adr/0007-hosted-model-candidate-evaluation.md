# ADR-0007: Hosted model 후보의 활성화 전 평가 경계

- 상태: Accepted
- 날짜: 2026-07-13
- 관련: ADR-0002, ADR-0006, ADR-0009

## 배경

provider-neutral model boundary와 128개 합성 routing corpus만으로는 실제 hosted model의
wire 형식, tokenizer, strict structured output, SDK retry, 지연 시간과 비용을 검증할 수 없다.
반대로 검증되지 않은 adapter를 LangGraph에 바로 연결하면 명확한 deterministic 질문까지 외부로
전송하거나, token reservation과 실제 청구량이 달라질 수 있다.

## 결정

첫 평가 후보로 OpenAI Responses API의 immutable snapshot
`gpt-5.4-nano-2026-03-17`을 구현한다. 이 선택은 운영 기본 provider 선정이 아니라 평가 가능한
후보 하나를 추가하는 결정이다. 이 ADR을 승인한 시점에는 graph flow `mobility-v1`, state schema
3과 모든 playbook의 `max_model_calls=0`을 변경하지 않았다. 이후 ADR-0009의 structured
clarification topology가 production flow를 `mobility-v2`로 올렸지만 state schema 3, model 0회
기본값과 이 hosted candidate 평가 경계는 그대로 유지한다.

### 불변 계약

- `HostedModelContract`가 provider/model alias뿐 아니라 model snapshot, API·wire·schema transform,
  counter, context/output cap, reasoning cap 의미, 가격표와 data-policy version을 묶는다.
- `provider_contract_version`은 invocation, result identity와 model reservation schema v2에 포함한다.
- count와 invoke는 같은 immutable invocation에서 컴파일한 system instruction, message, strict JSON
  Schema, reasoning, verbosity, tool 없음과 truncation 설정을 공유한다.
- count는 Responses input-token endpoint의 값을 `exact`로 취급하되 object와 non-negative integer를
  다시 검증한다. provider-neutral fallback estimator는 hosted admission에 사용할 수 없다.
- invoke는 `max_output_tokens=512`, `store=false`, `background=false`, `stream=false`를 사용한다.
  응답 model snapshot과 반환된 output cap도 요청과 같아야 한다.
- API origin은 `https://api.openai.com/v1`로 고정하고 base URL, header, API key, retry, tool,
  sampling option 주입을 받지 않는다. SDK retry는 0이고 호출 timeout은 유한하다.
- usage는 output validation보다 먼저 정규화한다. refusal, incomplete, malformed/extra/coerced JSON,
  model/cap drift와 provider 오류는 raw body 없는 typed failure로 바꾼다.

### 평가 gate

`evals/hosted/candidates.v1.json`은 후보 계약 hash, corpus hash, 공개 가격표 snapshot과 다음 기준을
버전으로 고정한다.

- 같은 128-case corpus를 3회 반복
- exact routing accuracy 95% 이상, release blocker 실패 0
- strict structured output와 complete usage 100%
- provider count 대비 reported input undercount 0
- output cap mismatch 0
- p95 3,000ms 이하, 평균 보수적 비용 USD 0.001 이하

live 평가는 일반 CI에서 실행하지 않는다. `--confirm-live-api`, provider request 상한, worst-case
spend 상한과 `OPENAI_API_KEY`가 모두 있어야 하며 stdout에는 aggregate만 출력한다. prompt,
response, 질문 fingerprint와 secret은 artifact로 남기지 않는다.

### 원격 count의 제한

Responses input-token count도 사용자 입력을 provider에 전송하는 별도 외부 request다. 현재 구현은
candidate 평가 truth와 wire 검증에만 사용한다. 운영 graph를 활성화하려면 local exact tokenizer나
provider가 보장한 local conservative counter를 우선 선택한다. 원격 preflight가 불가피하다면
inference와 별도로 privacy, request budget, timeout과 crash 정책에 포함해야 한다.

## 활성화 조건

이 ADR 완료만으로 provider를 선택하거나 graph를 활성화하지 않는다. 동일 corpus로 비교 가능한 두
후보 이상의 live 결과, 보호된 환경의 승인 기록, Stage C context reduction과 reservation/model node
통합이 모두 끝난 뒤 별도 ADR에서 기본 후보를 정한다.

## 참고

- [OpenAI GPT-5.4 nano model contract](https://developers.openai.com/api/docs/models/gpt-5.4-nano)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/responses-vs-chat-completions)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
