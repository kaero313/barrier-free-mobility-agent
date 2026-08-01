import {
  AgentErrorResponseSchema,
  AgentIdSchema,
  ConversationRunResponseSchema,
  CreateThreadResponseSchema,
  ReadinessResponseSchema,
  ResumeRunRequestSchema,
  StartRunRequestSchema,
} from "../../../contracts/agent";
import {
  createBffErrorResponse,
  proxyAgentRequest,
} from "../../../server/agent-api/client";

export function createThread(request: Request): Promise<Response> {
  return proxyAgentRequest({
    request,
    method: "POST",
    upstreamPath: "/v1/threads",
    successStatuses: [201],
    successSchema: CreateThreadResponseSchema,
    errorSchema: AgentErrorResponseSchema,
  });
}

export function deleteThread(request: Request, threadId: string): Promise<Response> {
  const invalid = rejectInvalidId(request, threadId, "thread");
  if (invalid !== null) {
    return Promise.resolve(invalid);
  }
  return proxyAgentRequest({
    request,
    method: "DELETE",
    upstreamPath: `/v1/threads/${encodeURIComponent(threadId)}`,
    successStatuses: [204],
    allowEmptySuccess: true,
    errorSchema: AgentErrorResponseSchema,
  });
}

export function startRun(
  request: Request,
  threadId: string,
): Promise<Response> {
  const invalid = rejectInvalidId(request, threadId, "thread");
  if (invalid !== null) {
    return Promise.resolve(invalid);
  }
  return proxyAgentRequest({
    request,
    method: "POST",
    upstreamPath: `/v1/threads/${encodeURIComponent(threadId)}/runs`,
    successStatuses: [200],
    requestSchema: StartRunRequestSchema,
    successSchema: ConversationRunResponseSchema,
    errorSchema: AgentErrorResponseSchema,
    responseSchemaVersion: "4",
    idempotentMutation: true,
  });
}

export function getRun(request: Request, runId: string): Promise<Response> {
  const invalid = rejectInvalidId(request, runId, "run");
  if (invalid !== null) {
    return Promise.resolve(invalid);
  }
  return proxyAgentRequest({
    request,
    method: "GET",
    upstreamPath: `/v1/runs/${encodeURIComponent(runId)}`,
    successStatuses: [200],
    successSchema: ConversationRunResponseSchema,
    errorSchema: AgentErrorResponseSchema,
    responseSchemaVersion: "4",
  });
}

export function resumeRun(request: Request, runId: string): Promise<Response> {
  const invalid = rejectInvalidId(request, runId, "run");
  if (invalid !== null) {
    return Promise.resolve(invalid);
  }
  return proxyAgentRequest({
    request,
    method: "POST",
    upstreamPath: `/v1/runs/${encodeURIComponent(runId)}/resume`,
    successStatuses: [200],
    requestSchema: ResumeRunRequestSchema,
    successSchema: ConversationRunResponseSchema,
    errorSchema: AgentErrorResponseSchema,
    responseSchemaVersion: "4",
    idempotentMutation: true,
  });
}

export function getReadiness(request: Request): Promise<Response> {
  return proxyAgentRequest({
    request,
    method: "GET",
    upstreamPath: "/health/ready",
    successStatuses: [200, 503],
    successSchema: ReadinessResponseSchema,
    errorSchema: AgentErrorResponseSchema,
  });
}

function rejectInvalidId(
  request: Request,
  value: string,
  resource: "thread" | "run",
): Response | null {
  if (AgentIdSchema.safeParse(value).success) {
    return null;
  }
  return createBffErrorResponse(
    request,
    422,
    "BFF_PATH_VALIDATION_ERROR",
    `${resource} 식별자 형식이 올바르지 않습니다.`,
    false,
  );
}
