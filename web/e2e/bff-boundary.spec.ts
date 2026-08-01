import { once } from "node:events";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { expect, test } from "@playwright/test";

import { READY_TRIP_ONLY, SUCCESS_RUN } from "../src/test/agent-fixtures";

const FAKE_AGENT_PORT = 18080;
const INTERNAL_TOKEN = "e2e-bff-shared-secret-not-for-production-0001";
const APP_ORIGIN = process.env.E2E_APP_ORIGIN ?? "";
const OWNER_PATTERN = /^[a-f0-9]{64}$/;
const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/;
const PUBLIC_STATUS_OWNER_ID = "0".repeat(64);
const observedOwners = new Set<string>();
const observedRequests: string[] = [];
const observedMutationKeys = new Set<string>();

let fakeAgent: ReturnType<typeof createServer>;

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  fakeAgent = createServer((request, response) => {
    void handleAgentRequest(request, response).catch(() => {
      if (!response.headersSent) {
        response.statusCode = 500;
      }
      response.end();
    });
  });
  fakeAgent.listen(FAKE_AGENT_PORT, "127.0.0.1");
  await once(fakeAgent, "listening");
});

test.afterAll(async () => {
  const closed = once(fakeAgent, "close");
  fakeAgent.close();
  await closed;
});

test.beforeEach(() => {
  observedOwners.clear();
  observedRequests.length = 0;
  observedMutationKeys.clear();
});

test("projects readiness for public pages without creating conversation state", async ({
  page,
  request,
}) => {
  test.setTimeout(90_000);
  const browserRequests: string[] = [];
  page.on("request", (browserRequest) => browserRequests.push(browserRequest.url()));

  const homeStatusResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/agent/public-status") &&
      response.request().method() === "GET",
  );
  await page.goto(`${APP_ORIGIN}/`);
  const publicResponse = await homeStatusResponse;
  const publicBody = (await publicResponse.json()) as Record<string, unknown>;
  const serialized = JSON.stringify(publicBody);

  expect(publicResponse.status()).toBe(200);
  expect(publicResponse.headers()["cache-control"]).toBe("no-store");
  expect(publicResponse.headers()["set-cookie"]).toBeUndefined();
  expect(publicResponse.headers()["x-request-id"]).toBeUndefined();
  expect(publicBody).toMatchObject({
    schema_version: "1",
    status: "operational",
    features: [
      { id: "trip_accessibility", status: "available" },
      { id: "station_facility", status: "not_supported" },
    ],
  });
  for (const forbidden of [
    "dependencies",
    "tool_count",
    "schema_fingerprint",
    "backend",
    "private-agent-version",
  ]) {
    expect(serialized).not.toContain(forbidden);
  }

  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "교통약자의 이동 가능성",
  );
  await expect(page.getByText("현재 이동 경로 조회를 시작할 수 있습니다.")).toBeVisible();

  const statusResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/agent/public-status") &&
      response.request().method() === "GET",
  );
  await page.goto(`${APP_ORIGIN}/status`);
  await statusResponse;
  await expect(page.getByRole("heading", { level: 1, name: "서비스 상태" })).toBeVisible();
  await expect(page.getByText("현재 지원하지 않음")).toBeVisible();

  expect(observedRequests).toEqual(["GET /health/ready", "GET /health/ready"]);
  expect(observedOwners).toEqual(new Set([PUBLIC_STATUS_OWNER_ID]));
  expect(
    browserRequests.some((url) =>
      /\/api\/agent\/(?:threads|runs)(?:\/|$)/.test(new URL(url).pathname),
    ),
  ).toBe(false);
  expect(browserRequests.some((url) => url.includes(`127.0.0.1:${FAKE_AGENT_PORT}`))).toBe(
    false,
  );
  expect((await page.context().cookies()).some((cookie) => cookie.name === "bfm_anon_session")).toBe(
    false,
  );

  const removedRawRoute = await request.get(`${APP_ORIGIN}/api/agent/readiness`);
  expect(removedRawRoute.status()).toBe(404);
  expect(await removedRawRoute.text()).not.toContain("schema_fingerprint");
});

test("routes a durable trip through the real BFF security boundary", async ({ page }) => {
  test.setTimeout(60_000);
  const browserRequests: string[] = [];
  page.on("request", (request) => browserRequests.push(request.url()));

  await page.goto(`${APP_ORIGIN}/assistant`);
  await expect(page.getByRole("heading", { name: "이동 가능한 경로를 확인해 보세요" })).toBeVisible();
  await page
    .getByRole("textbox", { name: "이동 질문" })
    .fill("시청역에서 강남역까지 휠체어로 갈 수 있나요?");
  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/agent/threads") &&
      response.request().method() === "POST",
  );
  const runResponsePromise = page.waitForResponse(
    (response) =>
      /\/api\/agent\/threads\/[a-f0-9]{32}\/runs$/.test(response.url()) &&
      response.request().method() === "POST",
  );
  const submitButton = page.getByRole("button", { name: "조회 시작" });
  await expect(submitButton).toHaveAttribute("aria-disabled", "false");
  await submitButton.click();

  const createResponse = await createResponsePromise;
  const runResponse = await runResponsePromise;
  expect(await createResponse.json()).toMatchObject({ persistence: "durable" });
  // Next dev may replace an explicit no-store response with its stricter
  // no-cache/must-revalidate development policy. Both prevent reuse; unit and
  // production-container checks assert the BFF's exact no-store header.
  expect(createResponse.headers()["cache-control"]).toMatch(/\bno-(?:store|cache)\b/);
  expect(runResponse.headers()["cache-control"]).toMatch(/\bno-(?:store|cache)\b/);
  expect(runResponse.headers()["idempotency-key"]).toBeUndefined();
  await expect(page.getByText(SUCCESS_RUN.user_message)).toBeVisible();

  expect(observedOwners.has(PUBLIC_STATUS_OWNER_ID)).toBe(true);
  expect([...observedOwners].filter((owner) => owner !== PUBLIC_STATUS_OWNER_ID)).toHaveLength(1);
  expect(observedRequests).toEqual(
    expect.arrayContaining([
      "GET /health/ready",
      "POST /v1/threads",
      `POST /v1/threads/${SUCCESS_RUN.thread_id}/runs`,
    ]),
  );
  expect(observedMutationKeys.size).toBe(1);
  expect([...observedMutationKeys][0]).toMatch(IDEMPOTENCY_KEY_PATTERN);
  expect(browserRequests.some((url) => url.includes(`127.0.0.1:${FAKE_AGENT_PORT}`))).toBe(
    false,
  );
  expect(
    browserRequests
      .filter((url) => url.includes("/api/agent/"))
      .every((url) => new URL(url).origin === new URL(page.url()).origin),
  ).toBe(true);
  const sessionCookie = (await page.context().cookies()).find(
    (cookie) => cookie.name === "bfm_anon_session",
  );
  expect(sessionCookie).toMatchObject({ httpOnly: true, sameSite: "Lax" });
});

async function handleAgentRequest(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const owner = firstHeader(request.headers["x-agent-owner-id"]);
  const token = firstHeader(request.headers["x-agent-internal-token"]);
  if (token !== INTERNAL_TOKEN || owner === null || !OWNER_PATTERN.test(owner)) {
    writeJson(response, 401, {
      schema_version: "2",
      request_id: "fake-agent-authentication",
      error: {
        code: "OWNER_AUTHENTICATION_REQUIRED",
        message: "authentication required",
        retryable: false,
        detail: {},
      },
    });
    return;
  }

  observedOwners.add(owner);
  observedRequests.push(`${request.method ?? "UNKNOWN"} ${request.url ?? ""}`);
  if (request.method === "GET" && request.url === "/health/ready") {
    writeJson(response, 200, {
      ...READY_TRIP_ONLY,
      version: "private-agent-version",
      dependencies: {
        ...READY_TRIP_ONLY.dependencies,
        persistence: { status: "ready", backend: "postgres" },
      },
    });
    return;
  }
  if (request.method === "POST" && request.url === "/v1/threads") {
    writeJson(response, 201, {
      schema_version: "1",
      request_id: "fake-agent-create",
      thread_id: SUCCESS_RUN.thread_id,
      persistence: "durable",
    });
    return;
  }
  if (
    request.method === "POST" &&
    request.url === `/v1/threads/${SUCCESS_RUN.thread_id}/runs`
  ) {
    const idempotencyKey = firstHeader(request.headers["idempotency-key"]);
    if (idempotencyKey === null || !IDEMPOTENCY_KEY_PATTERN.test(idempotencyKey)) {
      writeJson(response, 422, {
        schema_version: "2",
        request_id: "fake-agent-idempotency-key",
        error: {
          code: "IDEMPOTENCY_KEY_REQUIRED",
          message: "idempotency key is required",
          retryable: false,
          detail: {},
        },
      });
      return;
    }
    observedMutationKeys.add(idempotencyKey);
    if (firstHeader(request.headers["x-agent-response-schema"]) !== "4") {
      writeJson(response, 400, {
        schema_version: "2",
        request_id: "fake-agent-response-schema",
        error: {
          code: "RESPONSE_SCHEMA_REQUIRED",
          message: "response schema 4 is required",
          retryable: false,
          detail: {},
        },
      });
      return;
    }
    const body = JSON.parse(await readBody(request)) as { question?: unknown };
    if (typeof body.question !== "string") {
      writeJson(response, 422, {
        schema_version: "2",
        request_id: "fake-agent-invalid-question",
        error: {
          code: "INVALID_QUESTION",
          message: "question is required",
          retryable: false,
          detail: {},
        },
      });
      return;
    }
    writeJson(response, 200, SUCCESS_RUN);
    return;
  }
  writeJson(response, 404, {
    schema_version: "2",
    request_id: "fake-agent-not-found",
    error: {
      code: "NOT_FOUND",
      message: "not found",
      retryable: false,
      detail: {},
    },
  });
}

function firstHeader(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) {
    return value.length === 1 ? (value[0] ?? null) : null;
  }
  return value ?? null;
}

async function readBody(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

function writeJson(response: ServerResponse, status: number, payload: unknown): void {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(payload));
}
