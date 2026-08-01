import { describe, expect, it } from "vitest";

import type { ReadinessResponse } from "@/contracts/agent";

import {
  createUnknownPublicServiceStatus,
  mapReadinessToPublicStatus,
} from "./mapper";

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

describe("public status mapper", () => {
  it("keeps a trip-only product operational while marking facilities unsupported", () => {
    const result = mapReadinessToPublicStatus(READY);

    expect(result).toMatchObject({
      status: "operational",
      checked_at: "2026-07-14T03:00:00Z",
      services: [
        { id: "agent", status: "available" },
        { id: "mobility_information", status: "available" },
      ],
      features: [
        { id: "trip_accessibility", status: "available" },
        { id: "station_facility", status: "not_supported" },
      ],
    });
  });

  it("exposes facilities only when the capability is currently ready", () => {
    const result = mapReadinessToPublicStatus({
      ...READY,
      dependencies: {
        ...READY.dependencies,
        mobility_mcp: {
          ...READY.dependencies.mobility_mcp,
          capabilities: ["trip_accessibility", "facility_question"],
        },
      },
    });

    expect(result.status).toBe("operational");
    expect(result.features[1].status).toBe("available");
  });

  it("degrades a ready dependency when the primary trip capability is absent", () => {
    const result = mapReadinessToPublicStatus({
      ...READY,
      dependencies: {
        ...READY.dependencies,
        mobility_mcp: {
          ...READY.dependencies.mobility_mcp,
          capabilities: [],
        },
      },
    });

    expect(result.status).toBe("degraded");
    expect(result.features.map((feature) => feature.status)).toEqual([
      "not_supported",
      "not_supported",
    ]);
  });

  it("ignores stale capabilities while the mobility dependency is unavailable", () => {
    const result = mapReadinessToPublicStatus({
      ...READY,
      status: "not_ready",
      dependencies: {
        ...READY.dependencies,
        mobility_mcp: {
          ...READY.dependencies.mobility_mcp,
          status: "unavailable",
          capabilities: ["trip_accessibility", "facility_question"],
          error_code: "MCP_UNAVAILABLE",
        },
      },
    });

    expect(result.status).toBe("unavailable");
    expect(result.services.map((service) => service.status)).toEqual([
      "unavailable",
      "unavailable",
    ]);
    expect(result.features.map((feature) => feature.status)).toEqual([
      "temporarily_unavailable",
      "temporarily_unavailable",
    ]);
  });

  it("distinguishes an available mobility dependency from an unavailable product runtime", () => {
    const result = mapReadinessToPublicStatus({
      ...READY,
      status: "not_ready",
      dependencies: {
        ...READY.dependencies,
        persistence: { status: "unavailable", backend: "postgres" },
      },
    });

    expect(result.status).toBe("unavailable");
    expect(result.services.map((service) => service.status)).toEqual([
      "unavailable",
      "available",
    ]);
    expect(result.features[0].status).toBe("temporarily_unavailable");
  });

  it("fails closed when the dependency timestamp is not public-contract safe", () => {
    const result = mapReadinessToPublicStatus({
      ...READY,
      dependencies: {
        ...READY.dependencies,
        mobility_mcp: {
          ...READY.dependencies.mobility_mcp,
          checked_at: "2026-07-14T03:00:00",
        },
      },
    });

    expect(result).toEqual(createUnknownPublicServiceStatus());
  });

  it("never carries raw readiness values into the serialized projection", () => {
    const serialized = JSON.stringify(mapReadinessToPublicStatus(READY));

    for (const forbidden of [
      "private-agent-name",
      "private-version",
      "private-fingerprint",
      "tool_count",
      "schema_fingerprint",
      "backend",
      "dependencies",
    ]) {
      expect(serialized).not.toContain(forbidden);
    }
  });
});

