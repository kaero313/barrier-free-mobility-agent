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

const FACILITY_RUN: ConversationRunResponse = {
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

  it("labels a FAILED facility presentation as reference information", () => {
    render(
      <ResultSummary
        run={{
          ...FACILITY_RUN,
          source: "agent",
          status: "FAILED",
          user_message: "엘리베이터 상태는 판단하지 못했지만 일부 시설 정보를 확인했습니다.",
        }}
      />,
    );

    expect(
      screen.getByText("판단은 완료되지 않았지만 아래 정보는 참고용으로 제공합니다."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "확인된 시설 정보" })).toBeInTheDocument();
  });

  it("shows only the answer for a FAILED trip, without a route strip or extra notices", () => {
    const trip = SUCCESS_RUN.presentation;
    if (trip?.kind !== "trip") throw new Error("SUCCESS_RUN must carry a trip presentation");
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          status: "FAILED",
          user_message: "입력한 역·호선과 맞는 지하철 경로를 찾지 못했습니다.",
          presentation: {
            ...trip,
            trip: { ...trip.trip, route_summary: "유효한 지하철 경로 후보를 확인하지 못했습니다." },
          },
        }}
      />,
    );

    // 실패한 경로 결과의 경로 요약은 실패 이유를 되풀이하므로 참고 정보로 붙이지 않는다.
    expect(screen.queryByRole("heading", { name: "확인된 이동 정보" })).not.toBeInTheDocument();
    expect(screen.queryByText("유효한 지하철 경로 후보를 확인하지 못했습니다.")).not.toBeInTheDocument();
    expect(screen.queryByText(/참고용으로 제공합니다/)).not.toBeInTheDocument();
    expect(screen.queryByText("안전 안내")).not.toBeInTheDocument();
    // 판단하지 못했다는 사실은 제목이 말하므로 위험도·자료 신뢰도를 다시 늘어놓지 않는다.
    expect(screen.queryByText("위험도")).not.toBeInTheDocument();
    expect(screen.queryByText("자료 신뢰도")).not.toBeInTheDocument();
  });

  it("keeps execution failure separate and exposes only support-safe identifiers", () => {
    render(<ResultSummary run={EXECUTION_FAILED_RUN} />);

    expect(screen.getByRole("heading", { name: "조회가 중단되었습니다" })).toBeInTheDocument();
    expect(screen.getByText("문제 신고용 정보")).toBeInTheDocument();
    expect(screen.queryByText(/stack/i)).not.toBeInTheDocument();
  });

  it("states the status once and keeps the safety notice only when the answer lacks it", () => {
    const { rerender } = render(<ResultSummary run={SUCCESS_RUN} />);

    // 위험도 CAUTION 인 SUCCESS 는 완료 표시가 아니라 확인할 점이 있다는 제목을 한 번만 보인다.
    expect(screen.getAllByText("출발 전에 확인할 점이 있습니다")).toHaveLength(1);
    expect(screen.queryByText("확인이 완료되었습니다")).not.toBeInTheDocument();
    expect(screen.getByText("안전 안내")).toBeInTheDocument();

    const safetyNotice = SUCCESS_RUN.presentation!.safety_notice;
    rerender(
      <ResultSummary
        run={{ ...SUCCESS_RUN, user_message: `경로를 확인했습니다.\n\n> ${safetyNotice}` }}
      />,
    );

    expect(screen.queryByText("안전 안내")).not.toBeInTheDocument();
    expect(screen.getAllByText(safetyNotice)).toHaveLength(1);
  });

  it.each([
    ["LOW", "확인된 범위에서 큰 위험 요소는 없습니다"],
    ["CAUTION", "출발 전에 확인할 점이 있습니다"],
    ["HIGH", "이동에 어려움이 예상됩니다"],
    ["UNKNOWN", "이동 가능 여부를 판단하지 못했습니다"],
    [null, "조회를 마쳤습니다"],
  ] as const)("titles a SUCCESS result with risk %s by what the rider should do", (risk, title) => {
    render(
      <ResultSummary run={{ ...SUCCESS_RUN, evidence: { ...SUCCESS_RUN.evidence, risk_level: risk } }} />,
    );

    expect(screen.getByRole("heading", { level: 2, name: title })).toBeInTheDocument();
  });

  it("merges repeated sources and moves condition provenance into the details", () => {
    const presentation = SUCCESS_RUN.presentation!;
    const limitation = "대표 접근성 시설만 포함했습니다.";
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          // Agent가 만든 답변은 본문에 기준 시각이 없으므로 상세에 출처와 제한 사항을 보여 준다.
          source: "agent",
          evidence: {
            ...SUCCESS_RUN.evidence,
            limitations: [limitation],
            unverified_parts: [limitation, "승강장 연결은 확인하지 못했습니다."],
          },
          presentation: {
            ...presentation,
            evidence_sources: [
              ...presentation.evidence_sources,
              { ...presentation.evidence_sources[0]!, source_type: "cache" },
            ],
          },
        }}
      />,
    );

    expect(screen.getAllByText(/교통약자 이동 정보 API/)).toHaveLength(1);
    expect(screen.getAllByText(limitation)).toHaveLength(1);
    expect(screen.getByText("승강장 연결은 확인하지 못했습니다.")).toBeInTheDocument();
    expect(screen.getByText("휠체어 사용 · 이번 질문에서 제공")).toBeInTheDocument();
  });

  it("does not repeat the MCP answer's checked times and limitations in the details", () => {
    const limitation = "대표 접근성 시설만 포함했습니다.";
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          evidence: {
            ...SUCCESS_RUN.evidence,
            limitations: [limitation],
            unverified_parts: [limitation, "승강장 연결은 확인하지 못했습니다."],
          },
        }}
      />,
    );

    expect(SUCCESS_RUN.source).toBe("mcp");
    expect(screen.queryByText(limitation)).not.toBeInTheDocument();
    expect(screen.queryByText("확인 출처")).not.toBeInTheDocument();
    expect(screen.queryByText(/마지막 확인/)).not.toBeInTheDocument();
    expect(screen.getByText("승강장 연결은 확인하지 못했습니다.")).toBeInTheDocument();
    expect(screen.getByText("휠체어 사용 · 이번 질문에서 제공")).toBeInTheDocument();
    expect(screen.getByText("문제 신고용 정보")).toBeInTheDocument();
  });

  it("puts the checked conditions beside the evidence, after the answer", () => {
    const { container } = render(<ResultSummary run={SUCCESS_RUN} />);

    const aside = container.querySelector('[data-ui="result-aside"]');
    const answer = container.querySelector('section[aria-labelledby="result-title"]');
    const context = aside?.querySelector('section[aria-labelledby="conversation-context-title"]');
    expect(context).not.toBeNull();
    expect(aside?.querySelector('aside[aria-labelledby="evidence-title"]')).not.toBeNull();
    // 답변이 먼저 읽히고, 이동 조건과 근거가 뒤따른다.
    expect(
      (answer as Element).compareDocumentPosition(context as Element) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("does not add a route strip that repeats the stations and the transfer section", () => {
    render(<ResultSummary run={SUCCESS_RUN} />);

    expect(screen.queryByRole("heading", { name: "확인된 이동 정보" })).not.toBeInTheDocument();
    expect(screen.queryByText("엘리베이터가 확인된 환승 경로입니다.")).not.toBeInTheDocument();
  });

  it("does not repeat the risk level that the title already states", () => {
    const { rerender } = render(<ResultSummary run={SUCCESS_RUN} />);

    // 위험도 CAUTION 은 제목 "출발 전에 확인할 점이 있습니다"가 이미 말한다.
    expect(screen.queryByText("위험도")).not.toBeInTheDocument();
    expect(screen.getByText("자료 신뢰도")).toBeInTheDocument();

    rerender(<ResultSummary run={{ ...SUCCESS_RUN, status: "PARTIAL" }} />);

    // PARTIAL 제목은 위험도를 말하지 않으므로 근거 영역에 위험도를 남긴다.
    expect(screen.getByText("위험도")).toBeInTheDocument();
    expect(screen.getByText("주의")).toBeInTheDocument();
  });

  it("does not repeat partial information that the answer already contains", () => {
    const repeated = "경로를 확인하지 못해 역별 엘리베이터 정보는 조회하지 않았습니다.";
    const extra = "출발역과 도착역은 확인했습니다.";
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          status: "PARTIAL",
          user_message: `경로를 찾지 못했습니다.\n\n### 확인 결과\n\n- ${repeated}`,
          available_partial_info: [repeated, extra],
        }}
      />,
    );

    expect(screen.getAllByText(repeated)).toHaveLength(1);
    expect(screen.getByText(extra)).toBeInTheDocument();
  });

  it("does not list facilities again under an MCP answer that already tables them", () => {
    render(<ResultSummary run={{ ...FACILITY_RUN, status: "FAILED" }} />);

    // MCP 시설 답변 본문은 시설 표를 이미 담으므로 같은 목록과 그 목록을 가리키는 안내를 더하지 않는다.
    expect(screen.queryByRole("heading", { name: "확인된 시설 정보" })).not.toBeInTheDocument();
    expect(
      screen.queryByText("판단은 완료되지 않았지만 아래 정보는 참고용으로 제공합니다."),
    ).not.toBeInTheDocument();
  });

  it("omits the risk and confidence block when the result has no such judgement", () => {
    render(
      <ResultSummary
        run={{
          ...SUCCESS_RUN,
          evidence: { ...SUCCESS_RUN.evidence, risk_level: null, confidence_level: null },
        }}
      />,
    );

    expect(screen.queryByRole("heading", { name: "판단 수준" })).not.toBeInTheDocument();
    expect(screen.queryByText("제공되지 않음")).not.toBeInTheDocument();
  });

  it("renders a capability-gated facility projection without a fake pagination action", () => {
    render(<ResultSummary run={{ ...FACILITY_RUN, source: "agent" }} />);

    // 조회 역은 현재 이동 조건 카드가 보여 주므로 시설 결과 제목에서 반복하지 않는다.
    expect(screen.getByRole("heading", { name: "확인된 시설 정보" })).toBeInTheDocument();
    expect(screen.getByText("상태 혼재")).toBeInTheDocument();
    expect(screen.getByText("전체 7개 중 1개 표시")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "전체 보기" })).not.toBeInTheDocument();
  });
});
