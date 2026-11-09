// @vitest-environment jsdom

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AppShell, type AppShellProps } from "./app-shell";

// 서버 렌더링 결과를 그대로 검사한다. 첫 화면의 landmark 구조는 hydration 전에 갖춰져야 한다.
function renderShell(props: Omit<AppShellProps, "children">, heading: string): Document {
  const markup = renderToStaticMarkup(
    <AppShell {...props}>
      <h1>{heading}</h1>
    </AppShell>,
  );
  return new DOMParser().parseFromString(markup, "text/html");
}

describe("AppShell", () => {
  it("exposes one skip target and semantic page landmarks", () => {
    const page = renderShell(
      { currentPath: "/assistant", headerActions: <button type="button">설정</button> },
      "이동 경로 접근성 확인",
    );

    const focusable = page.querySelector("a[href], button");
    expect(focusable?.getAttribute("href")).toBe("#main-content");
    expect(focusable?.textContent).toBe("본문으로 건너뛰기");

    expect(page.querySelectorAll("#main-content")).toHaveLength(1);
    expect(page.querySelectorAll("main")).toHaveLength(1);
    expect(page.querySelector("main")?.id).toBe("main-content");
    // banner·contentinfo 가 되려면 header·footer 가 main 밖에 있어야 한다.
    expect(page.querySelector("main header, main footer")).toBeNull();
    expect(page.querySelector("header")).not.toBeNull();
    expect(page.querySelector("footer")).not.toBeNull();

    const navigation = page.querySelector('nav[aria-label="주요 메뉴"]');
    expect(navigation).not.toBeNull();
    const current = navigation?.querySelectorAll('[aria-current="page"]');
    expect(current).toHaveLength(1);
    expect(current?.[0]?.getAttribute("href")).toBe("/assistant");

    expect(page.querySelector('a[href="/accessibility"]')).not.toBeNull();
    expect(page.querySelector('a[href^="/ops"]')).toBeNull();
    expect(page.body.textContent).toContain("설정");
  });

  it("always links the brand to the product entry route", () => {
    const page = renderShell({ currentPath: "/" }, "제품 진입");

    const navigation = page.querySelector('nav[aria-label="주요 메뉴"]');
    const brand = navigation?.querySelector('a[href="/"]');
    expect(brand?.textContent).toContain("Barrier-Free Mobility");
    expect(brand?.getAttribute("aria-current")).toBe("page");
  });
});
