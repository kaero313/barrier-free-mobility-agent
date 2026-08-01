import { z } from "zod";

import {
  AgentErrorResponseSchema,
  ConversationRunResponseSchema,
  CreateThreadResponseSchema,
  type AgentErrorResponse,
  type AgentValidationIssue,
  type ConversationRunResponse,
  type CreateThreadResponse,
  type StructuredResumeRunRequest,
  type StartRunRequest,
} from "@/contracts/agent";
import {
  PublicServiceStatusSchema,
  type PublicServiceStatus,
} from "@/contracts/public-status";

// Keep the browser deadline above the default 90 s BFF deadline so the BFF can
// return a typed timeout/outcome-unknown response instead of being cut off first.
const REQUEST_TIMEOUT_MS = 100_000;
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";

type ErrorKind = "agent" | "contract" | "transport";

export class AgentClientError extends Error {
  readonly kind: ErrorKind;
  readonly status: number | null;
  readonly requestId: string | null;
  readonly code: string | null;
  readonly retryable: boolean;
  readonly validationIssues: readonly AgentValidationIssue[];

  constructor(
    message: string,
    options: {
      kind: ErrorKind;
      status?: number | null;
      requestId?: string | null;
      code?: string | null;
      retryable?: boolean;
      validationIssues?: readonly AgentValidationIssue[];
      cause?: unknown;
    },
  ) {
    super(message, { cause: options.cause });
    this.name = "AgentClientError";
    this.kind = options.kind;
    this.status = options.status ?? null;
    this.requestId = options.requestId ?? null;
    this.code = options.code ?? null;
    this.retryable = options.retryable ?? false;
    this.validationIssues = options.validationIssues ?? [];
  }
}

function requestInit(init?: RequestInit): RequestInit {
  return {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init?.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
    signal: init?.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch (error) {
    throw new AgentClientError("이동 정보 서비스의 응답을 읽을 수 없습니다.", {
      kind: "contract",
      status: response.status,
      requestId: response.headers.get("x-request-id"),
      cause: error,
    });
  }
}

function contractError(response: Response, error: z.ZodError): AgentClientError {
  return new AgentClientError("이동 정보 서비스의 응답 형식을 안전하게 확인할 수 없습니다.", {
    kind: "contract",
    status: response.status,
    requestId: response.headers.get("x-request-id"),
    cause: error,
  });
}

function upstreamError(response: Response, body: unknown): AgentClientError {
  const parsed = AgentErrorResponseSchema.safeParse(body);
  if (!parsed.success) {
    return contractError(response, parsed.error);
  }
  const payload: AgentErrorResponse = parsed.data;
  const validationIssues =
    payload.error.code === "REQUEST_VALIDATION_ERROR" && "issues" in payload.error.detail
      ? payload.error.detail.issues
      : [];
  return new AgentClientError(payload.error.message, {
    kind: "agent",
    status: response.status,
    requestId: payload.request_id,
    code: payload.error.code,
    retryable: payload.error.retryable,
    validationIssues,
  });
}

async function fetchResponse(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, requestInit(init));
  } catch (error) {
    throw new AgentClientError("이동 정보 서비스와 통신하지 못했습니다.", {
      kind: "transport",
      cause: error,
    });
  }
}

async function requestJson<T>(
  input: string,
  schema: z.ZodType<T>,
  init?: RequestInit,
): Promise<T> {
  const response = await fetchResponse(input, init);
  const body = await readJson(response);
  if (!response.ok) {
    throw upstreamError(response, body);
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw contractError(response, parsed.error);
  }
  return parsed.data;
}

export function getPublicStatus(): Promise<PublicServiceStatus> {
  return requestJson(
    "/api/agent/public-status",
    PublicServiceStatusSchema,
  );
}

export function createThread(): Promise<CreateThreadResponse> {
  return requestJson("/api/agent/threads", CreateThreadResponseSchema, {
    method: "POST",
  });
}

export async function deleteThread(threadId: string): Promise<void> {
  const response = await fetchResponse(`/api/agent/threads/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
  });
  if (response.status === 204) {
    return;
  }
  const body = await readJson(response);
  throw upstreamError(response, body);
}

export function startRun(
  threadId: string,
  payload: StartRunRequest,
  idempotencyKey: string,
): Promise<ConversationRunResponse> {
  return requestJson(
    `/api/agent/threads/${encodeURIComponent(threadId)}/runs`,
    ConversationRunResponseSchema,
    {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { [IDEMPOTENCY_KEY_HEADER]: idempotencyKey },
    },
  );
}

export function getRun(runId: string): Promise<ConversationRunResponse> {
  return requestJson(
    `/api/agent/runs/${encodeURIComponent(runId)}`,
    ConversationRunResponseSchema,
  );
}

export function resumeRun(
  runId: string,
  payload: StructuredResumeRunRequest,
  idempotencyKey: string,
): Promise<ConversationRunResponse> {
  return requestJson(
    `/api/agent/runs/${encodeURIComponent(runId)}/resume`,
    ConversationRunResponseSchema,
    {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { [IDEMPOTENCY_KEY_HEADER]: idempotencyKey },
    },
  );
}

export function createIdempotencyKey(): string {
  return crypto.randomUUID();
}

export function isAgentClientError(error: unknown): error is AgentClientError {
  return error instanceof AgentClientError;
}
