import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AppShell } from "./app-shell";

describe("AppShell", () => {
  it("exposes one skip target and semantic page landmarks", () => {
    const markup = renderToStaticMarkup(
      <AppShell currentPath="/assistant" headerActions={<button type="button">설정</button>}>
        <h1>이동 경로 접근성 확인</h1>
      </AppShell>,
    );

    expect(markup).toContain('href="#main-content"');
    expect(markup).toContain('<header');
    expect(markup).toContain('<main');
    expect(markup).toContain('id="main-content"');
    expect(markup).toContain('<footer');
    expect(markup.match(/id="main-content"/g)).toHaveLength(1);
    expect(markup).toContain('aria-label="주요 메뉴"');
    expect(markup).toMatch(/aria-current="page"[^>]*href="\/assistant"/);
    expect(markup).toContain('href="/accessibility"');
    expect(markup).not.toContain("/ops");
  });

  it("always links the brand to the product entry route", () => {
    const markup = renderToStaticMarkup(
      <AppShell currentPath="/">
        <h1>제품 진입</h1>
      </AppShell>,
    );

    expect(markup).toContain('aria-current="page" class="');
    expect(markup).toContain('href="/"');
    expect(markup).toContain("Barrier-Free Mobility");
  });
});
