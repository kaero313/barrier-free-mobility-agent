import { afterEach, describe, expect, it, vi } from "vitest";

import { PUBLIC_STATUS_TRIP_ONLY } from "@/test/agent-fixtures";

import { getPublicStatus, PublicStatusClientError } from "./api";

describe("public status browser client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the same-origin no-store GET contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json(PUBLIC_STATUS_TRIP_ONLY, {
        headers: { "cache-control": "no-store" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getPublicStatus()).resolves.toEqual(PUBLIC_STATUS_TRIP_ONLY);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/agent/public-status",
      expect.objectContaining({
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
  });

  it("rejects data outside the public allowlist contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...PUBLIC_STATUS_TRIP_ONLY,
          schema_fingerprint: "must-not-reach-browser",
        }),
      ),
    );

    await expect(getPublicStatus()).rejects.toBeInstanceOf(PublicStatusClientError);
  });

  it("does not expose an upstream response body when JSON cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("not-json")));

    await expect(getPublicStatus()).rejects.toThrow(
      "서비스 상태 응답을 안전하게 확인하지 못했습니다.",
    );
  });
});
