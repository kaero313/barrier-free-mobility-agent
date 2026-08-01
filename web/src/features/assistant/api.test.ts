import { afterEach, describe, expect, it, vi } from "vitest";

import { PUBLIC_STATUS_UNKNOWN, SUCCESS_RUN } from "@/test/agent-fixtures";

import {
  AgentClientError,
  createIdempotencyKey,
  createThread,
  getPublicStatus,
  resumeRun,
  startRun,
} from "./api";

const IDEMPOTENCY_KEY = "mutation-key-0000000000000001";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("browser Agent BFF client", () => {
  it("uses only same-origin BFF paths and does not retry POST requests", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("network down"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      startRun("1".repeat(32), { question: "시청역에서 강남역" }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ kind: "transport" } satisfies Partial<AgentClientError>);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/agent/threads/${"1".repeat(32)}/runs`);
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "POST", cache: "no-store" });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("idempotency-key"))
      .toBe(IDEMPOTENCY_KEY);
  });

  it("strictly validates a successful schema v4 response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(SUCCESS_RUN), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      startRun("1".repeat(32), { question: "경로 확인" }, IDEMPOTENCY_KEY),
    ).resolves.toEqual(SUCCESS_RUN);
  });

  it("reads only the same-origin public status projection with no-store semantics", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(PUBLIC_STATUS_UNKNOWN), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getPublicStatus()).resolves.toEqual(PUBLIC_STATUS_UNKNOWN);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/agent/public-status");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      cache: "no-store",
      credentials: "same-origin",
    });
  });

  it("rejects a raw readiness document at the public status browser boundary", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "ready",
            service: "internal-service",
            dependencies: {},
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await expect(getPublicStatus()).rejects.toMatchObject({
      kind: "contract",
    } satisfies Partial<AgentClientError>);
  });

  it("rejects an invalid upstream success contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ schema_version: "1", thread_id: "invalid" }), {
          status: 201,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    await expect(createThread()).rejects.toMatchObject({
      kind: "contract",
    } satisfies Partial<AgentClientError>);
  });

  it("projects a validated v2 Agent error without carrying detail into the UI error", async () => {
    const detailOnlyValue = "validated-detail-is-not-ui-copy";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: "2",
            request_id: "request-error",
            error: {
              code: "REQUEST_VALIDATION_ERROR",
              message: "요청 형식이 올바르지 않습니다.",
              retryable: false,
              detail: {
                issues: [
                  {
                    type: "string_too_long",
                    location: ["body", "question"],
                    message: detailOnlyValue,
                  },
                ],
              },
            },
          }),
          { status: 422, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    let captured: unknown;
    try {
      await createThread();
    } catch (error) {
      captured = error;
    }

    expect(captured).toMatchObject({
      kind: "agent",
      status: 422,
      requestId: "request-error",
      code: "REQUEST_VALIDATION_ERROR",
      retryable: false,
    } satisfies Partial<AgentClientError>);
    expect(captured).not.toHaveProperty("detail");
    expect(String(captured)).not.toContain(detailOnlyValue);
  });

  it("treats arbitrary error detail as a contract failure", async () => {
    const secret = "raw-provider-secret";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: "2",
            request_id: "request-error",
            error: {
              code: "MCP_TOOL_EXECUTION_ERROR",
              message: "이동 정보를 조회하지 못했습니다.",
              retryable: false,
              detail: { provider_payload: { secret } },
            },
          }),
          { status: 502, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    let captured: unknown;
    try {
      await createThread();
    } catch (error) {
      captured = error;
    }

    expect(captured).toMatchObject({ kind: "contract" } satisfies Partial<AgentClientError>);
    expect(String(captured)).not.toContain(secret);
  });

  it("preserves bounded request validation issues for structured clarification fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: "2",
            request_id: "request-validation",
            error: {
              code: "REQUEST_VALIDATION_ERROR",
              message: "입력 내용을 확인해 주세요.",
              retryable: false,
              detail: {
                issues: [
                  {
                    type: "clarification_answer_missing",
                    location: ["body", "answer", "values", "origin-input"],
                    message: "출발역을 입력해 주세요.",
                  },
                ],
              },
            },
          }),
          { status: 422, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    let captured: unknown;
    try {
      await resumeRun("1".repeat(32), {
        schema_version: "2",
        clarification_id: "required-fields",
        answer: {
          mode: "fields",
          values: [{ input_id: "origin-input", value: { kind: "text", value: "시청역" } }],
        },
      }, IDEMPOTENCY_KEY);
    } catch (error) {
      captured = error;
    }

    expect(captured).toMatchObject({
      code: "REQUEST_VALIDATION_ERROR",
      validationIssues: [
        expect.objectContaining({
          location: ["body", "answer", "values", "origin-input"],
        }),
      ],
    });
  });

  it("creates a fresh opaque idempotency key without reading server state", () => {
    const first = createIdempotencyKey();
    const second = createIdempotencyKey();

    expect(first).toMatch(/^[0-9a-f-]{36}$/);
    expect(second).toMatch(/^[0-9a-f-]{36}$/);
    expect(first).not.toBe(second);
  });
});
