import "server-only";

import type { ReadinessResponse } from "@/contracts/agent";
import {
  PublicServiceStatusSchema,
  type PublicServiceStatus,
} from "@/contracts/public-status";

const LABELS = {
  agent: "이동 조회 서비스",
  mobilityInformation: "이동 정보 연결",
  tripAccessibility: "이동 경로 접근성",
  stationFacility: "역 시설 상태",
} as const;

const MESSAGES = {
  operational: "이동 경로 조회를 이용할 수 있습니다.",
  degraded: "일부 기능만 이용할 수 있습니다. 기능별 상태를 확인해 주세요.",
  unavailable: "현재 새 이동 조회를 시작할 수 없습니다. 잠시 후 다시 확인해 주세요.",
  unknown: "현재 서비스 상태를 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.",
} as const;

export function createUnknownPublicServiceStatus(): PublicServiceStatus {
  return {
    schema_version: "1",
    status: "unknown",
    checked_at: null,
    message: MESSAGES.unknown,
    services: [
      { id: "agent", label: LABELS.agent, status: "unknown" },
      {
        id: "mobility_information",
        label: LABELS.mobilityInformation,
        status: "unknown",
      },
    ],
    features: [
      {
        id: "trip_accessibility",
        label: LABELS.tripAccessibility,
        status: "unknown",
      },
      {
        id: "station_facility",
        label: LABELS.stationFacility,
        status: "unknown",
      },
    ],
  };
}

export function mapReadinessToPublicStatus(
  readiness: ReadinessResponse,
): PublicServiceStatus {
  const mobility = readiness.dependencies.mobility_mcp;
  const checkedAt = PublicServiceStatusSchema.shape.checked_at.safeParse(
    mobility.checked_at,
  );
  if (!checkedAt.success) {
    return createUnknownPublicServiceStatus();
  }

  const mobilityReady = mobility.status === "ready";
  const persistenceReady = readiness.dependencies.persistence.status === "ready";
  const productReady =
    readiness.status === "ready" && mobilityReady && persistenceReady;
  const capabilities = new Set(mobility.capabilities);
  const hasTrip = capabilities.has("trip_accessibility");
  const hasFacility = capabilities.has("facility_question");

  const overallStatus: PublicServiceStatus["status"] = !productReady
    ? "unavailable"
    : hasTrip
      ? "operational"
      : "degraded";
  const featureStatus = (supported: boolean) => {
    if (!productReady) {
      return "temporarily_unavailable" as const;
    }
    return supported ? ("available" as const) : ("not_supported" as const);
  };

  return {
    schema_version: "1",
    status: overallStatus,
    checked_at: checkedAt.data,
    message: MESSAGES[overallStatus],
    services: [
      {
        id: "agent",
        label: LABELS.agent,
        status: readiness.status === "ready" ? "available" : "unavailable",
      },
      {
        id: "mobility_information",
        label: LABELS.mobilityInformation,
        status: mobilityReady ? "available" : "unavailable",
      },
    ],
    features: [
      {
        id: "trip_accessibility",
        label: LABELS.tripAccessibility,
        status: featureStatus(hasTrip),
      },
      {
        id: "station_facility",
        label: LABELS.stationFacility,
        status: featureStatus(hasFacility),
      },
    ],
  };
}

