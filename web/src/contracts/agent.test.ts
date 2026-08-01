import { describe, expect, it } from "vitest";

import {
  AgentErrorResponseSchema,
  ClarificationViewSchema,
  ConversationRunResponseSchema,
  CreateThreadResponseSchema,
  ReadinessResponseSchema,
  ResumeRunRequestSchema,
} from "./agent";

const errorResponse = {
  schema_version: "2",
  request_id: "req-1",
  error: {
    code: "THREAD_BUSY",
    message: "먼저 진행 중인 요청을 완료해 주세요.",
    retryable: false,
    detail: {},
  },
} as const;

describe("Agent public contract", () => {
  it("accepts only an empty detail or bounded validation issues", () => {
    expect(AgentErrorResponseSchema.safeParse(errorResponse).success).toBe(true);
    expect(
      AgentErrorResponseSchema.safeParse({
        ...errorResponse,
        error: {
          ...errorResponse.error,
          code: "REQUEST_VALIDATION_ERROR",
          detail: {
            issues: [
              {
                type: "string_too_long",
                location: ["body", "question"],
                message: "질문이 너무 깁니다.",
              },
            ],
          },
        },
      }).success,
    ).toBe(true);
  });

  it("binds validation issues to REQUEST_VALIDATION_ERROR only", () => {
    const validationDetail = {
      issues: [{ type: "missing", location: ["body", "question"], message: "필수 값입니다." }],
    };

    expect(
      AgentErrorResponseSchema.safeParse({
        ...errorResponse,
        error: { ...errorResponse.error, code: "REQUEST_VALIDATION_ERROR", detail: {} },
      }).success,
    ).toBe(false);
    expect(
      AgentErrorResponseSchema.safeParse({
        ...errorResponse,
        error: { ...errorResponse.error, detail: validationDetail },
      }).success,
    ).toBe(false);
    expect(
      AgentErrorResponseSchema.safeParse({
        ...errorResponse,
        error: { ...errorResponse.error, code: "BFF_REQUEST_VALIDATION_ERROR", detail: {} },
      }).success,
    ).toBe(true);
  });

  it.each([
    { thread_id: "a".repeat(32) },
    { run_id: "b".repeat(32) },
    { reason: "internal_policy_branch" },
    { tool: "raw_tool_name" },
    { provider_payload: { secret: "must-not-cross-the-boundary" } },
  ])("rejects arbitrary public error detail: %o", (detail) => {
    const result = AgentErrorResponseSchema.safeParse({
      ...errorResponse,
      error: { ...errorResponse.error, detail },
    });

    expect(result.success).toBe(false);
  });

  it("enforces validation issue count, path depth, and string bounds", () => {
    const issue = { type: "type", location: ["body"], message: "message" };
    const invalidDetails = [
      { issues: [] },
      { issues: Array.from({ length: 33 }, () => issue) },
      { issues: [{ ...issue, type: "x".repeat(101) }] },
      { issues: [{ ...issue, location: Array.from({ length: 17 }, () => "path") }] },
      { issues: [{ ...issue, location: ["x".repeat(101)] }] },
      { issues: [{ ...issue, message: "x".repeat(301) }] },
    ];

    for (const detail of invalidDetails) {
      expect(
        AgentErrorResponseSchema.safeParse({
          ...errorResponse,
          error: { ...errorResponse.error, code: "REQUEST_VALIDATION_ERROR", detail },
        }).success,
      ).toBe(false);
    }
  });

  it("bounds public error codes and messages", () => {
    for (const error of [
      { ...errorResponse.error, code: "lowercase_code" },
      { ...errorResponse.error, code: `A${"A".repeat(100)}` },
      { ...errorResponse.error, message: "" },
      { ...errorResponse.error, message: "x".repeat(501) },
    ]) {
      expect(AgentErrorResponseSchema.safeParse({ ...errorResponse, error }).success).toBe(false);
    }
  });

  it("rejects the broad v1 error contract", () => {
    expect(
      AgentErrorResponseSchema.safeParse({ ...errorResponse, schema_version: "1" }).success,
    ).toBe(false);
  });

  it("rejects a pre-v4 run response", () => {
    const result = ConversationRunResponseSchema.safeParse({ schema_version: "3" });
    expect(result.success).toBe(false);
  });

  it("accepts a strict structured clarification and typed resume answer", () => {
    const clarification = {
      schema_version: "2",
      id: "trip-fields",
      prompts: [
        {
          id: "mobility",
          question: "계단을 이용할 수 있나요?",
          fields: ["can_use_stairs"],
        },
      ],
      answer_request: {
        mode: "fields",
        inputs: [
          {
            id: "stairs-input",
            prompt_id: "mobility",
            field: "can_use_stairs",
            control: "tri_state",
            label: "계단 이용",
            required: true,
            options: [
              { value: "yes", label: "가능" },
              { value: "no", label: "불가능" },
              { value: "unknown", label: "잘 모르겠음" },
            ],
          },
        ],
      },
    } as const;

    expect(ClarificationViewSchema.safeParse(clarification).success).toBe(true);
    expect(
      ResumeRunRequestSchema.safeParse({
        schema_version: "2",
        clarification_id: "trip-fields",
        answer: {
          mode: "fields",
          values: [
            {
              input_id: "stairs-input",
              value: { kind: "tri_state", value: "no" },
            },
          ],
        },
      }).success,
    ).toBe(true);
  });

  it("rejects reordered tri-state options, orphan inputs, and duplicate answers", () => {
    const base = {
      schema_version: "2",
      id: "trip-fields",
      prompts: [
        {
          id: "mobility",
          question: "계단을 이용할 수 있나요?",
          fields: ["can_use_stairs"],
        },
      ],
      answer_request: {
        mode: "fields",
        inputs: [
          {
            id: "stairs-input",
            prompt_id: "mobility",
            field: "can_use_stairs",
            control: "tri_state",
            label: "계단 이용",
            required: true,
            options: [
              { value: "no", label: "불가능" },
              { value: "yes", label: "가능" },
              { value: "unknown", label: "잘 모르겠음" },
            ],
          },
        ],
      },
    };
    expect(ClarificationViewSchema.safeParse(base).success).toBe(false);
    expect(
      ClarificationViewSchema.safeParse({
        ...base,
        answer_request: {
          ...base.answer_request,
          inputs: [{ ...base.answer_request.inputs[0], prompt_id: "missing" }],
        },
      }).success,
    ).toBe(false);
    expect(
      ResumeRunRequestSchema.safeParse({
        schema_version: "2",
        clarification_id: "trip-fields",
        answer: {
          mode: "fields",
          values: [
            { input_id: "stairs-input", value: { kind: "tri_state", value: "yes" } },
            { input_id: "stairs-input", value: { kind: "tri_state", value: "no" } },
          ],
        },
      }).success,
    ).toBe(false);
  });

  it("keeps the legacy single-answer resume request as a BFF migration branch", () => {
    expect(
      ResumeRunRequestSchema.safeParse({
        answer: "시청역",
        clarification_id: "legacy-clarification",
      }).success,
    ).toBe(true);
    expect(
      ResumeRunRequestSchema.safeParse({
        schema_version: "2",
        clarification_id: "structured",
        answer: "legacy-mixed-value",
      }).success,
    ).toBe(false);
  });

  it("accepts durable thread persistence without exposing storage details", () => {
    const result = CreateThreadResponseSchema.safeParse({
      schema_version: "1",
      request_id: "req-1",
      thread_id: "a".repeat(32),
      persistence: "durable",
    });

    expect(result.success).toBe(true);
  });

  it("rejects interrupted state without a clarification", () => {
    const result = ConversationRunResponseSchema.safeParse({
      schema_version: "4",
      request_id: "req-1",
      thread_id: "a".repeat(32),
      run_id: "b".repeat(32),
      run_status: "interrupted",
      source: "agent",
      status: "NEEDS_CLARIFICATION",
      intent: "unknown",
      user_message: "추가 정보가 필요합니다.",
      clarification_needed: true,
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
    });
    expect(result.success).toBe(false);
  });

  it("accepts degraded readiness as a typed readiness result", () => {
    const result = ReadinessResponseSchema.safeParse({
      status: "not_ready",
      service: "agent",
      version: "0.1.0",
      dependencies: {
        mobility_mcp: {
          status: "degraded",
          checked_at: "2026-07-11T00:00:00Z",
          tool_count: 8,
          capabilities: ["trip_accessibility"],
          schema_fingerprint: "sha256:abc",
          error_code: null,
        },
        persistence: {
          status: "ready",
          backend: "postgres",
        },
      },
    });
    expect(result.success).toBe(true);
  });

  it("rejects ready service state when persistence is unavailable", () => {
    const result = ReadinessResponseSchema.safeParse({
      status: "ready",
      service: "agent",
      version: "0.1.0",
      dependencies: {
        mobility_mcp: {
          status: "ready",
          checked_at: "2026-07-11T00:00:00Z",
          tool_count: 8,
          capabilities: ["trip_accessibility"],
          schema_fingerprint: "sha256:abc",
          error_code: null,
        },
        persistence: {
          status: "unavailable",
          backend: "postgres",
        },
      },
    });
    expect(result.success).toBe(false);
  });
});
