import { describe, expect, it } from "vitest";

import { SUCCESS_RUN } from "@/test/agent-fixtures";

import { ConversationRunResponseSchema } from "./agent";

describe("failed source public contract", () => {
  const publicFailedSource = {
    source_name: "facility_information",
    display_name: "역 시설 정보",
    recoverable: true,
  } as const;

  const partialRun = {
    ...SUCCESS_RUN,
    status: "PARTIAL",
    evidence: {
      ...SUCCESS_RUN.evidence,
      failed_sources: [publicFailedSource],
    },
  } as const;

  it("accepts a stable public code and bounded display name", () => {
    expect(ConversationRunResponseSchema.safeParse(partialRun).success).toBe(true);
  });

  it.each([
    { ...publicFailedSource, source_name: "elevator_status" },
    { source_name: "facility_information", recoverable: true },
    { ...publicFailedSource, display_name: "x".repeat(81) },
    { ...publicFailedSource, reason: "private upstream reason" },
  ])("rejects internal or over-broad failed-source data: %o", (failedSource) => {
    const result = ConversationRunResponseSchema.safeParse({
      ...partialRun,
      evidence: {
        ...partialRun.evidence,
        failed_sources: [failedSource],
      },
    });

    expect(result.success).toBe(false);
  });
});
