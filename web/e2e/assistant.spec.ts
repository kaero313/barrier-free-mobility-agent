import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import type { ConversationRunResponse } from "../src/contracts/agent";
import {
  CLARIFICATION_RUN,
  PUBLIC_STATUS_TRIP_ONLY,
  SUCCESS_RUN,
} from "../src/test/agent-fixtures";

const TRIP_QUESTION = "휠체어로 시청역에서 강남역까지 갈 수 있나요?";
const APP_ORIGIN = process.env.E2E_APP_ORIGIN ?? "";

async function gotoAssistant(page: Page) {
  await page.goto(`${APP_ORIGIN}/assistant`);
}

async function mockPublicStatus(page: Page) {
  await page.route("**/api/agent/public-status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PUBLIC_STATUS_TRIP_ONLY),
    });
  });
}

async function mockConversationRun(page: Page, run: ConversationRunResponse) {
  await page.route("**/api/agent/threads", async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1",
        request_id: "e2e-create",
        thread_id: run.thread_id,
        persistence: "ephemeral",
      }),
    });
  });
  await page.route("**/api/agent/threads/*/runs", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ question: TRIP_QUESTION });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(run),
    });
  });
}

async function submitTripQuestion(page: Page) {
  const submitButton = page.getByRole("button", { name: "조회 시작" });
  await expect(submitButton).toHaveAttribute("aria-disabled", "false");
  await page.getByRole("textbox", { name: "이동 질문" }).fill(TRIP_QUESTION);
  await submitButton.click();
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

test.beforeEach(async ({ page }) => {
  await mockPublicStatus(page);
});

test("keeps the ready empty state focused, bounded, and free of dashboard placeholders", async ({
  page,
}) => {
  const threadRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/agent/threads")) {
      threadRequests.push(request.url());
    }
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await gotoAssistant(page);

  await expect(page.getByRole("heading", { name: "이동 가능한 경로를 확인해 보세요" })).toBeVisible();
  await expect(page.getByRole("button", { name: "조회 시작" })).toHaveAttribute(
    "aria-disabled",
    "false",
  );

  const workspace = page.locator('[data-ui="assistant-workspace"]');
  await expect(workspace).toHaveAttribute("data-state", "empty");
  const workspaceBox = await workspace.boundingBox();
  expect(workspaceBox).not.toBeNull();
  expect(workspaceBox!.width).toBeLessThanOrEqual(880.5);

  await expect(page.getByRole("heading", { name: "이동 경로 조회 가능" })).toHaveCount(0);
  await expect(page.getByText("판단 대기 중")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "역 시설 상태 확인" })).toHaveCount(0);
  await expect(page.getByText(/MCP Tool|LangGraph|Agent/)).toHaveCount(0);
  expect(threadRequests).toEqual([]);

  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
});

test("preserves reflow, reduced motion, and a visible forced-colors focus ring", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "active" });
  await gotoAssistant(page);

  const question = page.getByRole("textbox", { name: "이동 질문" });
  await expect(question).toBeVisible();
  await question.focus();

  const focusAppearance = await question.evaluate((element) => {
    const style = getComputedStyle(element);
    return { outlineStyle: style.outlineStyle, outlineWidth: Number.parseFloat(style.outlineWidth) };
  });
  expect(focusAppearance.outlineStyle).not.toBe("none");
  expect(focusAppearance.outlineWidth).toBeGreaterThanOrEqual(1);

  const excessiveMotion = await page.locator('[data-ui="assistant-workspace"]').evaluate((root) => {
    const elements = [root, ...root.querySelectorAll("*")];
    const durationInMs = (value: string) => {
      const duration = Number.parseFloat(value);
      return value.trim().endsWith("ms") ? duration : duration * 1_000;
    };
    return elements.some((element) => {
      const style = getComputedStyle(element);
      const animationDurations = style.animationDuration.split(",").map(durationInMs);
      const transitionDurations = style.transitionDuration.split(",").map(durationInMs);
      return (
        (style.animationName !== "none" && animationDurations.some((duration) => duration > 0.011)) ||
        transitionDurations.some((duration) => duration > 0.011)
      );
    });
  });
  expect(excessiveMotion).toBe(false);

  await expectNoHorizontalOverflow(page);
});

test("moves into a compact, keyboard-ready clarification state", async ({ page }) => {
  const resumeBodies: unknown[] = [];
  await page.route("**/api/agent/runs/*/resume", async (route) => {
    resumeBodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(SUCCESS_RUN),
    });
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockConversationRun(page, CLARIFICATION_RUN);
  await gotoAssistant(page);
  await submitTripQuestion(page);

  const workspace = page.locator('[data-ui="assistant-workspace"]');
  await expect(workspace).toHaveAttribute("data-state", "clarifying");
  const workspaceBox = await workspace.boundingBox();
  expect(workspaceBox).not.toBeNull();
  expect(workspaceBox!.width).toBeLessThanOrEqual(960.5);

  const clarificationHeading = page.getByRole("heading", { name: "추가 정보가 필요합니다" });
  await expect(clarificationHeading).toBeFocused();
  const choices = page.getByRole("radio");
  const answerRequest = CLARIFICATION_RUN.clarification!.answer_request;
  const expectedChoices =
    answerRequest.mode === "fields" && answerRequest.inputs[0]?.control === "single_choice"
      ? answerRequest.inputs[0].options.length
      : 0;
  await expect(choices).toHaveCount(expectedChoices);
  for (const choice of await choices.all()) {
    await expect(choice).not.toBeChecked();
  }
  await expect(page.getByRole("textbox", { name: "이동 질문" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "후속 질문" })).toHaveCount(0);
  await expect(
    page.getByText("추가 질문을 확인하는 동안 새 질문 입력은 잠겨 있습니다."),
  ).toBeVisible();

  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);

  await choices.first().check();
  await page.getByRole("button", { name: "입력한 정보로 계속" }).click();
  await expect(page.getByRole("heading", { name: "확인이 완료되었습니다" })).toBeVisible();
  expect(resumeBodies).toEqual([
    {
      schema_version: "2",
      clarification_id: "origin-station-choice",
      answer: {
        mode: "fields",
        values: [
          {
            input_id: "origin-station",
            value: { kind: "choice", value: "CITY_HALL_LINE_1" },
          },
        ],
      },
    },
  ]);
});

test("uses the same-origin BFF and presents an 8:4 result that reflows in DOM order", async ({
  page,
}) => {
  const canonicalHeadline = "엘리베이터 동선을 출발 전에 확인해 주세요.";
  const markdownRun: ConversationRunResponse = {
    ...SUCCESS_RUN,
    user_message: [
      `**${canonicalHeadline}**`,
      "",
      "### 역별 접근성 체크",
      "",
      "| 역 | 상태 |",
      "|---|---|",
      "| 홍대입구역 | 운행 중 |",
    ].join("\n"),
  };
  const browserRequests: string[] = [];
  page.on("request", (request) => browserRequests.push(request.url()));

  await page.setViewportSize({ width: 1280, height: 900 });
  await mockConversationRun(page, markdownRun);
  await gotoAssistant(page);
  await submitTripQuestion(page);

  await expect(page.getByRole("heading", { name: "확인이 완료되었습니다" })).toBeVisible();
  await expect(page.getByText(canonicalHeadline)).toBeVisible();
  await expect(page.getByRole("region", { name: "조회 응답 표" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "판단 근거와 정보 한계" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "후속 질문" })).toBeVisible();

  const resultLayout = page.locator('[data-ui="result-summary"]');
  const resultBody = resultLayout.locator(":scope > section");
  const evidence = resultLayout.locator(":scope > aside");
  const [resultBodyBox, evidenceBox] = await Promise.all([
    resultBody.boundingBox(),
    evidence.boundingBox(),
  ]);
  expect(resultBodyBox).not.toBeNull();
  expect(evidenceBox).not.toBeNull();
  expect(Math.abs(resultBodyBox!.y - evidenceBox!.y)).toBeLessThanOrEqual(1);
  expect(resultBodyBox!.width / evidenceBox!.width).toBeGreaterThan(1.9);
  expect(resultBodyBox!.width / evidenceBox!.width).toBeLessThan(2.1);

  const domOrder = await page.evaluate(() => {
    const resultTitle = document.getElementById("result-title");
    const evidenceTitle = document.getElementById("evidence-title");
    const followup = document.getElementById("assistant-question");
    if (!resultTitle || !evidenceTitle || !followup) return null;
    return {
      resultBeforeEvidence: Boolean(
        resultTitle.compareDocumentPosition(evidenceTitle) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
      evidenceBeforeFollowup: Boolean(
        evidenceTitle.compareDocumentPosition(followup) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    };
  });
  expect(domOrder).toEqual({ resultBeforeEvidence: true, evidenceBeforeFollowup: true });

  expect(browserRequests.some((url) => url.includes("127.0.0.1:8080"))).toBe(false);
  expect(browserRequests.some((url) => url.includes("127.0.0.1:18080"))).toBe(false);
  expect(browserRequests.some((url) => url.includes("/api/agent/threads"))).toBe(true);

  await page.setViewportSize({ width: 320, height: 800 });
  const [mobileResultBodyBox, mobileEvidenceBox] = await Promise.all([
    resultBody.boundingBox(),
    evidence.boundingBox(),
  ]);
  expect(mobileResultBodyBox).not.toBeNull();
  expect(mobileEvidenceBox).not.toBeNull();
  expect(mobileEvidenceBox!.y).toBeGreaterThanOrEqual(
    mobileResultBodyBox!.y + mobileResultBodyBox!.height - 1,
  );
  expect(Math.abs(mobileResultBodyBox!.width - mobileEvidenceBox!.width)).toBeLessThanOrEqual(1);

  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
});
