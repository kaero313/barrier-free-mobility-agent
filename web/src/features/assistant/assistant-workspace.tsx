"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { AstryxPanel } from "@/components/primitives/astryx-adapter";
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
import styles from "./assistant-workspace.module.css";

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
  const pageDescription =
    workspaceState === "clarifying"
      ? "확인한 조건을 바탕으로 필요한 정보만 이어서 질문합니다."
      : workspaceState === "result"
        ? "확인 결과와 정보 한계를 함께 살펴보고 이어서 질문할 수 있습니다."
        : "출발지, 목적지와 이동 조건을 자연어로 알려주세요.";

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
      <ol className={styles.transcript} aria-label="대화 기록">
        {transcriptItems.map((transcriptEvent) => {
          if (transcriptEvent.role === "user") {
            return (
              <li key={transcriptEvent.id} className={styles.userTurn}>
                <span>{transcriptEvent.label}</span>
                <p>{transcriptEvent.text}</p>
              </li>
            );
          }
          return (
            <li key={transcriptEvent.id} className={styles.agentTurn}>
              <span>조회 응답</span>
              <CanonicalMarkdown compact message={transcriptEvent.run.user_message} />
            </li>
          );
        })}
      </ol>
    );

    if (collapsed) {
      return (
        <details className={styles.historyDisclosure}>
          <summary>이전 질문과 대화</summary>
          {transcript}
        </details>
      );
    }

    return (
      <section className={styles.conversationHistory} aria-labelledby="conversation-history-title">
        <h2 id="conversation-history-title">지금까지 나눈 내용</h2>
        {transcript}
      </section>
    );
  }

  function renderComposer(mode: "initial" | "followup") {
    const isFollowup = mode === "followup";
    return (
      <form
        className={`${styles.composer} ${isFollowup ? styles.followupComposer : ""}`}
        onSubmit={handleQuestionSubmit}
        aria-busy={busy}
        noValidate
      >
        <label htmlFor="assistant-question">
          {isFollowup ? "후속 질문" : "이동 질문"}
        </label>
        <textarea
          ref={composerRef}
          id="assistant-question"
          value={draft}
          rows={isFollowup ? 3 : 5}
          maxLength={QUESTION_MAX_CHARS}
          placeholder={
            isFollowup
              ? "예: 계단을 이용할 수 없다면 다른 경로가 있나요?"
              : "예: 휠체어를 이용해 시청역에서 강남역까지 이동할 수 있을까요?"
          }
          readOnly={busy || unknownSubmission !== null}
          aria-describedby="composer-help composer-count"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleComposerKeyDown}
        />
        <div className={styles.composerFooter}>
          <div className={styles.composerMeta}>
            <p id="composer-count">
              {draft.length.toLocaleString("ko-KR")} /{" "}
              {QUESTION_MAX_CHARS.toLocaleString("ko-KR")}자
            </p>
            <p id="composer-help">
              Enter는 줄바꿈, Ctrl 또는 Command + Enter는 {isFollowup ? "이어서 질문" : "조회 시작"}
            </p>
          </div>
          <button
            type="submit"
            className={styles.primaryButton}
            aria-disabled={busy || !serviceReady || unknownSubmission !== null}
          >
            {busy ? "확인 중…" : isFollowup ? "이어서 질문" : "조회 시작"}
          </button>
        </div>
      </form>
    );
  }

  return (
    <div
      className={`${styles.workspace} ${styles[`workspace_${workspaceState}`]}`}
      data-state={workspaceState}
      data-ui="assistant-workspace"
    >
      <div className={styles.statusAnnouncer} role="status" aria-live="polite">
        {statusMessage}
      </div>

      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>이동 접근성 조회</p>
          <h1 id="conversation-title" ref={pageHeadingRef} tabIndex={-1}>
            이동 가능한 경로를 확인해 보세요
          </h1>
          <p>{pageDescription}</p>
        </div>
        {canStartNewLookup ? (
          <div className={styles.pageActions}>
            {workspaceState === "result" ? (
              <a
                className={styles.resultJumpLink}
                href="#result-title"
                onClick={() => {
                  window.setTimeout(() => document.getElementById("result-title")?.focus(), 0);
                }}
              >
                확인 결과로 이동
              </a>
            ) : null}
            <button
              type="button"
              className={styles.secondaryButton}
              onClick={() => void handleDeleteConversation()}
              aria-disabled={busy}
            >
              새 이동 조회
            </button>
          </div>
        ) : null}
      </header>

      {showAvailabilityNotice ? (
        <section
          className={`${styles.availabilityNotice} ${styles[`notice_${status.tone}`]}`}
          aria-labelledby="service-status-title"
        >
          <div>
            <h2 id="service-status-title">{status.title}</h2>
            <p>{status.detail}</p>
          </div>
          {availability === "not_ready" || availability === "fetch_error" ? (
            <button
              type="button"
              className={styles.secondaryButton}
              onClick={() => void refreshPublicStatus()}
            >
              상태 다시 확인
            </button>
          ) : null}
        </section>
      ) : null}

      {latestRun ? <ConversationContextSummary context={latestRun.context} /> : null}

      {error ? (
        <section className={styles.errorCard} role="alert" aria-labelledby="error-title">
          <h2 id="error-title">{error.title}</h2>
          <p>{error.message}</p>
          {unknownSubmission ? (
            <>
              <p>
                전송한 {unknownSubmission.phase === "start" ? "질문" : "추가 정보"}: “
                {unknownSubmission.question}”
              </p>
              <button
                type="button"
                className={styles.secondaryButton}
                aria-disabled={busy}
                onClick={() => void handleUnknownSubmissionCheck()}
              >
                접수한 요청 결과 확인
              </button>
            </>
          ) : null}
          {error.requestId || error.code ? (
            <details>
              <summary>문제 신고용 정보</summary>
              {error.requestId ? (
                <p>
                  요청 ID: <code>{error.requestId}</code>
                </p>
              ) : null}
              {error.code ? (
                <p>
                  오류 코드: <code>{error.code}</code>
                </p>
              ) : null}
            </details>
          ) : null}
        </section>
      ) : null}

      {workspaceState === "empty" ? (
        <section className={styles.emptyExperience} aria-label="이동 질문 시작">
          <AstryxPanel className={styles.questionSurface} aria-labelledby="question-surface-title">
            <div className={styles.questionIntro}>
              <h2 id="question-surface-title">어떤 이동을 확인할까요?</h2>
              <p>조건이 더 필요하면 확인에 필요한 내용을 이어서 질문합니다.</p>
            </div>
            {renderComposer("initial")}
          </AstryxPanel>

          <section className={styles.exampleSection} aria-labelledby="example-title">
            <div>
              <p className={styles.eyebrow}>질문 예시</p>
              <h2 id="example-title">이렇게 물어볼 수 있어요</h2>
            </div>
            <div className={styles.exampleList} aria-label="예시 질문">
              <button
                type="button"
                onClick={() =>
                  setExample("휠체어를 이용해 시청역에서 강남역까지 이동할 수 있을까요?")
                }
              >
                휠체어로 시청역에서 강남역까지 이동할 수 있을까요?
              </button>
              <button
                type="button"
                onClick={() =>
                  setExample("계단을 이용할 수 없는데 서울역에서 잠실역까지 갈 수 있을까요?")
                }
              >
                계단을 이용할 수 없을 때의 경로를 확인하고 싶어요
              </button>
              {facilityReady ? (
                <button
                  type="button"
                  aria-label="역 시설 상태 확인"
                  onClick={() => setExample("서울역 엘리베이터가 지금 이용 가능한가요?")}
                >
                  서울역 엘리베이터 상태를 확인하고 싶어요
                </button>
              ) : null}
            </div>
          </section>

          <section className={styles.scopeSection} aria-labelledby="scope-title">
            <div>
              <p className={styles.eyebrow}>지원 범위</p>
              <h2 id="scope-title">확인할 수 있는 범위와 정보의 한계</h2>
            </div>
            <ul>
              <li>출발역과 도착역 사이의 이동 경로 접근성을 확인합니다.</li>
              <li>휠체어·유모차와 계단·에스컬레이터 이용 조건을 반영합니다.</li>
              <li>확인되지 않은 정보는 이동 가능하다고 추정하지 않습니다.</li>
            </ul>
          </section>
        </section>
      ) : null}

      {workspaceState === "clarifying" && latestRun ? (
        <section className={styles.clarificationFlow} aria-label="추가 정보 확인">
          {renderTranscript(false)}
          <div className={styles.clarificationMessage}>
            <CanonicalMarkdown compact message={latestRun.user_message} />
          </div>
          {preservedClarificationDraft ? (
            <details className={styles.preservedDraft}>
              <summary>자동 재전송하지 않은 이전 입력</summary>
              <p>{preservedClarificationDraft.displayValue}</p>
              <small>
                이전 추가 질문 ID: <code>{preservedClarificationDraft.clarificationId}</code>
              </small>
            </details>
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
          <p className={styles.lockedComposerNotice}>
            추가 질문을 확인하는 동안 새 질문 입력은 잠겨 있습니다.
          </p>
        </section>
      ) : null}

      {workspaceState === "result" && latestRun ? (
        <div className={styles.resultExperience}>
          <ResultSummary run={latestRun} />
          {renderComposer("followup")}
          {renderTranscript(true)}
        </div>
      ) : null}
    </div>
  );
}
