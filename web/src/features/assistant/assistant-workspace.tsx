"use client";

import {
  CheckIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  InfoIcon,
  LockIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SendIcon,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { Disclosure } from "@/components/disclosure";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Message, MessageContent, MessageHeader } from "@/components/ui/message";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type {
  AgentValidationIssue,
  ConversationRunResponse,
} from "@/contracts/agent";
import type { PublicServiceStatus } from "@/contracts/public-status";

import {
  createThread,
  createIdempotencyKey,
  deleteThread,
  getPublicStatus,
  getRun,
  isAgentClientError,
  resumeRun,
  startRun,
} from "./api";
import { CanonicalMarkdown } from "./canonical-markdown";
import { ClarificationForm, type ClarificationAnswer } from "./clarification-form";
import { ConversationContextSummary } from "./conversation-context-summary";
import { ResultSummary } from "./result-summary";

type Availability =
  | "checking"
  | "ready_trip_only"
  | "ready_trip_and_facility"
  | "not_ready"
  | "fetch_error";

type TranscriptEvent =
  | { id: number; role: "user"; label: string; text: string }
  | { id: number; role: "assistant"; run: ConversationRunResponse };

interface UiError {
  title: string;
  message: string;
  requestId: string | null;
  code: string | null;
}

interface UnknownSubmission {
  phase: "start" | "resume";
  question: string;
  runId: string | null;
  clarificationId: string | null;
  idempotencyKey: string;
}

interface PreservedClarificationDraft {
  clarificationId: string;
  displayValue: string;
}

const availabilityCopy: Record<Availability, { title: string; detail: string; tone: string }> = {
  checking: {
    title: "서비스 상태 확인 중",
    detail: "이동 정보 서비스의 연결 상태를 확인하고 있습니다.",
    tone: "info",
  },
  ready_trip_only: {
    title: "이동 경로 조회 가능",
    detail: "출발역, 도착역과 이동 조건을 자연어로 입력해 주세요.",
    tone: "success",
  },
  ready_trip_and_facility: {
    title: "이동 경로와 시설 조회 가능",
    detail: "경로 접근성과 역 시설 상태를 자연어로 확인할 수 있습니다.",
    tone: "success",
  },
  not_ready: {
    title: "현재 이동 정보를 확인할 수 없음",
    detail: "이동 정보 서비스가 준비되지 않았습니다. 잠시 후 상태를 다시 확인해 주세요.",
    tone: "danger",
  },
  fetch_error: {
    title: "서비스 상태를 확인하지 못함",
    detail: "연결 상태를 확인할 수 없어 새 조회를 시작하지 않았습니다.",
    tone: "danger",
  },
};

// 질문 전·추가 질문 중에는 읽기 폭을 좁히고, 결과에서는 본문과 근거를 나란히 두도록 넓힌다.
const WORKSPACE_WIDTH = {
  empty: "max-w-[55rem]",
  clarifying: "max-w-[60rem]",
  result: "max-w-none",
} as const;

const PAGE_TITLE = {
  empty: "이동 가능한 경로를 확인해 보세요",
  clarifying: "이동 조회",
  result: "이동 조회 결과",
} as const;

// 글자 수는 제한에 가까워졌을 때만 보여 준다. 그 전에는 maxLength 가 넘치지 않게 막는다.
const COUNT_VISIBLE_RATIO = 0.9;

const SCOPE_ITEMS = [
  "출발역과 도착역 사이의 이동 경로 접근성을 확인합니다.",
  "휠체어·유모차와 계단·에스컬레이터 이용 조건을 반영합니다.",
  "확인되지 않은 정보는 이동 가능하다고 추정하지 않습니다.",
] as const;

const QUESTION_MAX_CHARS = 2_000;
const RUN_RECONCILIATION_ATTEMPTS = 4;
const RUN_RECONCILIATION_INTERVAL_MS = 750;

async function reconcileKnownRun(
  runId: string,
  clarificationId: string,
): Promise<ConversationRunResponse | null> {
  for (let attempt = 0; attempt < RUN_RECONCILIATION_ATTEMPTS; attempt += 1) {
    if (attempt > 0) {
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, RUN_RECONCILIATION_INTERVAL_MS);
      });
    }
    try {
      const run = await getRun(runId);
      const movedForward =
        run.run_status !== "interrupted" ||
        run.clarification?.id !== clarificationId;
      if (movedForward) {
        return run;
      }
    } catch {
      // GET is safe to retry within this small, bounded reconciliation window.
    }
  }
  return null;
}

function availabilityFrom(status: PublicServiceStatus): Availability {
  if (status.status === "unknown") {
    return "fetch_error";
  }
  const trip = status.features.find((feature) => feature.id === "trip_accessibility");
  if (trip?.status !== "available") {
    return "not_ready";
  }
  const facility = status.features.find((feature) => feature.id === "station_facility");
  return facility?.status === "available"
    ? "ready_trip_and_facility"
    : "ready_trip_only";
}

function toUiError(error: unknown, fallbackTitle: string): UiError {
  if (!isAgentClientError(error)) {
    return {
      title: fallbackTitle,
      message: "예상하지 못한 오류가 발생했습니다. 새 이동 조회로 다시 시작해 주세요.",
      requestId: null,
      code: null,
    };
  }
  if (error.kind === "contract") {
    return {
      title: "응답 계약을 확인할 수 없습니다",
      message: "안전하게 표시할 수 없는 응답이어서 결과를 숨겼습니다.",
      requestId: error.requestId,
      code: error.code,
    };
  }
  if (error.kind === "transport") {
    return {
      title: fallbackTitle,
      message: "이동 정보 서비스와 연결하지 못했습니다. 자동으로 다시 전송하지 않았습니다.",
      requestId: error.requestId,
      code: error.code,
    };
  }
  return {
    title: fallbackTitle,
    message: error.message,
    requestId: error.requestId,
    code: error.code,
  };
}

function isMutationOutcomeUnknown(error: unknown): boolean {
  if (!isAgentClientError(error)) {
    return false;
  }
  return (
    error.kind === "transport" ||
    error.code === "AGENT_MUTATION_OUTCOME_UNKNOWN" ||
    error.code === "EXECUTION_IN_PROGRESS" ||
    error.code === "AGENT_UPSTREAM_TIMEOUT" ||
    error.code === "AGENT_UPSTREAM_UNAVAILABLE"
  );
}

export function AssistantWorkspace() {
  const [availability, setAvailability] = useState<Availability>("checking");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [events, setEvents] = useState<TranscriptEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiError | null>(null);
  const [unknownSubmission, setUnknownSubmission] = useState<UnknownSubmission | null>(null);
  const [clarificationValidationIssues, setClarificationValidationIssues] = useState<
    readonly AgentValidationIssue[]
  >([]);
  const [preservedClarificationDraft, setPreservedClarificationDraft] =
    useState<PreservedClarificationDraft | null>(null);
  const [statusMessage, setStatusMessage] = useState("서비스 상태를 확인하고 있습니다.");
  const eventSequence = useRef(0);
  const initialStatusRequestRef = useRef<Promise<PublicServiceStatus> | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);

  const assistantEvents = useMemo(
    () => events.filter((event): event is Extract<TranscriptEvent, { role: "assistant" }> => event.role === "assistant"),
    [events],
  );
  const latestRun = assistantEvents.at(-1)?.run ?? null;
  const latestAssistantEventId = assistantEvents.at(-1)?.id ?? null;
  const activeClarification =
    latestRun?.run_status === "interrupted" ? latestRun.clarification : null;
  // 서버가 되묻는 문장을 응답에도 담으면 아래 추가 질문 상자가 같은 문장을 보여 준다.
  // 그 줄은 응답 말풍선에서 빼고, 이유처럼 상자에 없는 줄만 남긴다. 남는 줄이 없으면 말풍선을 생략한다.
  const clarificationReply =
    activeClarification !== null && latestRun !== null
      ? latestRun.user_message
          .split("\n")
          .filter(
            (line) =>
              !activeClarification.prompts.some((prompt) => prompt.question.trim() === line.trim()),
          )
          .join("\n")
          .trim()
      : "";
  const serviceReady =
    availability === "ready_trip_only" || availability === "ready_trip_and_facility";
  const facilityReady = availability === "ready_trip_and_facility";

  async function refreshPublicStatus() {
    setAvailability("checking");
    setError(null);
    setStatusMessage("서비스 상태를 확인하고 있습니다.");
    try {
      const response = await getPublicStatus();
      const next = availabilityFrom(response);
      setAvailability(next);
      setStatusMessage(availabilityCopy[next].title);
    } catch {
      setAvailability("fetch_error");
      setStatusMessage("서비스 상태를 확인하지 못했습니다.");
    }
  }

  useEffect(() => {
    let active = true;
    const statusRequest = initialStatusRequestRef.current ?? getPublicStatus();
    initialStatusRequestRef.current = statusRequest;
    void (async () => {
      try {
        const response = await statusRequest;
        if (!active) return;
        const next = availabilityFrom(response);
        setAvailability(next);
        setStatusMessage(availabilityCopy[next].title);
      } catch {
        if (!active) return;
        setAvailability("fetch_error");
        setStatusMessage("서비스 상태를 확인하지 못했습니다.");
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  function nextEventId(): number {
    eventSequence.current += 1;
    return eventSequence.current;
  }

  function appendCompletedTurn(
    label: string,
    userText: string,
    run: ConversationRunResponse,
  ) {
    setEvents((current) => [
      ...current,
      { id: nextEventId(), role: "user", label, text: userText },
      { id: nextEventId(), role: "assistant", run },
    ]);
  }

  function replaceLatestAssistantRun(run: ConversationRunResponse) {
    setEvents((current) => {
      let index = -1;
      for (let candidate = current.length - 1; candidate >= 0; candidate -= 1) {
        if (current[candidate]?.role === "assistant") {
          index = candidate;
          break;
        }
      }
      if (index < 0) return current;
      return current.map((event, eventIndex) =>
        eventIndex === index && event.role === "assistant" ? { ...event, run } : event,
      );
    });
  }

  async function handleQuestionSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = draft.trim();
    if (busy || activeClarification !== null || unknownSubmission !== null) {
      return;
    }
    if (!serviceReady) {
      setError({
        title: "새 조회를 시작할 수 없습니다",
        message: "서비스 연결 상태가 준비된 뒤 질문을 보내 주세요.",
        requestId: null,
        code: null,
      });
      return;
    }
    if (question === "") {
      setError({
        title: "질문을 입력해 주세요",
        message: "출발역, 도착역과 필요한 이동 조건을 자연어로 입력해 주세요.",
        requestId: null,
        code: null,
      });
      composerRef.current?.focus();
      return;
    }
    if (question.length > QUESTION_MAX_CHARS) {
      setError({
        title: "질문이 너무 깁니다",
        message: `질문은 ${QUESTION_MAX_CHARS.toLocaleString("ko-KR")}자 이내로 입력해 주세요.`,
        requestId: null,
        code: null,
      });
      composerRef.current?.focus();
      return;
    }

    setBusy(true);
    setError(null);
    setClarificationValidationIssues([]);
    setPreservedClarificationDraft(null);
    setStatusMessage("이동 정보를 확인하고 있습니다.");
    let currentThreadId = threadId;
    let runWasSubmitted = false;
    const idempotencyKey = createIdempotencyKey();
    try {
      if (currentThreadId === null) {
        const created = await createThread();
        currentThreadId = created.thread_id;
        setThreadId(created.thread_id);
      }
      runWasSubmitted = true;
      const run = await startRun(currentThreadId, { question }, idempotencyKey);
      appendCompletedTurn("질문", question, run);
      setDraft("");
      setStatusMessage(
        run.run_status === "interrupted"
          ? "추가 정보가 필요합니다."
          : "이동 정보 확인이 끝났습니다.",
      );
    } catch (submissionError) {
      if (runWasSubmitted && isMutationOutcomeUnknown(submissionError)) {
        setUnknownSubmission({
          phase: "start",
          question,
          runId: null,
          clarificationId: null,
          idempotencyKey,
        });
        setStatusMessage("질문 처리 결과를 확인할 수 없습니다.");
      }
      setError(toUiError(submissionError, runWasSubmitted ? "질문 처리 결과 확인 불가" : "대화 시작 실패"));
    } finally {
      setBusy(false);
    }
  }

  async function handleClarification(answer: ClarificationAnswer) {
    if (busy || latestRun === null || activeClarification === null) {
      return;
    }
    setBusy(true);
    setError(null);
    setClarificationValidationIssues([]);
    setStatusMessage("추가 정보를 반영하고 있습니다.");
    const idempotencyKey = createIdempotencyKey();
    try {
      const run = await resumeRun(latestRun.run_id, {
        schema_version: "2",
        clarification_id: activeClarification.id,
        answer: answer.answer,
      }, idempotencyKey);
      appendCompletedTurn("추가 정보", answer.displayValue, run);
      setPreservedClarificationDraft(null);
      setStatusMessage(
        run.run_status === "interrupted"
          ? "추가 정보가 더 필요합니다."
          : "이동 정보 확인이 끝났습니다.",
      );
    } catch (resumeError) {
      if (
        isAgentClientError(resumeError) &&
        resumeError.code === "REQUEST_VALIDATION_ERROR" &&
        resumeError.validationIssues.length > 0
      ) {
        setClarificationValidationIssues(resumeError.validationIssues);
        setStatusMessage("입력 내용을 다시 확인해 주세요.");
        return;
      }
      if (isAgentClientError(resumeError) && resumeError.code === "STALE_CLARIFICATION") {
        setPreservedClarificationDraft({
          clarificationId: activeClarification.id,
          displayValue: answer.displayValue,
        });
        setStatusMessage("최신 추가 질문을 확인하고 있습니다.");
        try {
          const reconciled = await getRun(latestRun.run_id);
          replaceLatestAssistantRun(reconciled);
          setError({
            title: "추가 질문이 변경되었습니다",
            message: "최신 질문을 불러왔습니다. 이전 입력은 자동으로 다시 보내지 않고 보존했습니다.",
            requestId: resumeError.requestId,
            code: resumeError.code,
          });
          setStatusMessage("최신 추가 질문을 불러왔습니다.");
          return;
        } catch {
          // Keep the rejected draft and surface the original stale response.
        }
      }
      if (isMutationOutcomeUnknown(resumeError)) {
        setStatusMessage("전송 결과를 제한된 횟수로 확인하고 있습니다.");
        const reconciled = await reconcileKnownRun(
          latestRun.run_id,
          activeClarification.id,
        );
        if (reconciled !== null) {
          appendCompletedTurn("추가 정보", answer.displayValue, reconciled);
          setPreservedClarificationDraft(null);
          setStatusMessage("현재 조회 상태를 다시 확인했습니다.");
          return;
        }
        setUnknownSubmission({
          phase: "resume",
          question: answer.displayValue,
          runId: latestRun.run_id,
          clarificationId: activeClarification.id,
          idempotencyKey,
        });
        setStatusMessage("추가 정보 처리 결과를 확인할 수 없습니다.");
      }
      setError(toUiError(resumeError, "추가 정보 처리 결과 확인 불가"));
    } finally {
      setBusy(false);
    }
  }

  async function handleUnknownSubmissionCheck() {
    const pending = unknownSubmission;
    if (busy || pending === null) {
      return;
    }
    setBusy(true);
    setStatusMessage("접수한 요청의 현재 결과를 확인하고 있습니다.");
    try {
      let reconciled: ConversationRunResponse | null = null;
      if (pending.phase === "start") {
        if (threadId === null) {
          throw new Error("The pending start submission lost its thread reference.");
        }
        // This is an explicit user action and reuses the original key. The BFF still
        // performs exactly one POST and the Agent joins the existing reservation.
        reconciled = await startRun(
          threadId,
          { question: pending.question },
          pending.idempotencyKey,
        );
      } else if (pending.runId !== null && pending.clarificationId !== null) {
        reconciled = await reconcileKnownRun(
          pending.runId,
          pending.clarificationId,
        );
      }

      if (reconciled === null) {
        setError({
          title: "아직 처리 결과를 확인할 수 없습니다",
          message: "요청을 다시 보내지 않았습니다. 잠시 후 접수한 요청 결과를 다시 확인해 주세요.",
          requestId: error?.requestId ?? null,
          code: "EXECUTION_IN_PROGRESS",
        });
        setStatusMessage("접수한 요청은 아직 처리 중입니다.");
        return;
      }

      appendCompletedTurn(
        pending.phase === "start" ? "질문" : "추가 정보",
        pending.question,
        reconciled,
      );
      setUnknownSubmission(null);
      setPreservedClarificationDraft(null);
      setError(null);
      if (pending.phase === "start") {
        setDraft("");
      }
      setStatusMessage(
        reconciled.run_status === "interrupted"
          ? "추가 정보가 필요합니다."
          : "접수한 요청의 결과를 확인했습니다.",
      );
    } catch (checkError) {
      setError(toUiError(checkError, "접수한 요청 결과 확인 불가"));
      setStatusMessage("접수한 요청 결과를 아직 확인할 수 없습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteConversation() {
    if (busy) return;
    if (threadId === null) {
      resetLocalConversation();
      setStatusMessage("조건을 초기화했습니다. 새 이동 조회를 시작할 수 있습니다.");
      return;
    }
    setBusy(true);
    setError(null);
    setClarificationValidationIssues([]);
    setStatusMessage("대화를 삭제하고 있습니다.");
    try {
      await deleteThread(threadId);
      resetLocalConversation();
      setStatusMessage("대화를 삭제했습니다. 새 이동 조회를 시작할 수 있습니다.");
    } catch (deleteError) {
      setError(toUiError(deleteError, "대화 삭제 결과 확인 불가"));
      setStatusMessage("대화를 삭제했는지 확인할 수 없습니다.");
    } finally {
      setBusy(false);
    }
  }

  function resetLocalConversation() {
    setThreadId(null);
    setEvents([]);
    setDraft("");
    setError(null);
    setUnknownSubmission(null);
    setClarificationValidationIssues([]);
    setPreservedClarificationDraft(null);
    eventSequence.current = 0;
    window.setTimeout(() => pageHeadingRef.current?.focus(), 0);
  }

  function setExample(question: string) {
    if (busy || activeClarification !== null || unknownSubmission !== null) return;
    setDraft(question);
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  const workspaceState = activeClarification
    ? "clarifying"
    : latestRun
      ? "result"
      : "empty";
  const status = availabilityCopy[availability];
  const showAvailabilityNotice =
    availability === "checking" ||
    availability === "not_ready" ||
    availability === "fetch_error";
  const canStartNewLookup =
    threadId !== null || events.length > 0 || unknownSubmission !== null;
  const pageTitle = PAGE_TITLE[workspaceState];
  const latestQuestion =
    events
      .filter(
        (event): event is Extract<TranscriptEvent, { role: "user" }> =>
          event.role === "user" && event.label === "질문",
      )
      .at(-1)?.text ?? null;
  const showCount = draft.length >= QUESTION_MAX_CHARS * COUNT_VISIBLE_RATIO;

  function renderTranscript(collapsed: boolean) {
    const transcriptItems = events.filter(
      (transcriptEvent) =>
        transcriptEvent.role !== "assistant" ||
        transcriptEvent.id !== latestAssistantEventId,
    );
    if (transcriptItems.length === 0) {
      return null;
    }

    const transcript = (
      <ol className="flex flex-col gap-4" aria-label="대화 기록">
        {transcriptItems.map((transcriptEvent) => {
          if (transcriptEvent.role === "user") {
            return (
              <li key={transcriptEvent.id}>
                <Message align="end">
                  <MessageContent>
                    <MessageHeader className="justify-end">{transcriptEvent.label}</MessageHeader>
                    <Bubble variant="secondary" align="end">
                      <BubbleContent className="px-4 py-2.5 text-base whitespace-pre-wrap">
                        {transcriptEvent.text}
                      </BubbleContent>
                    </Bubble>
                  </MessageContent>
                </Message>
              </li>
            );
          }
          return (
            <li key={transcriptEvent.id}>
              <AgentMessage label="조회 응답" message={transcriptEvent.run.user_message} />
            </li>
          );
        })}
      </ol>
    );

    if (collapsed) {
      if (transcriptItems.length === 1 && transcriptItems[0]?.role === "user") {
        return null;
      }
      return (
        <Disclosure summary="이전 질문과 대화" contentClassName="py-4">
          {transcript}
        </Disclosure>
      );
    }

    return transcript;
  }

  function renderComposer(mode: "initial" | "followup") {
    const isFollowup = mode === "followup";
    const blocked = busy || !serviceReady || unknownSubmission !== null;
    return (
      <form
        className="flex flex-col gap-3"
        onSubmit={handleQuestionSubmit}
        aria-busy={busy}
        noValidate
      >
        <Field>
          <FieldLabel htmlFor="assistant-question" className="text-base">
            {isFollowup ? "후속 질문" : "이동 질문"}
          </FieldLabel>
          <Textarea
            ref={composerRef}
            id="assistant-question"
            className={cn(
              "text-base read-only:bg-muted/50 md:text-base",
              isFollowup ? "min-h-20" : "min-h-32",
            )}
            value={draft}
            rows={isFollowup ? 3 : 5}
            maxLength={QUESTION_MAX_CHARS}
            placeholder={
              isFollowup
                ? "예: 계단을 이용할 수 없다면 다른 경로가 있나요?"
                : "예: 휠체어를 이용해 시청역에서 강남역까지 이동할 수 있을까요?"
            }
            readOnly={busy || unknownSubmission !== null}
            aria-describedby={showCount ? "composer-help composer-count" : "composer-help"}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleComposerKeyDown}
          />
        </Field>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-0.5 text-sm text-muted-foreground">
            {showCount ? (
              <p id="composer-count">
                {draft.length.toLocaleString("ko-KR")} /{" "}
                {QUESTION_MAX_CHARS.toLocaleString("ko-KR")}자
              </p>
            ) : null}
            <p id="composer-help" className="pointer-coarse:hidden">
              Enter는 줄바꿈, Ctrl 또는 Command + Enter는 {isFollowup ? "이어서 질문" : "조회 시작"}
            </p>
          </div>
          <Button
            type="submit"
            size="lg"
            className="w-full sm:w-auto"
            aria-disabled={blocked}
          >
            {busy ? (
              <Spinner data-icon="inline-start" aria-hidden="true" />
            ) : (
              <SendIcon data-icon="inline-start" aria-hidden="true" />
            )}
            {busy ? "확인 중…" : isFollowup ? "이어서 질문" : "조회 시작"}
          </Button>
        </div>
      </form>
    );
  }

  const AvailabilityIcon =
    status.tone === "danger" ? CircleAlertIcon : status.tone === "info" ? InfoIcon : CircleCheckIcon;

  return (
    <div
      className={cn("mx-auto flex w-full flex-col gap-6", WORKSPACE_WIDTH[workspaceState])}
      data-state={workspaceState}
      data-ui="assistant-workspace"
    >
      <div className="sr-only" role="status" aria-live="polite">
        {statusMessage}
      </div>

      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-col gap-2">
          <h1
            id="conversation-title"
            ref={pageHeadingRef}
            tabIndex={-1}
            className={cn(
              "font-semibold tracking-tight",
              workspaceState === "empty" ? "text-2xl sm:text-3xl" : "text-xl sm:text-2xl",
            )}
          >
            {pageTitle}
          </h1>
          {workspaceState === "result" && latestQuestion ? (
            <p className="text-muted-foreground">
              <span className="sr-only">질문: </span>“{latestQuestion}”
            </p>
          ) : null}
          {workspaceState === "empty" ? (
            <p className="text-muted-foreground">
              출발지, 목적지와 이동 조건을 자연어로 알려주세요. 확인에 필요한 조건이 빠지면 이어서
              질문합니다.
            </p>
          ) : null}
        </div>
        {canStartNewLookup ? (
          <Button
            variant="outline"
            className="self-start sm:self-auto"
            onClick={() => void handleDeleteConversation()}
            aria-disabled={busy}
          >
            <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
            새 이동 조회
          </Button>
        ) : null}
      </header>

      {showAvailabilityNotice ? (
        // 상태 안내는 위쪽 status 영역이 읽어 주므로 alert 로 다시 알리지 않는다.
        <Alert
          role={undefined}
          variant={status.tone === "danger" ? "destructive" : "default"}
          aria-labelledby="service-status-title"
        >
          <AvailabilityIcon aria-hidden="true" />
          <AlertTitle>
            <h2 id="service-status-title" className="font-semibold">
              {status.title}
            </h2>
          </AlertTitle>
          <AlertDescription>{status.detail}</AlertDescription>
          {availability === "not_ready" || availability === "fetch_error" ? (
            <div className="col-start-2 mt-3">
              <Button variant="outline" onClick={() => void refreshPublicStatus()}>
                <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
                상태 다시 확인
              </Button>
            </div>
          ) : null}
        </Alert>
      ) : null}

      {/* 결과 화면에서는 결과 요약이 이동 조건을 근거와 함께 오른쪽 칸에 보여 준다. */}
      {latestRun && workspaceState !== "result" ? (
        <ConversationContextSummary context={latestRun.context} />
      ) : null}

      {error ? (
        <Alert variant="destructive" role="alert" aria-labelledby="error-title">
          <CircleAlertIcon aria-hidden="true" />
          <AlertTitle>
            <h2 id="error-title" className="font-semibold">
              {error.title}
            </h2>
          </AlertTitle>
          <AlertDescription className="flex flex-col gap-1">
            <p>{error.message}</p>
            {unknownSubmission ? (
              <p>
                전송한 {unknownSubmission.phase === "start" ? "질문" : "추가 정보"}: “
                {unknownSubmission.question}”
              </p>
            ) : null}
          </AlertDescription>
          {unknownSubmission || error.requestId || error.code ? (
            <div className="col-start-2 mt-3 flex flex-col items-start gap-3 text-foreground">
              {unknownSubmission ? (
                <Button
                  variant="outline"
                  aria-disabled={busy}
                  onClick={() => void handleUnknownSubmissionCheck()}
                >
                  접수한 요청 결과 확인
                </Button>
              ) : null}
              {error.requestId || error.code ? (
                <Disclosure summary="문제 신고용 정보" className="w-full">
                  {error.requestId ? (
                    <p>
                      요청 ID: <code className="font-mono text-xs break-all">{error.requestId}</code>
                    </p>
                  ) : null}
                  {error.code ? (
                    <p>
                      오류 코드: <code className="font-mono text-xs break-all">{error.code}</code>
                    </p>
                  ) : null}
                </Disclosure>
              ) : null}
            </div>
          ) : null}
        </Alert>
      ) : null}

      {workspaceState === "empty" ? (
        <section className="flex flex-col gap-8" aria-label="이동 질문 시작">
          <section aria-labelledby="question-surface-title">
            <Card className="gap-5">
              <CardHeader>
                <CardTitle>
                  <h2 id="question-surface-title" className="text-lg font-semibold">
                    어떤 이동을 확인할까요?
                  </h2>
                </CardTitle>
              </CardHeader>
              <CardContent>{renderComposer("initial")}</CardContent>
            </Card>
          </section>

          <section className="flex flex-col gap-3" aria-labelledby="example-title">
            <h2 id="example-title" className="text-lg font-semibold">
              이렇게 물어볼 수 있어요
            </h2>
            <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label="예시 질문">
              <ExampleButton
                onClick={() =>
                  setExample("휠체어를 이용해 시청역에서 강남역까지 이동할 수 있을까요?")
                }
              >
                휠체어로 시청역에서 강남역까지 이동할 수 있을까요?
              </ExampleButton>
              <ExampleButton
                onClick={() =>
                  setExample("계단을 이용할 수 없는데 서울역에서 잠실역까지 갈 수 있을까요?")
                }
              >
                계단을 이용할 수 없을 때의 경로를 확인하고 싶어요
              </ExampleButton>
              {facilityReady ? (
                <ExampleButton
                  aria-label="역 시설 상태 확인"
                  onClick={() => setExample("서울역 엘리베이터가 지금 이용 가능한가요?")}
                >
                  서울역 엘리베이터 상태를 확인하고 싶어요
                </ExampleButton>
              ) : null}
            </div>
          </section>

          <section aria-labelledby="scope-title">
            <Card size="sm">
              <CardHeader>
                <CardTitle>
                  <h2 id="scope-title" className="text-base font-semibold">
                    확인할 수 있는 범위와 정보의 한계
                  </h2>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-col gap-2 text-sm leading-6">
                  {SCOPE_ITEMS.map((item) => (
                    <li key={item} className="flex gap-2">
                      <CheckIcon aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted-foreground" />
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          </section>
        </section>
      ) : null}

      {workspaceState === "clarifying" && latestRun ? (
        <section className="flex flex-col gap-6" aria-label="추가 정보 확인">
          {renderTranscript(false)}
          {clarificationReply === "" ? null : (
            <AgentMessage label="조회 응답" message={clarificationReply} />
          )}
          {preservedClarificationDraft ? (
            <Disclosure summary="자동 재전송하지 않은 이전 입력">
              <p>{preservedClarificationDraft.displayValue}</p>
              <small className="text-xs text-muted-foreground">
                이전 추가 질문 ID:{" "}
                <code className="font-mono break-all">{preservedClarificationDraft.clarificationId}</code>
              </small>
            </Disclosure>
          ) : null}
          {activeClarification && unknownSubmission === null ? (
            <ClarificationForm
              key={activeClarification.id}
              clarification={activeClarification}
              busy={busy}
              submitIssues={clarificationValidationIssues}
              onEdit={() => setClarificationValidationIssues([])}
              onSubmit={handleClarification}
            />
          ) : null}
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <LockIcon aria-hidden="true" className="size-4 shrink-0" />
            추가 질문을 확인하는 동안 새 질문 입력은 잠겨 있습니다.
          </p>
        </section>
      ) : null}

      {workspaceState === "result" && latestRun ? (
        <div className="flex flex-col gap-6">
          <ResultSummary run={latestRun} />
          <Card>
            <CardContent>{renderComposer("followup")}</CardContent>
          </Card>
          {renderTranscript(true)}
        </div>
      ) : null}
    </div>
  );
}

function AgentMessage({ label, message }: { label: string; message: string }) {
  return (
    <Message>
      <MessageContent>
        <MessageHeader>{label}</MessageHeader>
        <Bubble variant="muted" className="max-w-full sm:max-w-[90%]">
          <BubbleContent className="px-4 py-3">
            <CanonicalMarkdown compact message={message} />
          </BubbleContent>
        </Bubble>
      </MessageContent>
    </Message>
  );
}

function ExampleButton({ className, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button
      variant="outline"
      className={cn(
        "h-auto min-h-11 justify-start py-3 text-left text-sm leading-6 whitespace-normal",
        className,
      )}
      {...props}
    />
  );
}
