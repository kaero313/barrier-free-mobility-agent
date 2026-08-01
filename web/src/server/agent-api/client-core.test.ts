import { describe, expect, it, vi } from "vitest";

import {
  AgentErrorResponseSchema,
  ConversationRunResponseSchema,
  ReadinessResponseSchema,
  ResumeRunRequestSchema,
  StartRunRequestSchema,
} from "../../contracts/agent";
import {
  loadAgentProxyRuntime,
  proxyAgentRequest,
  proxyAgentRequestFromEnvironment,
  type AgentProxyRuntime,
} from "./client-core";

const THREAD_ID = "a".repeat(32);
const RUN_ID = "b".repeat(32);
const REQUEST_ID = "request-123";
const OWNER_ID = "c".repeat(64);
const BFF_SHARED_SECRET = "bff-shared-secret-".padEnd(48, "x");
const SESSION_SECRET = "anonymous-session-secret-".padEnd(48, "y");
const IDEMPOTENCY_KEY = "browser-mutation-key-0000000001";

const terminalRun = {
  schema_version: "4",
  request_id: REQUEST_ID,
  thread_id: THREAD_ID,
  run_id: RUN_ID,
  run_status: "completed",
  source: "agent",
  status: "FAILED",
  intent: "unknown",
  user_message: "현재 요청을 처리하지 못했습니다.",
  clarification_needed: false,
  clarification: null,
  available_partial_info: [],
  evidence: {
    risk_level: null,
    confidence_level: null,
    failed_sources: [],
    limitations: [],
    unverified_parts: [],
    last_checked_at: null,
  },
  presentation: null,
  context: {
    schema_version: "2",
    intent: "unknown",
    origin: null,
    destination: null,
    mobility_conditions: [],
    facility: null,
    active_clarification: null,
  },
} as const;

const readiness = {
  status: "not_ready",
  service: "Barrier-Free Mobility Agent",
  version: "0.1.0",
  dependencies: {
    mobility_mcp: {
      status: "unavailable",
      checked_at: null,
      tool_count: 0,
      capabilities: [],
      schema_fingerprint: null,
      error_code: "MCP_UNAVAILABLE",
    },
    persistence: {
      status: "ready",
      backend: "memory",
    },
  },
} as const;

describe("agent BFF proxy", () => {
  it("derives a stable anonymous owner, injects server headers, and never mirrors them", async () => {
    const raw = JSON.stringify(terminalRun);
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () => {
      const upstreamResponse = jsonResponse(raw, 200, REQUEST_ID);
      upstreamResponse.headers.set("x-agent-owner-id", "must-not-reach-browser");
      upstreamResponse.headers.set("x-agent-internal-token", "must-not-reach-browser");
      return upstreamResponse;
    });
    const environment = {
      AGENT_INTERNAL_URL: "http://agent:8080",
      AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
    };
    const options = (request: Request) => ({
      request,
      method: "GET" as const,
      upstreamPath: `/v1/runs/${RUN_ID}`,
      successStatuses: [200] as const,
      successSchema: ConversationRunResponseSchema,
    });

    const firstResponse = await proxyAgentRequestFromEnvironment(
      options(new Request("http://web.test/api/agent/runs", {
        headers: {
          "x-agent-owner-id": "browser-controlled-owner",
          "x-agent-internal-token": "browser-controlled-token",
        },
      })),
      environment,
      fetchImpl,
    );

    const setCookie = firstResponse.headers.get("set-cookie");
    expect(setCookie).toContain("bfm_anon_session=");
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=Lax");
    expect(setCookie).toContain("Path=/");
    expect(setCookie).not.toContain("Secure");
    const firstUpstreamHeaders = new Headers(fetchImpl.mock.calls[0]?.[1]?.headers);
    const firstOwnerId = firstUpstreamHeaders.get("x-agent-owner-id");
    expect(firstOwnerId).toMatch(/^[a-f0-9]{64}$/);
    expect(firstUpstreamHeaders.get("x-agent-internal-token")).toBe(BFF_SHARED_SECRET);
    expect(firstOwnerId).not.toBe("browser-controlled-owner");
    expect(firstResponse.headers.get("x-agent-owner-id")).toBeNull();
    expect(firstResponse.headers.get("x-agent-internal-token")).toBeNull();
    expect(setCookie).not.toContain(firstOwnerId ?? "unreachable");
    expect(setCookie).not.toContain(BFF_SHARED_SECRET);

    const cookiePair = setCookie?.split(";", 1)[0];
    expect(cookiePair).toBeDefined();
    const secondResponse = await proxyAgentRequestFromEnvironment(
      options(new Request("http://web.test/api/agent/runs", {
        headers: { cookie: cookiePair ?? "" },
      })),
      environment,
      fetchImpl,
    );
    const secondUpstreamHeaders = new Headers(fetchImpl.mock.calls[1]?.[1]?.headers);
    expect(secondUpstreamHeaders.get("x-agent-owner-id")).toBe(firstOwnerId);
    expect(secondResponse.headers.get("set-cookie")).toBeNull();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("fails closed before the upstream call when server secrets are missing or reused", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const request = new Request("http://web.test/api/agent/readiness");
    const options = {
      request,
      method: "GET" as const,
      upstreamPath: "/health/ready",
      successStatuses: [200, 503] as const,
      successSchema: ReadinessResponseSchema,
    };

    const missing = await proxyAgentRequestFromEnvironment(
      options,
      { AGENT_INTERNAL_URL: "http://agent:8080" },
      fetchImpl,
    );
    const reused = await proxyAgentRequestFromEnvironment(
      options,
      {
        AGENT_INTERNAL_URL: "http://agent:8080",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
        AGENT_ANONYMOUS_SESSION_SECRET: BFF_SHARED_SECRET,
        WEB_APP_ENV: "local",
      },
      fetchImpl,
    );

    expect(missing.status).toBe(500);
    expect(reused.status).toBe(500);
    expect(fetchImpl).not.toHaveBeenCalled();
    await expect(missing.json()).resolves.toMatchObject({
      error: { code: "BFF_CONFIGURATION_ERROR", retryable: false },
    });
  });

  it.each([
    { origin: "https://attacker.test", fetchSite: "cross-site" },
    { origin: undefined, fetchSite: "cross-site" },
    { origin: "not-an-origin", fetchSite: undefined },
  ])("rejects cross-site mutations before creating a session", async ({ origin, fetchSite }) => {
    const fetchImpl = vi.fn<typeof fetch>();
    const headers = new Headers({ "content-type": "application/json" });
    if (origin !== undefined) headers.set("origin", origin);
    if (fetchSite !== undefined) headers.set("sec-fetch-site", fetchSite);
    const request = new Request("http://web.test/api/agent/threads", {
      method: "POST",
      headers,
      body: JSON.stringify({ question: "경로 확인" }),
    });

    const response = await proxyAgentRequestFromEnvironment(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
      },
      {
        AGENT_INTERNAL_URL: "http://agent:8080",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
        AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
        WEB_APP_ENV: "local",
      },
      fetchImpl,
    );

    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
    await expect(response.json()).resolves.toMatchObject({
      error: { code: "BFF_CSRF_REJECTED", retryable: false },
    });
  });

  it.each([
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:4317",
  ])("accepts a local mutation from the configured canonical Origin %s", async (publicOrigin) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(JSON.stringify(terminalRun), 200, REQUEST_ID),
    );
    const response = await proxyAgentRequestFromEnvironment(
      {
        request: new Request(`${publicOrigin}/api/agent/threads`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            origin: publicOrigin,
            "sec-fetch-site": "same-origin",
          },
          body: JSON.stringify({ question: "경로 확인" }),
        }),
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
      },
      {
        AGENT_INTERNAL_URL: "http://agent:8080",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
        AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
        WEB_APP_ENV: "local",
        WEB_PUBLIC_ORIGIN: publicOrigin,
      },
      fetchImpl,
    );

    expect(response.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("uses the configured local Origin instead of the internal request URL", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const response = await proxyAgentRequestFromEnvironment(
      {
        request: new Request("http://127.0.0.1:3000/api/agent/threads", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            origin: "http://127.0.0.1:3000",
            "sec-fetch-site": "same-origin",
          },
          body: JSON.stringify({ question: "경로 확인" }),
        }),
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
      },
      {
        AGENT_INTERNAL_URL: "http://agent:8080",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
        AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
        WEB_APP_ENV: "local",
        WEB_PUBLIC_ORIGIN: "http://localhost:3000",
      },
      fetchImpl,
    );

    expect(response.status).toBe(403);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("requires the canonical public Origin for hosted mutations", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const response = await proxyAgentRequestFromEnvironment(
      {
        request: new Request("https://web.test/api/agent/threads", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ question: "경로 확인" }),
        }),
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
      },
      {
        AGENT_INTERNAL_URL: "https://agent.internal",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
        AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
        WEB_APP_ENV: "production",
        WEB_PUBLIC_ORIGIN: "https://web.test",
      },
      fetchImpl,
    );

    expect(response.status).toBe(403);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("validates and mirrors status, bytes, request id, and no-store semantics", async () => {
    const raw = JSON.stringify(terminalRun);
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(raw, 200, REQUEST_ID),
    );
    const runtime = testRuntime(fetchImpl);
    const request = new Request("http://web.test/api/agent/runs", {
      headers: { "x-request-id": REQUEST_ID },
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: `/v1/runs/${RUN_ID}`,
        successStatuses: [200],
        successSchema: ConversationRunResponseSchema,
        responseSchemaVersion: "4",
      },
      runtime,
      OWNER_ID,
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(raw);
    expect(response.headers.get("x-request-id")).toBe(REQUEST_ID);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(String(url)).toBe(`http://agent:8080/v1/runs/${RUN_ID}`);
    expect(init?.cache).toBe("no-store");
    const upstreamHeaders = new Headers(init?.headers);
    expect(upstreamHeaders.get("x-request-id")).toBe(REQUEST_ID);
    expect(upstreamHeaders.get("x-agent-owner-id")).toBe(OWNER_ID);
    expect(upstreamHeaders.get("x-agent-internal-token")).toBe(BFF_SHARED_SECRET);
    expect(upstreamHeaders.get("x-agent-response-schema")).toBe("4");
    expect(response.headers.get("x-agent-response-schema")).toBeNull();
  });

  it("validates a request before forwarding and never retries POST", async () => {
    const upstreamError = {
      schema_version: "2",
      request_id: REQUEST_ID,
      error: {
        code: "THREAD_BUSY",
        message: "처리 중입니다.",
        retryable: true,
        detail: {},
      },
    };
    const rawUpstreamError = JSON.stringify(upstreamError, null, 2);
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(rawUpstreamError, 409, REQUEST_ID),
    );
    const request = new Request("http://web.test/api/agent/runs", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-request-id": REQUEST_ID,
        "idempotency-key": IDEMPOTENCY_KEY,
      },
      body: JSON.stringify({ question: "홍대입구역에서 삼성역까지 갈 수 있나요?" }),
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
        errorSchema: AgentErrorResponseSchema,
        idempotentMutation: true,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(409);
    expect(await response.text()).toBe(JSON.stringify(upstreamError));
    expect(rawUpstreamError).not.toBe(JSON.stringify(upstreamError));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const upstreamHeaders = new Headers(fetchImpl.mock.calls[0]?.[1]?.headers);
    expect(upstreamHeaders.get("idempotency-key")).toBe(IDEMPOTENCY_KEY);
    expect(response.headers.get("idempotency-key")).toBeNull();
  });

  it("rejects an invalid idempotency key without contacting the Agent", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const request = new Request("http://web.test/api/agent/runs", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": "contains spaces and must not pass",
      },
      body: JSON.stringify({ question: "경로 확인" }),
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
        idempotentMutation: true,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(422);
    expect(await response.json()).toMatchObject({
      error: { code: "BFF_IDEMPOTENCY_KEY_INVALID", retryable: false },
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("fails closed when idempotency is accidentally configured on a read route", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const request = new Request("http://web.test/api/agent/runs");

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: `/v1/runs/${RUN_ID}`,
        successStatuses: [200],
        successSchema: ConversationRunResponseSchema,
        idempotentMutation: true,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(500);
    expect(await response.json()).toMatchObject({
      error: { code: "BFF_ROUTE_CONFIGURATION_ERROR", retryable: false },
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("generates an opaque mutation key when an older same-origin client omits it", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(JSON.stringify(terminalRun), 200, REQUEST_ID),
    );
    const request = new Request("http://web.test/api/agent/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "경로 확인" }),
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
        idempotentMutation: true,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(200);
    const upstreamHeaders = new Headers(fetchImpl.mock.calls[0]?.[1]?.headers);
    expect(upstreamHeaders.get("idempotency-key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(response.headers.get("idempotency-key")).toBeNull();
  });

  it.each([
    {
      name: "structured v2",
      payload: {
        schema_version: "2",
        clarification_id: "required-trip-fields",
        answer: {
          mode: "fields",
          values: [
            { input_id: "origin-input", value: { kind: "text", value: "시청역" } },
          ],
        },
      },
    },
    {
      name: "legacy single answer",
      payload: { answer: "시청역", clarification_id: "legacy-clarification" },
    },
  ])("accepts the $name resume request during migration", async ({ payload }) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(JSON.stringify(terminalRun), 200, REQUEST_ID),
    );
    const request = new Request(`http://web.test/api/agent/runs/${RUN_ID}/resume`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-request-id": REQUEST_ID },
      body: JSON.stringify(payload),
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/runs/${RUN_ID}/resume`,
        successStatuses: [200],
        requestSchema: ResumeRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
        errorSchema: AgentErrorResponseSchema,
        responseSchemaVersion: "4",
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0]?.[1]?.body).toBe(JSON.stringify(payload));
    expect(
      new Headers(fetchImpl.mock.calls[0]?.[1]?.headers).get("x-agent-response-schema"),
    ).toBe("4");
  });

  it("fails closed without reflecting arbitrary upstream error detail", async () => {
    const secret = "provider-secret-must-not-cross-the-bff";
    const upstreamError = {
      schema_version: "2",
      request_id: REQUEST_ID,
      error: {
        code: "MCP_TOOL_EXECUTION_ERROR",
        message: "이동 정보를 조회하는 중 오류가 발생했습니다.",
        retryable: false,
        detail: { provider_payload: { secret } },
      },
    };
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(JSON.stringify(upstreamError), 502, REQUEST_ID),
    );
    const request = new Request("http://web.test/api/agent/runs", {
      headers: { "x-request-id": REQUEST_ID },
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: `/v1/runs/${RUN_ID}`,
        successStatuses: [200],
        successSchema: ConversationRunResponseSchema,
        errorSchema: AgentErrorResponseSchema,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );
    const body = await response.text();

    expect(response.status).toBe(502);
    expect(JSON.parse(body)).toEqual({
      schema_version: "2",
      request_id: REQUEST_ID,
      error: {
        code: "AGENT_CONTRACT_INVALID",
        message: "이동 정보 서비스의 응답을 안전하게 확인할 수 없습니다.",
        retryable: false,
        detail: {},
      },
    });
    expect(body).not.toContain(secret);
    expect(body).not.toContain("provider_payload");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("rejects invalid input without contacting the Agent", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const request = new Request("http://web.test/api/agent/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "", unexpected: true }),
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: `/v1/threads/${THREAD_ID}/runs`,
        successStatuses: [200],
        requestSchema: StartRunRequestSchema,
        successSchema: ConversationRunResponseSchema,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(422);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(await response.json()).toMatchObject({
      error: { code: "BFF_REQUEST_VALIDATION_ERROR", retryable: false },
    });
  });

  it("fails closed when the upstream success payload violates schema v4", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(JSON.stringify({ ...terminalRun, schema_version: "2", secret: "hidden" }), 200),
    );
    const request = new Request("http://web.test/api/agent/runs");

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: `/v1/runs/${RUN_ID}`,
        successStatuses: [200],
        successSchema: ConversationRunResponseSchema,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    const raw = await response.text();
    expect(response.status).toBe(502);
    expect(raw).toContain("AGENT_CONTRACT_INVALID");
    expect(raw).not.toContain("hidden");
  });

  it("returns a safe transport error without leaking an internal URL or cause", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(
      new Error("connect ECONNREFUSED http://agent.internal:8080 private-token"),
    );
    const request = new Request("http://web.test/api/agent/readiness");

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: "/health/ready",
        successStatuses: [200, 503],
        successSchema: ReadinessResponseSchema,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    const raw = await response.text();
    expect(response.status).toBe(503);
    expect(raw).toContain("AGENT_UPSTREAM_UNAVAILABLE");
    expect(raw).not.toContain("agent.internal");
    expect(raw).not.toContain("private-token");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("marks a timed-out POST outcome unknown and never retries it", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation((_input, init) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
          once: true,
        });
      });
    });
    const request = new Request("http://web.test/api/agent/threads", {
      method: "POST",
      headers: { "idempotency-key": IDEMPOTENCY_KEY },
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "POST",
        upstreamPath: "/v1/threads",
        successStatuses: [201],
        idempotentMutation: true,
      },
      { ...testRuntime(fetchImpl), timeoutMs: 5 },
      OWNER_ID,
    );

    expect(response.status).toBe(504);
    expect(await response.json()).toMatchObject({
      error: { code: "AGENT_MUTATION_OUTCOME_UNKNOWN", retryable: false },
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("treats readiness 503 as a valid readiness document", async () => {
    const raw = JSON.stringify(readiness);
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(raw, 503, REQUEST_ID),
    );
    const request = new Request("http://web.test/api/agent/readiness");

    const response = await proxyAgentRequest(
      {
        request,
        method: "GET",
        upstreamPath: "/health/ready",
        successStatuses: [200, 503],
        successSchema: ReadinessResponseSchema,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(raw);
  });

  it("mirrors an empty 204 delete response", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, { status: 204, headers: { "x-request-id": REQUEST_ID } }),
    );
    const request = new Request("http://web.test/api/agent/thread", {
      method: "DELETE",
    });

    const response = await proxyAgentRequest(
      {
        request,
        method: "DELETE",
        upstreamPath: `/v1/threads/${THREAD_ID}`,
        successStatuses: [204],
        allowEmptySuccess: true,
      },
      testRuntime(fetchImpl),
      OWNER_ID,
    );

    expect(response.status).toBe(204);
    expect(await response.text()).toBe("");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

describe("Agent runtime configuration", () => {
  it("accepts only a server origin and a bounded timeout", () => {
    const runtime = loadAgentProxyRuntime(
      {
        AGENT_INTERNAL_URL: "http://agent:8080",
        AGENT_REQUEST_TIMEOUT_MS: "45000",
        AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
      },
      fetch,
    );
    expect(runtime.baseUrl.href).toBe("http://agent:8080/");
    expect(runtime.timeoutMs).toBe(45_000);
    expect(runtime.internalToken).toBe(BFF_SHARED_SECRET);

    expect(() =>
      loadAgentProxyRuntime({ AGENT_INTERNAL_URL: "http://user:secret@agent:8080" }, fetch),
    ).toThrow();
    expect(() =>
      loadAgentProxyRuntime({ AGENT_INTERNAL_URL: "http://agent:8080/v1" }, fetch),
    ).toThrow();
    expect(() =>
      loadAgentProxyRuntime(
        {
          AGENT_INTERNAL_URL: "http://agent:8080",
          AGENT_BFF_SHARED_SECRET: "too-short",
        },
        fetch,
      ),
    ).toThrow();
    expect(() =>
      loadAgentProxyRuntime(
        {
          AGENT_INTERNAL_URL: "http://agent:8080",
          AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
          WEB_APP_ENV: "production",
        },
        fetch,
      ),
    ).toThrow();
    expect(
      loadAgentProxyRuntime(
        {
          AGENT_INTERNAL_URL: "http://agent:8080",
          AGENT_BFF_SHARED_SECRET: BFF_SHARED_SECRET,
          WEB_APP_ENV: "production",
          AGENT_ALLOW_PRIVATE_HTTP: "true",
        },
        fetch,
      ).baseUrl.href,
    ).toBe("http://agent:8080/");
  });
});

function testRuntime(fetchImpl: typeof fetch): AgentProxyRuntime {
  return {
    baseUrl: new URL("http://agent:8080"),
    timeoutMs: 1_000,
    maxResponseBytes: 1024 * 1024,
    fetchImpl,
    internalToken: BFF_SHARED_SECRET,
  };
}

function jsonResponse(body: string, status: number, requestId?: string): Response {
  const headers = new Headers({ "content-type": "application/json" });
  if (requestId !== undefined) {
    headers.set("x-request-id", requestId);
  }
  return new Response(body, { status, headers });
}
