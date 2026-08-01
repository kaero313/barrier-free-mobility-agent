import { describe, expect, it } from "vitest";

import { PublicServiceStatusSchema } from "./public-status";

const validStatus = {
  schema_version: "1",
  status: "operational",
  checked_at: "2026-07-14T03:00:00Z",
  message: "이동 경로 조회를 이용할 수 있습니다.",
  services: [
    { id: "agent", label: "이동 조회 서비스", status: "available" },
    {
      id: "mobility_information",
      label: "이동 정보 연결",
      status: "available",
    },
  ],
  features: [
    {
      id: "trip_accessibility",
      label: "이동 경로 접근성",
      status: "available",
    },
    {
      id: "station_facility",
      label: "역 시설 상태",
      status: "not_supported",
    },
  ],
} as const;

describe("PublicServiceStatusSchema", () => {
  it("accepts only the fixed public service and feature projection", () => {
    expect(PublicServiceStatusSchema.parse(validStatus)).toEqual(validStatus);
  });

  it("rejects raw readiness fields at every object boundary", () => {
    expect(
      PublicServiceStatusSchema.safeParse({
        ...validStatus,
        schema_fingerprint: "private-fingerprint",
      }).success,
    ).toBe(false);
    expect(
      PublicServiceStatusSchema.safeParse({
        ...validStatus,
        services: [
          { ...validStatus.services[0], backend: "postgres" },
          validStatus.services[1],
        ],
      }).success,
    ).toBe(false);
  });

  it("requires each allowlisted ID exactly once and in canonical order", () => {
    expect(
      PublicServiceStatusSchema.safeParse({
        ...validStatus,
        services: [...validStatus.services].reverse(),
      }).success,
    ).toBe(false);
    expect(
      PublicServiceStatusSchema.safeParse({
        ...validStatus,
        features: [validStatus.features[0]],
      }).success,
    ).toBe(false);
  });

  it("rejects a dependency timestamp without an explicit timezone", () => {
    expect(
      PublicServiceStatusSchema.safeParse({
        ...validStatus,
        checked_at: "2026-07-14T03:00:00",
      }).success,
    ).toBe(false);
  });
});

