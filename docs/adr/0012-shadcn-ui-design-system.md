# ADR-0012: shadcn/ui 기반 Web 디자인 시스템

- 상태: Accepted
- 결정일: 2026-10-08
- 범위: `web/` 사용자 화면의 component, theme token, app shell
- 대체: ADR-0003의 "`docs/design-tokens.json`을 CSS custom properties로 변환한다" 항목

## 배경

Web은 Astryx `Card` adapter, 자체 primitive(`Button`, `Surface`, `StatusBadge`), CSS Module과
`docs/design-tokens.json`을 원본으로 하는 CSS 변수로 화면을 그렸다. 화면마다 CSS Module이
커지고(Assistant 단일 파일 1,300줄 이상) component 대부분을 직접 유지해야 했다. 같은 조직의
다른 제품(hyper-vision, atlaslink_poc)은 shadcn/ui 기본 component와 기본 테마로 통일되어 있어
이 제품만 별도 시각 언어와 primitive를 유지할 이유가 줄었다.

## 결정

1. shadcn/ui `base-nova` style, `slate` base color, Base UI primitive, lucide icon, Tailwind CSS 4를
   사용한다. 설정은 `web/components.json`, theme token은 `web/src/app/globals.css` 하나에 둔다.
   `docs/design-tokens.json`, `web/src/theme/`과 Astryx·StyleX 의존성은 제거한다.
2. `web/src/components/ui/`의 component는 참고 제품과 같은 shadcn 생성본을 기준으로 하고, 커스텀
   브랜드 색을 두지 않는다. 화면 code는 Card, Badge, Alert, Button, Field, Textarea, Sidebar,
   Breadcrumb, Message·Bubble 같은 기본 component를 조합한다.
3. 본문 폰트는 참고 제품과 같은 Pretendard 가변 폰트를 `next/font/local`로 빌드에 포함한다.
4. app shell은 shadcn sidebar 대시보드 구조(`Sidebar` + `SidebarInset`)를 쓰고, 좁은 화면에서는
   sidebar를 sheet로 연다. 라이트·다크 테마는 기기 설정을 따르며 header에서 전환할 수 있다.
5. 결과 상태를 한눈에 구분하도록 Tailwind 기본 팔레트의 `--success`(green)·`--warning`(amber)·
   `--info`(sky) 토큰을 더한다. 브랜드 색이 아니라 상태의 보조 신호라서 상태 아이콘과 옅은
   배경·테두리에만 쓰고 글자색으로 쓰지 않는다. 상태는 항상 아이콘 모양과 문구가 말한다.
   `theme-contrast.test.ts`가 카드·muted 표면 위 비텍스트 대비 3:1을 확인하고, forced-colors에서는
   `CanvasText`로 대응시킨다.

## 접근성 때문에 기본값에서 벗어나는 항목

교통약자 서비스라는 제품 성격상 WCAG 2.2 AA 기준선을 component 기본값보다 우선한다. 다음
항목만 기본값과 다르게 두고 이유를 code 주석에 남긴다.

- `--muted-foreground`, `--destructive`, light `--ring`: 기본값이 텍스트 4.5:1 또는 포커스
  3:1에 미달해 한 단계 짙게 둔다. `src/app/theme-contrast.test.ts`가 light·dark 토큰 쌍의
  대비를 계산해 회귀를 막는다.
- 키보드 포커스: 반투명 ring에 더해 `--ring` 실선 outline을 레이어 밖 규칙으로 그린다.
  forced-colors에서는 시스템 `Highlight` outline이 유일한 포커스 표시다.
- forced-colors: 테마 토큰을 `Canvas`, `CanvasText`, `ButtonFace`, `Highlight` 같은 시스템
  색으로 대응시킨다. 배경과 글자를 함께 칠하는 배지·버튼이 한쪽만 강제되는 것을 막는다.
- 주요 조작 영역: `Button` default·lg·icon 크기를 44px 이상으로 둔다. sidebar 메뉴 항목도 같다.
- 참고 제품의 `d` 단일 문자 테마 단축키는 두지 않는다(WCAG 2.1.4).
- clarification 선택지는 Base UI RadioGroup 대신 native radio를, 근거·신고 정보 disclosure는
  native `details`를 쓴다. 서버가 준 input을 native control로 그리는 ADR-0009 계약과 JavaScript
  없이 열리는 disclosure를 유지하기 위해서다.
- `SidebarInset`은 `main` 대신 `div`로 렌더링하고 그 안에 `header`·`main#main-content`·`footer`를
  둔다. banner·main·contentinfo landmark와 skip link 계약을 유지하기 위해서다.

## 결과

- 화면 code가 공통 component 조합으로 줄고 조직 내 다른 제품과 시각 언어가 같아진다.
- 시각 기준의 단일 원천이 `docs/design-tokens.json`에서 `web/src/app/globals.css`로 옮겨진다.
  Python test의 토큰 mirror 검사는 제거하고, 대비 검사는 Web unit test가 맡는다.
- Assistant의 레이아웃 계약(질문 전 880px, clarification 960px, 결과 본문:근거 = 8:4)과
  320px reflow, forced-colors, reduced-motion, axe 검사는 기존 Playwright E2E로 계속 검증한다.
- shadcn component를 갱신할 때는 위 접근성 예외가 덮어쓰이지 않았는지 확인해야 한다.
