import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReadinessResponse } from "@/contracts/agent";

import { createUnknownPublicServiceStatus } from "./mapper";
import {
  PUBLIC_STATUS_MAX_RESPONSE_BYTES,
  PUBLIC_STATUS_TIMEOUT_MS,
  fetchPublicServiceStatus,
  loadPublicServiceStatus,
  type PublicStatusRuntime,
} from "./service";

const INTERNAL_TOKEN = "public-status-test-shared-secret".padEnd(48, "x");
const READY: ReadinessResponse = {
  status: "ready",
  service: "private-agent-name",
  version: "private-version",
  dependencies: {
    mobility_mcp: {
      status: "ready",
      checked_at: "2026-07-14T03:00:00Z",
      tool_count: 8,
      capabilities: ["trip_accessibility"],
      schema_fingerprint: "private-fingerprint",
      error_code: null,
    },
    persistence: { status: "ready", backend: "postgres" },
  },
};

afterEach(() => {
  vi.useRealTimers();
});

describe("public status upstream service", () => {
  it("performs one bounded no-store GET and returns only the public projection", async () => {
    const upstream = jsonResponse(READY, 200, {
      "set-cookie": "private-cookie=secret",
      "x-request-id": "private-request-id",
    });
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(upstream);

    const result = await fetchPublicServiceStatus(testRuntime(fetchImpl));

    expect(result.status).toBe("operational");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(String(url)).toBe("http://agent:8080/health/ready");
    expect(init).toMatchObject({
      method: "GET",
      cache: "no-store",
      redirect: "error",
    });
    const headers = new Headers(init?.headers);
    expect(headers.get("accept")).toBe("application/json");
    expect(headers.get("x-agent-internal-token")).toBe(INTERNAL_TOKEN);
    expect(headers.get("x-agent-owner-id")).toMatch(/^[a-f0-9]{64}$/);
    expect(headers.get("cookie")).toBeNull();

    const serialized = JSON.stringify(result);
    for (const forbidden of [
      "private-cookie",
      "private-request-id",
      "private-agent-name",
      "private-version",
      "private-fingerprint",
      "tool_count",
      "backend",
    ]) {
      expect(serialized).not.toContain(forbidden);
    }
  });

  it("maps a schema-valid upstream 503 to a typed unavailable status", async () => {
    const notReady: ReadinessResponse = {
      ...READY,
      status: "not_ready",
      dependencies: {
        ...READY.dependencies,
        mobility_mcp: {
          ...READY.dependencies.mobility_mcp,
          status: "unavailable",
          error_code: "MCP_UNAVAILABLE",
        },
      },
    };
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(notReady, 503));

    const result = await fetchPublicServiceStatus(testRuntime(fetchImpl));

    expect(result.status).toBe("unavailable");
    expect(result.checked_at).toBe("2026-07-14T03:00:00Z");
  });

  it.each([
    ["unexpected status", jsonResponse(READY, 500)],
    ["invalid content type", new Response(JSON.stringify(READY), { status: 200 })],
    [
      "invalid JSON",
      new Response("{", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ],
    [
      "schema drift",
      jsonResponse({ ...READY, private_field: "must-not-pass" }, 200),
    ],
    ["empty body", new Response(null, { status: 200, headers: { "content-type": "application/json" } })],
  ])("maps %s to unknown", async (_name, response) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response);

    await expect(
      fetchPublicServiceStatus(testRuntime(fetchImpl)),
    ).resolves.toEqual(createUnknownPublicServiceStatus());
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      200,
      {
        ...READY,
        status: "not_ready",
        dependencies: {
          ...READY.dependencies,
          mobility_mcp: {
            ...READY.dependencies.mobility_mcp,
            status: "unavailable",
          },
        },
      },
    ],
    [503, READY],
  ])("rejects HTTP %i when it contradicts the readiness body", async (status, body) => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(body, status));

    await expect(
      fetchPublicServiceStatus(testRuntime(fetchImpl)),
    ).resolves.toEqual(createUnknownPublicServiceStatus());
  });

  it("rejects both declared and streamed bodies over 64 KiB", async () => {
    const declared = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(READY, 200, {
        "content-length": String(PUBLIC_STATUS_MAX_RESPONSE_BYTES + 1),
      }),
    );
    const streamed = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        { ...READY, padding: "x".repeat(PUBLIC_STATUS_MAX_RESPONSE_BYTES) },
        200,
      ),
    );

    await expect(
      fetchPublicServiceStatus(testRuntime(declared)),
    ).resolves.toEqual(createUnknownPublicServiceStatus());
    await expect(
      fetchPublicServiceStatus(testRuntime(streamed)),
    ).resolves.toEqual(createUnknownPublicServiceStatus());
    expect(declared).toHaveBeenCalledTimes(1);
    expect(streamed).toHaveBeenCalledTimes(1);
  });

  it("aborts after five seconds and does not retry the readiness GET", async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation((_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("private timeout detail", "AbortError")),
          { once: true },
        );
      }),
    );

    const pending = fetchPublicServiceStatus(testRuntime(fetchImpl));
    await vi.advanceTimersByTimeAsync(PUBLIC_STATUS_TIMEOUT_MS);

    await expect(pending).resolves.toEqual(createUnknownPublicServiceStatus());
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("fails closed before fetch when the server runtime is not configured", async () => {
    const fetchImpl = vi.fn<typeof fetch>();

    await expect(
      loadPublicServiceStatus({ NODE_ENV: "test" }, fetchImpl),
    ).resolves.toEqual(
      createUnknownPublicServiceStatus(),
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

function testRuntime(fetchImpl: typeof fetch): PublicStatusRuntime {
  return {
    baseUrl: new URL("http://agent:8080"),
    fetchImpl,
    internalToken: INTERNAL_TOKEN,
  };
}

function jsonResponse(
  payload: unknown,
  status: number,
  additionalHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json", ...additionalHeaders },
  });
}
