// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConversationContextView } from "@/contracts/agent";
import { SUCCESS_RUN } from "@/test/agent-fixtures";

import { ConversationContextSummary } from "./conversation-context-summary";

const CONTEXT: ConversationContextView = {
  ...SUCCESS_RUN.context,
  destination: { ...SUCCESS_RUN.context.destination!, status: "provided" },
  mobility_conditions: [
    ...SUCCESS_RUN.context.mobility_conditions,
    {
      field: "can_use_stairs",
      label: "계단 이용 가능",
      value: false,
      provenance: "user_confirmed",
      provenance_label: "추가 질문에서 확인",
    },
  ],
};

describe("ConversationContextSummary", () => {
  it("reads a false condition as a limitation instead of 'not applicable'", () => {
    render(<ConversationContextSummary context={CONTEXT} />);

    expect(screen.getByText("휠체어 사용")).toBeInTheDocument();
    expect(screen.getByText("계단 이용 불가")).toBeInTheDocument();
    expect(screen.queryByText(/해당 없음/)).not.toBeInTheDocument();
    expect(screen.queryByText("추가 질문에서 확인")).not.toBeInTheDocument();
  });

  it("flags only stations that are not resolved yet", () => {
    render(<ConversationContextSummary context={CONTEXT} />);

    expect(screen.getAllByText("확인 전")).toHaveLength(1);
    expect(screen.queryByText("확인됨")).not.toBeInTheDocument();
  });
});
