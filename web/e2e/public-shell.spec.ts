import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { PUBLIC_STATUS_TRIP_ONLY } from "../src/test/agent-fixtures";

const APP_ORIGIN = process.env.E2E_APP_ORIGIN ?? "";

async function mockPublicStatus(
  page: Page,
  onRequest: (method: string) => void = () => undefined,
) {
  await page.route("**/api/agent/public-status", async (route) => {
    onRequest(route.request().method());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify(PUBLIC_STATUS_TRIP_ONLY),
    });
  });
}

async function expectNoAxeViolations(page: Page) {
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(hasHorizontalOverflow).toBe(false);
}

test("connects the public information architecture without conversation or ops requests", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const agentRequests: Array<{ method: string; path: string }> = [];
  const browserPaths: string[] = [];
  const directAgentRequests: string[] = [];
  await mockPublicStatus(page);
  page.on("request", (request) => {
    const url = new URL(request.url());
    browserPaths.push(url.pathname);
    if (url.pathname.startsWith("/api/agent/")) {
      agentRequests.push({ method: request.method(), path: url.pathname });
    }
    if (url.port === "18080") {
      directAgentRequests.push(request.url());
    }
  });

  const routes = [
    { path: "/", heading: "교통약자의 이동 가능성을 확인된 정보와 한계를 함께 안내합니다." },
    { path: "/how-it-works", heading: "이동 정보를 확인하는 방식" },
    { path: "/status", heading: "서비스 상태" },
    { path: "/accessibility", heading: "화면을 이용하는 방법" },
  ] as const;

  for (const route of routes) {
    await page.goto(`${APP_ORIGIN}${route.path}`);
    await expect(page.getByRole("heading", { level: 1, name: route.heading })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "주요 메뉴" })).toBeVisible();
    await expect(page.locator('a[href="/ops"]')).toHaveCount(0);
    await expectNoAxeViolations(page);
  }

  expect(
    agentRequests.filter(
      ({ path }) => /\/api\/agent\/(?:threads|runs)(?:\/|$)/.test(path),
    ),
  ).toEqual([]);
  expect(browserPaths.some((path) => path.startsWith("/internal/ops/v1"))).toBe(false);
  expect(browserPaths).not.toContain("/api/agent/readiness");
  expect(agentRequests.every(({ method }) => method === "GET")).toBe(true);
  expect(directAgentRequests).toEqual([]);

  const sitemap = await page.request.get(`${APP_ORIGIN}/sitemap.xml`);
  expect(sitemap.status()).toBe(200);
  const sitemapBody = await sitemap.text();
  expect(sitemapBody).toContain("/assistant");
  expect(sitemapBody).toContain("/accessibility");
  expect(sitemapBody).not.toContain("/ops");
});

test("performs one initial status GET and one explicit refresh GET without polling", async ({
  page,
}) => {
  const methods: string[] = [];
  await mockPublicStatus(page, (method) => methods.push(method));

  await page.goto(`${APP_ORIGIN}/status`);
  await expect(
    page.locator('[data-ui="status-badge"]').filter({ hasText: "이동 경로 조회 가능" }),
  ).toBeVisible();
  expect(methods).toEqual(["GET"]);

  await page.getByRole("button", { name: "상태 새로 확인" }).click();
  await expect.poll(() => methods).toEqual(["GET", "GET"]);
  await page.waitForTimeout(300);
  expect(methods).toEqual(["GET", "GET"]);
  await expect(page.locator("time")).toHaveAttribute(
    "datetime",
    "2026-07-11T03:00:00+00:00",
  );
});

test("keeps navigation and status usable at 320px and with forced colors", async ({ page }) => {
  await mockPublicStatus(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  await page.goto(`${APP_ORIGIN}/`);

  await expect(page.getByRole("link", { name: "접근성 안내" }).first()).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "본문으로 건너뛰기" });
  await expect(skipLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await page.getByRole("link", { name: "서비스 상태", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "서비스 상태" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
});
