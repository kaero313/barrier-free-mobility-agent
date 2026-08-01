// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationRunResponse } from "@/contracts/agent";
import {
  CLARIFICATION_RUN,
  PUBLIC_STATUS_TRIP_ONLY,
  PUBLIC_STATUS_UNKNOWN,
  PUBLIC_STATUS_WITH_FACILITY,
  SUCCESS_RUN,
} from "@/test/agent-fixtures";

import {
  AgentClientError,
  createThread,
  deleteThread,
  getPublicStatus,
  getRun,
  resumeRun,
  startRun,
} from "./api";
import { AssistantWorkspace } from "./assistant-workspace";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    createThread: vi.fn(),
    deleteThread: vi.fn(),
    getPublicStatus: vi.fn(),
    getRun: vi.fn(),
    resumeRun: vi.fn(),
    startRun: vi.fn(),
  };
});

const mockedCreateThread = vi.mocked(createThread);
const mockedDeleteThread = vi.mocked(deleteThread);
const mockedGetPublicStatus = vi.mocked(getPublicStatus);
const mockedGetRun = vi.mocked(getRun);
const mockedResumeRun = vi.mocked(resumeRun);
const mockedStartRun = vi.mocked(startRun);
const ASYNC_UI_TIMEOUT = { timeout: 5_000 };
const THREAD_ID = "1".repeat(32);

const FREE_TEXT_CLARIFICATION_RUN: ConversationRunResponse = {
  ...CLARIFICATION_RUN,
  request_id: "request-free-text-clarification",
  run_id: "6".repeat(32),
  user_message: "출발 위치를 구분할 수 있도록 추가 설명이 필요합니다.",
  clarification: {
    schema_version: "2",
    id: "origin-station-free-text",
    prompts: [
      {
        id: "origin-detail",
        question: "출발하려는 시청역의 노선이나 주변 장소를 알려주세요.",
        fields: ["origin"],
      },
    ],
    answer_request: {
      mode: "single_text",
      label: "추가 정보 입력",
      min_length: 1,
      max_length: 20_000,
    },
  },
  context: {
    ...CLARIFICATION_RUN.context,
    active_clarification: {
      id: "origin-station-free-text",
      prompt_ids: ["origin-detail"],
      fields: ["origin"],
    },
  },
};

const STRUCTURED_FIELDS_CLARIFICATION_RUN: ConversationRunResponse = {
  ...CLARIFICATION_RUN,
  request_id: "request-structured-fields",
  run_id: "7".repeat(32),
  user_message: "출발역과 계단·에스컬레이터 이용 조건을 확인해 주세요.",
  clarification: {
    schema_version: "2",
    id: "required-trip-fields",
    prompts: [
      { id: "origin", question: "어디에서 출발하나요?", fields: ["origin"] },
      {
        id: "mobility",
        question: "계단과 에스컬레이터를 각각 이용할 수 있나요?",
        fields: ["can_use_stairs", "can_use_escalator"],
      },
    ],
    answer_request: {
      mode: "fields",
      inputs: [
        {
          id: "origin-input",
          prompt_id: "origin",
          field: "origin",
          control: "text",
          label: "출발역",
          required: true,
          min_length: 1,
          max_length: 120,
        },
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
        {
          id: "escalator-input",
          prompt_id: "mobility",
          field: "can_use_escalator",
          control: "tri_state",
          label: "에스컬레이터 이용",
          required: true,
          options: [
            { value: "yes", label: "가능" },
            { value: "no", label: "불가능" },
            { value: "unknown", label: "잘 모르겠음" },
          ],
        },
      ],
    },
  },
  context: {
    ...CLARIFICATION_RUN.context,
    active_clarification: {
      id: "required-trip-fields",
      prompt_ids: ["origin", "mobility"],
      fields: ["origin", "can_use_stairs", "can_use_escalator"],
    },
  },
};

const NEXT_CLARIFICATION_RUN: ConversationRunResponse = {
  ...FREE_TEXT_CLARIFICATION_RUN,
  request_id: "request-latest-clarification",
  user_message: "최신 이동 조건을 다시 확인해 주세요.",
  clarification: {
    ...FREE_TEXT_CLARIFICATION_RUN.clarification!,
    id: "latest-free-text",
    prompts: [
      {
        id: "latest-origin",
        question: "현재 출발역을 다시 입력해 주세요.",
        fields: ["origin"],
      },
    ],
  },
  context: {
    ...FREE_TEXT_CLARIFICATION_RUN.context,
    active_clarification: {
      id: "latest-free-text",
      prompt_ids: ["latest-origin"],
      fields: ["origin"],
    },
  },
};

function getWorkspace(state: "empty" | "clarifying" | "result"): HTMLElement {
  const workspace = document.querySelector<HTMLElement>(
    `[data-ui="assistant-workspace"][data-state="${state}"]`,
  );
  expect(workspace).not.toBeNull();
  return workspace as HTMLElement;
}

function expectBefore(earlier: Element, later: Element) {
  expect(
    Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING),
  ).toBe(true);
}

async function waitUntilTripReady() {
  const submit = await screen.findByRole(
    "button",
    { name: "조회 시작" },
    ASYNC_UI_TIMEOUT,
  );
  await waitFor(
    () => expect(submit).toHaveAttribute("aria-disabled", "false"),
    ASYNC_UI_TIMEOUT,
  );
  return submit;
}

async function submitInitialQuestion(question: string) {
  const user = userEvent.setup();
  const submit = await waitUntilTripReady();
  await user.type(screen.getByLabelText("이동 질문"), question);
  await user.click(submit);
  return user;
}

describe("AssistantWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetPublicStatus.mockResolvedValue(PUBLIC_STATUS_TRIP_ONLY);
    mockedCreateThread.mockResolvedValue({
      schema_version: "1",
      request_id: "request-create",
      thread_id: THREAD_ID,
      persistence: "ephemeral",
    });
    mockedDeleteThread.mockResolvedValue(undefined);
    mockedStartRun.mockResolvedValue(SUCCESS_RUN);
    mockedResumeRun.mockResolvedValue(SUCCESS_RUN);
  });

  it("starts as a focused empty experience without creating a thread or exposing internal UI", async () => {
    render(<AssistantWorkspace />);

    await waitUntilTripReady();

    const workspace = getWorkspace("empty");
    expect(
      screen.getByRole("heading", { name: "이동 가능한 경로를 확인해 보세요" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "어떤 이동을 확인할까요?" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "이동 경로 조회 가능" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "확인한 이동 조건" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "판단 근거와 정보 한계" })).not.toBeInTheDocument();
    expect(screen.queryByText("판단 대기 중")).not.toBeInTheDocument();
    expect(workspace.querySelector('[data-ui="result-summary"]')).not.toBeInTheDocument();
    expect(workspace).not.toHaveTextContent(/\b(?:Agent|MCP Tool|LangGraph)\b/);
    expect(mockedCreateThread).not.toHaveBeenCalled();
    expect(mockedStartRun).not.toHaveBeenCalled();
  });

  it("performs one initial public status lookup when React replays effects", async () => {
    render(
      <StrictMode>
        <AssistantWorkspace />
      </StrictMode>,
    );

    await waitUntilTripReady();

    expect(mockedGetPublicStatus).toHaveBeenCalledTimes(1);
  });

  it("lazy-creates a thread only when the first question is submitted", async () => {
    render(<AssistantWorkspace />);

    await submitInitialQuestion("휠체어로 시청역에서 강남역까지 갈 수 있나요?");

    expect(await screen.findByText(SUCCESS_RUN.user_message)).toBeInTheDocument();
    expect(mockedCreateThread).toHaveBeenCalledTimes(1);
    expect(mockedStartRun).toHaveBeenCalledWith(THREAD_ID, {
      question: "휠체어로 시청역에서 강남역까지 갈 수 있나요?",
    }, expect.stringMatching(/^[0-9a-f-]{36}$/));
  });

  it("shows context, canonical clarification, and one unselected choice form in order", async () => {
    mockedStartRun.mockResolvedValue(CLARIFICATION_RUN);
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 강남역까지 갈 수 있나요?");

    const clarificationHeading = await screen.findByRole(
      "heading",
      { name: "추가 정보가 필요합니다" },
      ASYNC_UI_TIMEOUT,
    );
    const workspace = getWorkspace("clarifying");
    const context = workspace.querySelector(
      'section[aria-labelledby="conversation-context-title"]',
    );
    const transcript = workspace.querySelector('ol[aria-label="대화 기록"]');
    expect(context).not.toBeNull();
    expect(transcript).not.toBeNull();
    expectBefore(context as Element, transcript as Element);
    expectBefore(transcript as Element, clarificationHeading);
    expect(screen.getAllByText(CLARIFICATION_RUN.user_message)).toHaveLength(1);
    expect(screen.queryByLabelText("이동 질문")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("후속 질문")).not.toBeInTheDocument();
    expect(
      screen.getByText("추가 질문을 확인하는 동안 새 질문 입력은 잠겨 있습니다."),
    ).toBeInTheDocument();
    await waitFor(() => expect(clarificationHeading).toHaveFocus(), ASYNC_UI_TIMEOUT);

    const group = screen.getByRole("group", { name: "출발역 선택" });
    const radios = screen.getAllByRole("radio");
    radios.forEach((radio) => expect(radio).not.toBeChecked());

    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));
    const validationError = await screen.findByRole("alert");
    expect(validationError).toHaveTextContent("입력 내용을 확인해 주세요.");
    expect(group).toHaveAttribute("aria-invalid", "true");

    await user.click(screen.getByRole("radio", { name: "시청역 · 1호선" }));
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    await waitFor(() => {
      expect(mockedResumeRun).toHaveBeenCalledWith(CLARIFICATION_RUN.run_id, {
        schema_version: "2",
        clarification_id: "origin-station-choice",
        answer: {
          mode: "fields",
          values: [
            {
              input_id: "origin-station",
              value: { kind: "choice", value: "CITY_HALL_LINE_1" },
            },
          ],
        },
      }, expect.stringMatching(/^[0-9a-f-]{36}$/));
    });
  });

  it("validates and submits a free-text clarification with its clarification id", async () => {
    mockedStartRun.mockResolvedValue(FREE_TEXT_CLARIFICATION_RUN);
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 출발하고 싶어요.");
    const field = await screen.findByLabelText("추가 정보 입력", undefined, ASYNC_UI_TIMEOUT);

    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));
    const validationError = await screen.findByRole("alert");
    expect(validationError).toHaveTextContent("계속하려면 추가 정보를 입력해 주세요.");
    expect(field).toHaveAttribute("aria-invalid", "true");
    const describedError = document.getElementById(field.getAttribute("aria-describedby") ?? "");
    expect(describedError).toHaveTextContent("계속하려면 추가 정보를 입력해 주세요.");

    await user.type(field, "1호선 시청역 승강장");
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    await waitFor(() => {
      expect(mockedResumeRun).toHaveBeenCalledWith(FREE_TEXT_CLARIFICATION_RUN.run_id, {
        schema_version: "2",
        clarification_id: "origin-station-free-text",
        answer: { mode: "single_text", value: "1호선 시청역 승강장" },
      }, expect.stringMatching(/^[0-9a-f-]{36}$/));
    });
  });

  it("submits server-described text and tri-state fields without parsing prompt text", async () => {
    mockedStartRun.mockResolvedValue(STRUCTURED_FIELDS_CLARIFICATION_RUN);
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("강남역까지 이동하고 싶어요.");
    const origin = await screen.findByLabelText("출발역", undefined, ASYNC_UI_TIMEOUT);

    fireEvent.keyDown(origin, {
      key: "Enter",
      code: "Enter",
      isComposing: true,
      keyCode: 229,
    });
    expect(mockedResumeRun).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("출발역을(를) 입력해 주세요.");
    await waitFor(() => expect(origin).toHaveFocus(), ASYNC_UI_TIMEOUT);

    await user.type(origin, "시청역");
    const stairs = screen.getByRole("group", { name: "계단 이용" });
    const escalator = screen.getByRole("group", { name: "에스컬레이터 이용" });
    await user.click(within(stairs).getByRole("radio", { name: "불가능" }));
    await user.click(within(escalator).getByRole("radio", { name: "잘 모르겠음" }));
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    await waitFor(() => {
      expect(mockedResumeRun).toHaveBeenCalledWith(
        STRUCTURED_FIELDS_CLARIFICATION_RUN.run_id,
        {
          schema_version: "2",
          clarification_id: "required-trip-fields",
          answer: {
            mode: "fields",
            values: [
              { input_id: "origin-input", value: { kind: "text", value: "시청역" } },
              { input_id: "stairs-input", value: { kind: "tri_state", value: "no" } },
              {
                input_id: "escalator-input",
                value: { kind: "tri_state", value: "unknown" },
              },
            ],
          },
        },
        expect.stringMatching(/^[0-9a-f-]{36}$/),
      );
    });
  });

  it("maps request validation issues to the active structured input", async () => {
    mockedStartRun.mockResolvedValue(STRUCTURED_FIELDS_CLARIFICATION_RUN);
    mockedResumeRun.mockRejectedValue(
      new AgentClientError("입력 내용을 확인해 주세요.", {
        kind: "agent",
        status: 422,
        code: "REQUEST_VALIDATION_ERROR",
        validationIssues: [
          {
            type: "clarification_answer_invalid_option",
            location: ["body", "answer", "values", "stairs-input"],
            message: "현재 선택값을 사용할 수 없습니다.",
          },
        ],
      }),
    );
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("강남역까지 이동하고 싶어요.");
    await user.type(await screen.findByLabelText("출발역"), "시청역");
    const stairs = screen.getByRole("group", { name: "계단 이용" });
    const escalator = screen.getByRole("group", { name: "에스컬레이터 이용" });
    await user.click(within(stairs).getByRole("radio", { name: "가능" }));
    await user.click(within(escalator).getByRole("radio", { name: "가능" }));
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "현재 선택값을 사용할 수 없습니다.",
    );
    await waitFor(
      () => expect(within(stairs).getByRole("radio", { name: "가능" })).toHaveFocus(),
      ASYNC_UI_TIMEOUT,
    );
    expect(screen.getByLabelText("출발역")).toHaveValue("시청역");
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).not.toHaveBeenCalled();
  });

  it("reconciles a stale clarification once without reposting and preserves the rejected draft", async () => {
    mockedStartRun.mockResolvedValue(FREE_TEXT_CLARIFICATION_RUN);
    mockedResumeRun.mockRejectedValue(
      new AgentClientError("추가 질문이 변경되었습니다.", {
        kind: "agent",
        status: 409,
        requestId: "request-stale",
        code: "STALE_CLARIFICATION",
      }),
    );
    mockedGetRun.mockResolvedValue(NEXT_CLARIFICATION_RUN);
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 출발하고 싶어요.");
    const input = await screen.findByLabelText("추가 정보 입력", undefined, ASYNC_UI_TIMEOUT);
    await user.type(input, "1호선 시청역 승강장");
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    expect(
      await screen.findByRole("heading", { name: "추가 질문이 변경되었습니다" }),
    ).toBeInTheDocument();
    expect(screen.getByText("현재 출발역을 다시 입력해 주세요.")).toBeInTheDocument();
    const preserved = screen.getByText("자동 재전송하지 않은 이전 입력");
    await user.click(preserved);
    expect(screen.getByText("1호선 시청역 승강장")).toBeInTheDocument();
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledTimes(1);
  });

  it("reconciles an ambiguous resume with one read-only run lookup and no POST retry", async () => {
    mockedStartRun.mockResolvedValue(FREE_TEXT_CLARIFICATION_RUN);
    mockedResumeRun.mockRejectedValue(
      new AgentClientError("transport failed", { kind: "transport" }),
    );
    mockedGetRun.mockResolvedValue({
      ...SUCCESS_RUN,
      run_id: FREE_TEXT_CLARIFICATION_RUN.run_id,
      thread_id: FREE_TEXT_CLARIFICATION_RUN.thread_id,
    });
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 출발하고 싶어요.");
    const input = await screen.findByLabelText("추가 정보 입력", undefined, ASYNC_UI_TIMEOUT);
    await user.type(input, "1호선 시청역 승강장");
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    expect(await screen.findByText(SUCCESS_RUN.user_message)).toBeInTheDocument();
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledWith(FREE_TEXT_CLARIFICATION_RUN.run_id);
  });

  it("polls a known run within a bound until an ambiguous resume advances", async () => {
    mockedStartRun.mockResolvedValue(FREE_TEXT_CLARIFICATION_RUN);
    mockedResumeRun.mockRejectedValue(
      new AgentClientError("요청을 안전하게 접수했으며 처리 중입니다.", {
        kind: "agent",
        status: 409,
        code: "EXECUTION_IN_PROGRESS",
        retryable: true,
      }),
    );
    mockedGetRun
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValueOnce({
        ...SUCCESS_RUN,
        run_id: FREE_TEXT_CLARIFICATION_RUN.run_id,
        thread_id: FREE_TEXT_CLARIFICATION_RUN.thread_id,
      });
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 출발하고 싶어요.");
    const input = await screen.findByLabelText("추가 정보 입력", undefined, ASYNC_UI_TIMEOUT);
    await user.type(input, "1호선 시청역 승강장");
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    expect(
      await screen.findByText(SUCCESS_RUN.user_message, undefined, ASYNC_UI_TIMEOUT),
    ).toBeInTheDocument();
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledTimes(3);
  });

  it("lets the user recheck a still-running resume with GET only", async () => {
    mockedStartRun.mockResolvedValue(FREE_TEXT_CLARIFICATION_RUN);
    mockedResumeRun.mockRejectedValue(
      new AgentClientError("요청을 안전하게 접수했으며 처리 중입니다.", {
        kind: "agent",
        status: 409,
        code: "EXECUTION_IN_PROGRESS",
        retryable: true,
      }),
    );
    mockedGetRun
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValueOnce(FREE_TEXT_CLARIFICATION_RUN)
      .mockResolvedValue({
        ...SUCCESS_RUN,
        run_id: FREE_TEXT_CLARIFICATION_RUN.run_id,
        thread_id: FREE_TEXT_CLARIFICATION_RUN.thread_id,
      });
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 출발하고 싶어요.");
    const input = await screen.findByLabelText("추가 정보 입력", undefined, ASYNC_UI_TIMEOUT);
    await user.type(input, "1호선 시청역 승강장");
    await user.click(screen.getByRole("button", { name: "입력한 정보로 계속" }));

    const resultCheck = await screen.findByRole(
      "button",
      { name: "접수한 요청 결과 확인" },
      ASYNC_UI_TIMEOUT,
    );
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledTimes(4);

    await user.click(resultCheck);

    expect(await screen.findByText(SUCCESS_RUN.user_message)).toBeInTheDocument();
    expect(mockedResumeRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).toHaveBeenCalledTimes(5);
  });

  it("orders the result body, public evidence, and follow-up composer without duplicating canonical text", async () => {
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("휠체어로 시청역에서 강남역까지 갈 수 있나요?");
    await screen.findByRole(
      "heading",
      { name: "확인이 완료되었습니다" },
      ASYNC_UI_TIMEOUT,
    );

    const workspace = getWorkspace("result");
    const summary = workspace.querySelector('[data-ui="result-summary"]');
    const resultBody = summary?.querySelector('section[aria-labelledby="result-title"]');
    const evidence = summary?.querySelector('aside[aria-labelledby="evidence-title"]');
    const followupForm = screen.getByLabelText("후속 질문").closest("form");
    expect(summary).not.toBeNull();
    expect(resultBody).not.toBeNull();
    expect(evidence).not.toBeNull();
    expect(followupForm).not.toBeNull();
    expectBefore(resultBody as Element, evidence as Element);
    expectBefore(summary as Element, followupForm as Element);
    expect(screen.getAllByText(SUCCESS_RUN.user_message)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "새 이동 조회" }));

    await waitFor(() => expect(mockedDeleteThread).toHaveBeenCalledWith(THREAD_ID));
    const pageHeading = screen.getByRole("heading", {
      name: "이동 가능한 경로를 확인해 보세요",
    });
    await waitFor(() => expect(pageHeading).toHaveFocus(), ASYNC_UI_TIMEOUT);
    expect(getWorkspace("empty")).toBeInTheDocument();
    expect(document.querySelector('[data-ui="result-summary"]')).not.toBeInTheDocument();
  });

  it("does not submit Ctrl+Enter while a Korean IME composition is active", async () => {
    const user = userEvent.setup();
    render(<AssistantWorkspace />);

    await waitUntilTripReady();
    const composer = screen.getByLabelText("이동 질문");
    await user.type(composer, "시청역에서 강남역까지 갈 수 있나요?");

    fireEvent.keyDown(composer, {
      key: "Enter",
      code: "Enter",
      ctrlKey: true,
      isComposing: true,
    });
    expect(mockedCreateThread).not.toHaveBeenCalled();
    expect(mockedStartRun).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, {
      key: "Enter",
      code: "Enter",
      ctrlKey: true,
      isComposing: false,
    });
    await waitFor(() => expect(mockedStartRun).toHaveBeenCalledTimes(1));
  });

  it("does not automatically retry an outcome-unknown POST", async () => {
    mockedStartRun.mockRejectedValue(
      new AgentClientError("transport failed", { kind: "transport" }),
    );
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 강남역까지 갈 수 있나요?");

    await screen.findByRole(
      "heading",
      { name: "질문 처리 결과 확인 불가" },
      ASYNC_UI_TIMEOUT,
    );
    expect(mockedStartRun).toHaveBeenCalledTimes(1);
    const idempotencyKey = mockedStartRun.mock.calls[0]?.[2];
    expect(idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(document.body).not.toHaveTextContent(idempotencyKey ?? "unreachable");
    const submit = screen.getByRole("button", { name: "조회 시작" });
    expect(submit).toHaveAttribute("aria-disabled", "true");
    await user.click(submit);
    expect(mockedStartRun).toHaveBeenCalledTimes(1);
    expect(mockedGetRun).not.toHaveBeenCalled();
  });

  it("keeps a bounded-wait in-progress submission locked without another POST", async () => {
    mockedStartRun.mockRejectedValue(
      new AgentClientError("요청을 안전하게 접수했으며 처리 중입니다.", {
        kind: "agent",
        status: 409,
        code: "EXECUTION_IN_PROGRESS",
        retryable: true,
      }),
    );
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 강남역까지 갈 수 있나요?");

    await screen.findByRole(
      "heading",
      { name: "질문 처리 결과 확인 불가" },
      ASYNC_UI_TIMEOUT,
    );
    expect(mockedStartRun).toHaveBeenCalledTimes(1);
    const submit = screen.getByRole("button", { name: "조회 시작" });
    expect(submit).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "접수한 요청 결과 확인" })).toBeInTheDocument();
    await user.click(submit);
    expect(mockedStartRun).toHaveBeenCalledTimes(1);
  });

  it("rejoins an in-progress start only after explicit confirmation with the same key", async () => {
    mockedStartRun
      .mockRejectedValueOnce(
        new AgentClientError("요청을 안전하게 접수했으며 처리 중입니다.", {
          kind: "agent",
          status: 409,
          code: "EXECUTION_IN_PROGRESS",
          retryable: true,
        }),
      )
      .mockResolvedValueOnce(SUCCESS_RUN);
    render(<AssistantWorkspace />);

    const user = await submitInitialQuestion("시청역에서 강남역까지 갈 수 있나요?");
    const resultCheck = await screen.findByRole(
      "button",
      { name: "접수한 요청 결과 확인" },
      ASYNC_UI_TIMEOUT,
    );
    const originalKey = mockedStartRun.mock.calls[0]?.[2];

    expect(mockedStartRun).toHaveBeenCalledTimes(1);
    await user.click(resultCheck);

    expect(await screen.findByText(SUCCESS_RUN.user_message)).toBeInTheDocument();
    expect(mockedStartRun).toHaveBeenCalledTimes(2);
    expect(mockedStartRun.mock.calls[1]?.[2]).toBe(originalKey);
    expect(mockedCreateThread).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "접수한 요청 결과 확인" }))
      .not.toBeInTheDocument();
  });

  it("shows the facility example only when the public feature is available", async () => {
    mockedGetPublicStatus.mockResolvedValue(PUBLIC_STATUS_UNKNOWN);
    const unknownStatus = render(<AssistantWorkspace />);

    await screen.findByRole(
      "heading",
      { name: "서비스 상태를 확인하지 못함" },
      ASYNC_UI_TIMEOUT,
    );
    expect(screen.queryByRole("button", { name: "역 시설 상태 확인" })).not.toBeInTheDocument();

    unknownStatus.unmount();
    mockedGetPublicStatus.mockResolvedValue(PUBLIC_STATUS_WITH_FACILITY);
    render(<AssistantWorkspace />);

    expect(
      await screen.findByRole(
        "button",
        { name: "역 시설 상태 확인" },
        ASYNC_UI_TIMEOUT,
      ),
    ).toBeInTheDocument();
  });
});
