// @vitest-environment jsdom

import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  PUBLIC_STATUS_TRIP_ONLY,
  PUBLIC_STATUS_WITH_FACILITY,
} from "@/test/agent-fixtures";

import { getPublicStatus } from "./api";
import { CurrentSupportSummary } from "./current-support-summary";
import { PublicStatusView } from "./public-status-view";

vi.mock("./api", () => ({
  getPublicStatus: vi.fn(),
}));

const getPublicStatusMock = vi.mocked(getPublicStatus);

describe("PublicStatusView", () => {
  beforeEach(() => {
    getPublicStatusMock.mockReset();
    getPublicStatusMock.mockResolvedValue(PUBLIC_STATUS_TRIP_ONLY);
  });

  it("deduplicates the initial GET under React StrictMode", async () => {
    render(
      <StrictMode>
        <PublicStatusView />
      </StrictMode>,
    );

    expect(await screen.findByText("이동 경로 조회 가능")).toBeInTheDocument();
    expect(getPublicStatusMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("현재 지원하지 않음")).toBeInTheDocument();
    expect(screen.getByText("이동 조회 서비스")).toBeInTheDocument();
    expect(screen.getByText("이동 정보")).toBeInTheDocument();
    expect(screen.getByText("2026. 7. 11.", { exact: false }).closest("time")).toHaveAttribute(
      "datetime",
      PUBLIC_STATUS_TRIP_ONLY.checked_at,
    );
  });

  it("clears the previous result and sends one GET for a manual refresh", async () => {
    const user = userEvent.setup();
    render(<PublicStatusView />);
    expect(await screen.findByText("이동 경로 조회 가능")).toBeInTheDocument();

    let rejectRefresh: ((reason?: unknown) => void) | undefined;
    getPublicStatusMock.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectRefresh = reject;
        }),
    );
    await user.click(screen.getByRole("button", { name: "상태 새로 확인" }));

    expect(screen.queryByText("이동 경로 조회 가능")).not.toBeInTheDocument();
    expect(screen.getByText("지금 이동 조회를 시작할 수 있는지 확인하고 있습니다.")).toBeInTheDocument();
    expect(getPublicStatusMock).toHaveBeenCalledTimes(2);

    rejectRefresh?.(new Error("network unavailable"));
    expect(await screen.findByText("이전 결과를 현재 상태로 추정하지 않습니다.", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText("이동 경로 조회 가능")).not.toBeInTheDocument();
  });

  it("announces a loaded state without exposing internal implementation terms", async () => {
    render(<PublicStatusView />);
    await screen.findByText("이동 경로 조회 가능");

    const status = screen.getByRole("heading", { name: "이동 조회 상태" }).closest("section");
    expect(status).not.toHaveTextContent(/MCP|Tool|LangGraph|Agent API/);
    expect(status).toHaveAttribute("aria-busy", "false");
  });
});

describe("CurrentSupportSummary", () => {
  beforeEach(() => {
    getPublicStatusMock.mockReset();
  });

  it("does not advertise facility questions when the feature is unsupported", async () => {
    getPublicStatusMock.mockResolvedValue(PUBLIC_STATUS_TRIP_ONLY);
    render(<CurrentSupportSummary />);

    expect(await screen.findByText("현재 이동 경로 조회를 시작할 수 있습니다.")).toBeInTheDocument();
    expect(screen.queryByText("이 환경에서는 역 시설 상태 질문도 지원합니다.")).not.toBeInTheDocument();
  });

  it("shows facility scope only after the public feature says it is available", async () => {
    getPublicStatusMock.mockResolvedValue(PUBLIC_STATUS_WITH_FACILITY);
    render(<CurrentSupportSummary />);

    await waitFor(() => {
      expect(screen.getByText("이 환경에서는 역 시설 상태 질문도 지원합니다.")).toBeInTheDocument();
    });
  });
});
