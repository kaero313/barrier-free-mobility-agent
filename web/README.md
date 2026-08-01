# Barrier-Free Mobility Agent Web

Next.js App Router 기반 사용자·운영 화면 workspace다. 사용자 화면은 Agent의 공개 BFF 계약만 사용하며, Python 프로젝트 파일을 Web build에서 직접 import하지 않는다.

## Runtime

- Node.js 22 이상
- npm 10 이상
- Next.js 16.2.10
- React / React DOM 19.2.7

## Commands

```powershell
npm ci
npm run contracts:check
npm run lint
npm run typecheck
npm test
npm run test:coverage
npm run test:e2e
npm run build
```

FastAPI OpenAPI snapshot을 의도적으로 갱신한 경우에만 `npm run contracts:generate`로 생성
타입을 다시 만들고 변경 내용을 함께 검토한다.

## UI boundary

- `src/theme/design-tokens.json`은 `../docs/design-tokens.json`의 build-local mirror다.
- 공개 CSS 변수는 `src/theme/tokens.css`에 정의한다.
- App Router root layout만 전역 CSS를 불러온다.
- feature는 `AppShell`이 제공하는 `main#main-content`를 중복 생성하지 않는다.
- `openapi/agent.openapi.json`에서 생성한 `agent-openapi.generated.ts`와 strict Zod runtime
  validator를 함께 사용하고 CI에서 두 artifact의 drift를 차단한다.
- landmark나 form 의미가 필요한 UI는 native semantic primitive를 기본으로 사용한다.
- Astryx는 `components/primitives/astryx-adapter.tsx` 밖에서 직접 import하지 않는다.
- Astryx `Card`는 `div`만 렌더링하므로 adapter는 비의미적 grouping에만 사용한다. 의미 있는 section에는 `Surface`를 사용한다.

## Anonymous session and BFF identity

- `AGENT_ANONYMOUS_SESSION_SECRET`은 32 UTF-8 bytes 이상의 cookie 서명·owner 파생 전용 secret이다.
- `AGENT_BFF_SHARED_SECRET`은 32 bytes 이상의 visible ASCII 값이며 Agent의 동일 설정과 일치해야 한다.
- 두 값은 반드시 서로 달라야 하고 `NEXT_PUBLIC_` 변수로 만들면 안 된다.
- 브라우저에는 random nonce와 HMAC-SHA256 signature로 구성된 HttpOnly cookie만 전달한다. Agent owner ID는 별도 HMAC domain에서 파생해 `X-Agent-Owner-ID`로 upstream에만 전송한다.
- BFF는 `X-Agent-Internal-Token`에 shared secret을 주입하며 브라우저가 보낸 owner·internal-token header를 전달하지 않는다.
- `WEB_APP_ENV`는 `local`, `staging`, `production`만 허용한다. production Node runtime에서는 생략할 수 없다. `WEB_PUBLIC_ORIGIN`은 브라우저가 쓰는 정확한 canonical origin으로 설정하며 local에서는 HTTP loopback을, staging/production에서는 HTTPS만 허용한다.
- cookie는 `SameSite=Lax; Path=/`인 browser-session cookie다. `WEB_APP_ENV=staging|production`에서는 `Secure`와 `__Host-` prefix를 함께 사용한다. 최적화 build 여부를 나타내는 `NODE_ENV`로 cookie transport 보안을 결정하지 않는다.
- POST/DELETE BFF 요청은 local에서도 설정된 `WEB_PUBLIC_ORIGIN`과 다른 `Origin`/cross-site `Sec-Fetch-Site`를 차단하고, staging/production에서는 Origin이 없거나 canonical origin과 다르면 session 생성과 Agent 호출 전에 403으로 차단한다. `localhost`와 `127.0.0.1`은 서로 다른 origin이므로 실제 browser URL과 설정을 일치시킨다.
- staging/production의 `AGENT_INTERNAL_URL`은 HTTPS가 기본이다. service mesh/private network에서 평문 hop을 의도적으로 사용할 때만 `AGENT_ALLOW_PRIVATE_HTTP=true`를 설정하고 network policy를 별도로 강제한다.

개발 secret 예시 생성:

```powershell
openssl rand -base64 48
openssl rand -base64 48
```

## Accessibility baseline

- document language: Korean
- body: 16px / 1.6
- control and pointer target: minimum 44px
- visible keyboard focus and skip link
- reduced-motion and forced-colors support
- status meaning is always conveyed with visible text, not color alone

## Dependency security note

Next.js 16.2.10이 고정한 오래된 PostCSS 8.x의 moderate advisory를 피하기 위해
`package.json`에서 호환 가능한 `postcss@8.5.16`을 override한다. 이 override는 Web lint,
unit test, production build와 E2E gate를 모두 통과해야 하며, Next가 수정 버전을 직접 제공하면
제거한다. `npm audit fix --force`로 framework major version을 변경하지 않는다.
