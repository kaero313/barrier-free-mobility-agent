// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConversationRunResponse } from "@/contracts/agent";
import {
  BUSINESS_FAILED_RUN,
  EXECUTION_FAILED_RUN,
  SUCCESS_RUN,
} from "@/test/agent-fixtures";

import { ResultSummary } from "./result-summary";

describe("ResultSummary", () => {
  it("keeps PARTIAL distinct from SUCCESS", () => {
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          status: "PARTIAL",
          user_message: "일부 출처만 확인했습니다.",
        }}
      />,
    );

    expect(screen.getByRole("heading", { name: "일부 정보만 확인되었습니다" })).toBeInTheDocument();
    expect(screen.getByText("일부 출처만 확인했습니다.")).toBeInTheDocument();
  });

  it("renders only the public failed-source label", () => {
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          status: "PARTIAL",
          evidence: {
            ...SUCCESS_RUN.evidence,
            failed_sources: [
              {
                source_name: "facility_information",
                display_name: "역 시설 정보",
                recoverable: true,
              },
            ],
          },
        }}
      />,
    );

    expect(screen.getByText("역 시설 정보 · 다시 확인 가능")).toBeInTheDocument();
    expect(screen.queryByText("facility_information")).not.toBeInTheDocument();
    expect(screen.queryByText("elevator_status")).not.toBeInTheDocument();
  });

  it("renders business FAILED without inventing a presentation", () => {
    render(<ResultSummary run={BUSINESS_FAILED_RUN} />);

    expect(screen.getByRole("heading", { name: "현재 확인하지 못했습니다" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "확인된 이동 정보" })).not.toBeInTheDocument();
    expect(screen.getByText(BUSINESS_FAILED_RUN.user_message)).toBeInTheDocument();
  });

  it("labels a FAILED presentation as reference information", () => {
    const failedWithPresentation: ConversationRunResponse = {
      ...SUCCESS_RUN,
      status: "FAILED",
      user_message: "이동 가능 여부는 판단하지 못했지만 일부 시설 정보를 확인했습니다.",
    };
    render(<ResultSummary run={failedWithPresentation} />);

    expect(
      screen.getByText("판단은 완료되지 않았지만 아래 정보는 참고용으로 제공합니다."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "확인된 이동 정보" })).toBeInTheDocument();
  });

  it("keeps execution failure separate and exposes only support-safe identifiers", () => {
    render(<ResultSummary run={EXECUTION_FAILED_RUN} />);

    expect(screen.getByRole("heading", { name: "조회가 중단되었습니다" })).toBeInTheDocument();
    expect(screen.getByText("문제 신고용 정보")).toBeInTheDocument();
    expect(screen.queryByText(/stack/i)).not.toBeInTheDocument();
  });

  it("renders a capability-gated facility projection without a fake pagination action", () => {
    const facilityRun: ConversationRunResponse = {
      ...SUCCESS_RUN,
      intent: "facility_status",
      status: "PARTIAL",
      user_message: "서울역 엘리베이터 상태를 일부 확인했습니다.",
      presentation: {
        schema_version: "2",
        kind: "facility",
        facility: {
          station_name: "서울역",
          line: "1호선",
          question_kind: "status",
          items: [
            {
              facility_type: "ELEVATOR",
              answer_state: "MIXED",
              facilities: [
                {
                  station_name: "서울역",
                  line: "1호선",
                  facility_type: "ELEVATOR",
                  status: "AVAILABLE",
                  location_description: "2번 출입구",
                  inside_gate: false,
                  open_time: null,
                  has_emergency_bell: null,
                },
              ],
              total_count: 7,
              truncated: true,
            },
          ],
        },
        safety_notice: "시설 상태는 출발 직전에 다시 확인해 주세요.",
        evidence_sources: [],
      },
      context: {
        schema_version: "2",
        intent: "facility_status",
        origin: null,
        destination: null,
        mobility_conditions: [],
        facility: {
          target: {
            query: "서울역",
            station_name: "서울역",
            line: "1호선",
            label: "서울역 · 1호선",
            status: "resolved",
          },
          target_provenance: "user_provided",
          target_provenance_label: "이번 질문에서 제공",
          requested_types: ["ELEVATOR"],
          requested_types_provenance: "user_provided",
          requested_types_provenance_label: "이번 질문에서 제공",
          question_kind: "status",
        },
        active_clarification: null,
      },
    };
    render(<ResultSummary run={facilityRun} />);

    expect(screen.getByText("상태 혼재")).toBeInTheDocument();
    expect(screen.getByText("전체 7개 중 1개 표시")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "전체 보기" })).not.toBeInTheDocument();
  });
});
