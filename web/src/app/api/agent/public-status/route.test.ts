import { beforeEach, describe, expect, it, vi } from "vitest";

import { createUnknownPublicServiceStatus } from "@/server/public-status/mapper";
import { loadPublicServiceStatus } from "@/server/public-status/service";

import { dynamic, GET, revalidate, runtime } from "./route";

vi.mock("@/server/public-status/service", () => ({
  loadPublicServiceStatus: vi.fn(),
}));

const mockedLoadPublicServiceStatus = vi.mocked(loadPublicServiceStatus);

beforeEach(() => {
  mockedLoadPublicServiceStatus.mockReset();
});

describe("GET /api/agent/public-status", () => {
  it("is always dynamic and returns a no-store public DTO without correlation headers", async () => {
    const publicStatus = createUnknownPublicServiceStatus();
    mockedLoadPublicServiceStatus.mockResolvedValue(publicStatus);

    const response = await GET();

    expect(dynamic).toBe("force-dynamic");
    expect(revalidate).toBe(0);
    expect(runtime).toBe("nodejs");
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("x-request-id")).toBeNull();
    await expect(response.json()).resolves.toEqual(publicStatus);
  });

  it("still returns a safe unknown DTO if the service unexpectedly throws", async () => {
    mockedLoadPublicServiceStatus.mockRejectedValue(
      new Error("private upstream URL and credential"),
    );

    const response = await GET();
    const body = await response.text();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(JSON.parse(body)).toEqual(createUnknownPublicServiceStatus());
    expect(body).not.toContain("private upstream URL");
    expect(body).not.toContain("credential");
  });
});

