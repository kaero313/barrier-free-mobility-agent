import type {
  ConversationRunResponse,
  ReadinessResponse,
} from "@/contracts/agent";
import type { PublicServiceStatus } from "@/contracts/public-status";

export const PUBLIC_STATUS_TRIP_ONLY: PublicServiceStatus = {
  schema_version: "1",
  status: "operational",
  checked_at: "2026-07-11T03:00:00+00:00",
  message: "이동 경로 조회를 시작할 수 있습니다.",
  services: [
    { id: "agent", label: "이동 조회 서비스", status: "available" },
    { id: "mobility_information", label: "이동 정보", status: "available" },
  ],
  features: [
    { id: "trip_accessibility", label: "이동 경로 접근성", status: "available" },
    { id: "station_facility", label: "역 시설 정보", status: "not_supported" },
  ],
};

export const PUBLIC_STATUS_WITH_FACILITY: PublicServiceStatus = {
  ...PUBLIC_STATUS_TRIP_ONLY,
  features: [
    PUBLIC_STATUS_TRIP_ONLY.features[0],
    {
      ...PUBLIC_STATUS_TRIP_ONLY.features[1],
      status: "available",
    },
  ],
};

export const PUBLIC_STATUS_UNKNOWN: PublicServiceStatus = {
  schema_version: "1",
  status: "unknown",
  checked_at: null,
  message: "현재 서비스 상태를 확인하지 못했습니다.",
  services: [
    { id: "agent", label: "이동 조회 서비스", status: "unknown" },
    { id: "mobility_information", label: "이동 정보", status: "unknown" },
  ],
  features: [
    { id: "trip_accessibility", label: "이동 경로 접근성", status: "unknown" },
    { id: "station_facility", label: "역 시설 정보", status: "unknown" },
  ],
};

export const READY_TRIP_ONLY: ReadinessResponse = {
  status: "ready",
  service: "barrier-free-mobility-agent",
  version: "0.1.0",
  dependencies: {
    mobility_mcp: {
      status: "ready",
      checked_at: "2026-07-11T03:00:00+00:00",
      tool_count: 4,
      capabilities: ["trip_accessibility"],
      schema_fingerprint: "test-trip-contract",
      error_code: null,
    },
    persistence: {
      status: "ready",
      backend: "memory",
    },
  },
};

export const READY_WITH_FACILITY: ReadinessResponse = {
  ...READY_TRIP_ONLY,
  dependencies: {
    ...READY_TRIP_ONLY.dependencies,
    mobility_mcp: {
      ...READY_TRIP_ONLY.dependencies.mobility_mcp,
      capabilities: ["trip_accessibility", "facility_question"],
      schema_fingerprint: "test-trip-facility-contract",
    },
  },
};

const EVIDENCE: ConversationRunResponse["evidence"] = {
  risk_level: "CAUTION",
  confidence_level: "HIGH",
  failed_sources: [],
  limitations: ["운영 상태는 이동 직전에 다시 확인해야 합니다."],
  unverified_parts: [],
  last_checked_at: "2026-07-11T03:00:00+00:00",
};

const TRIP_CONTEXT: ConversationRunResponse["context"] = {
  schema_version: "2",
  intent: "trip_accessibility",
  origin: {
    query: "시청역",
    station_name: "시청역",
    line: "1호선",
    label: "시청역 · 1호선",
    status: "resolved",
  },
  destination: {
    query: "강남역",
    station_name: "강남역",
    line: "2호선",
    label: "강남역 · 2호선",
    status: "resolved",
  },
  mobility_conditions: [
    {
      field: "wheelchair",
      label: "휠체어 이용",
      value: true,
      provenance: "user_provided",
      provenance_label: "이번 질문에서 제공",
    },
  ],
  facility: null,
  active_clarification: null,
};

export const SUCCESS_RUN: ConversationRunResponse = {
  schema_version: "4",
  request_id: "request-success",
  thread_id: "1".repeat(32),
  run_id: "2".repeat(32),
  run_status: "completed",
  source: "mcp",
  status: "SUCCESS",
  intent: "trip_accessibility",
  user_message: "시청역에서 강남역까지 휠체어 이동 경로를 확인했습니다.",
  clarification_needed: false,
  clarification: null,
  available_partial_info: [],
  evidence: EVIDENCE,
  presentation: {
    schema_version: "2",
    kind: "trip",
    trip: {
      origin: "시청역",
      destination: "강남역",
      route_summary: "엘리베이터가 확인된 환승 경로입니다.",
    },
    safety_notice: "시설 운영 상태는 이동 직전에 다시 확인해 주세요.",
    confidence_reasons: ["경로와 역사 시설 정보를 함께 확인했습니다."],
    evidence_sources: [
      {
        display_name: "교통약자 이동 정보 API",
        source_type: "public_api",
        checked_at: "2026-07-11T03:00:00+00:00",
        success: true,
      },
    ],
  },
  context: TRIP_CONTEXT,
};

export const CLARIFICATION_RUN: ConversationRunResponse = {
  ...SUCCESS_RUN,
  request_id: "request-clarification",
  run_id: "3".repeat(32),
  run_status: "interrupted",
  source: "agent",
  status: "NEEDS_CLARIFICATION",
  user_message: "어느 시청역을 의미하는지 추가 정보가 필요합니다.",
  clarification_needed: true,
  clarification: {
    schema_version: "2",
    id: "origin-station-choice",
    prompts: [
      {
        id: "origin",
        question: "어느 시청역인가요?",
        fields: ["origin"],
      },
    ],
    answer_request: {
      mode: "fields",
      inputs: [
        {
          id: "origin-station",
          prompt_id: "origin",
          field: "origin",
          label: "출발역 선택",
          required: true,
          control: "single_choice",
          options: [
            { value: "CITY_HALL_LINE_1", label: "시청역 · 1호선" },
            { value: "CITY_HALL_LINE_2", label: "시청역 · 2호선" },
          ],
        },
      ],
    },
  },
  presentation: null,
  context: {
    ...TRIP_CONTEXT,
    origin: {
      query: "시청역",
      station_name: null,
      line: null,
      label: "시청역",
      status: "provided",
    },
    active_clarification: {
      id: "origin-station-choice",
      prompt_ids: ["origin"],
      fields: ["origin"],
    },
  },
};

export const BUSINESS_FAILED_RUN: ConversationRunResponse = {
  ...SUCCESS_RUN,
  request_id: "request-business-failed",
  run_id: "4".repeat(32),
  source: "agent",
  status: "FAILED",
  user_message: "현재 계약된 이동 정보 도구로는 요청을 확인하지 못했습니다.",
  presentation: null,
  evidence: {
    risk_level: "UNKNOWN",
    confidence_level: "LOW",
    failed_sources: [],
    limitations: ["요청한 기능이 현재 MCP capability에 없습니다."],
    unverified_parts: ["이동 가능 여부"],
    last_checked_at: null,
  },
};

export const EXECUTION_FAILED_RUN: ConversationRunResponse = {
  ...BUSINESS_FAILED_RUN,
  request_id: "request-execution-failed",
  run_id: "5".repeat(32),
  run_status: "failed",
  user_message: "이동 정보 조회가 중단되었습니다.",
};
