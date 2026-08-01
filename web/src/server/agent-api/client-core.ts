import "server-only";

import { randomUUID } from "node:crypto";

import type { ZodType } from "zod";

import {
  AgentErrorResponseSchema,
  RequestIdSchema,
  type AgentErrorResponse,
} from "../../contracts/agent";
import {
  appendAnonymousSessionCookie,
  loadAnonymousSessionConfig,
  resolveAnonymousSession,
} from "../security/anonymous-session";
import { readRequiredServerSecret } from "../security/server-secret";

const REQUEST_ID_HEADER = "x-request-id";
const OWNER_ID_HEADER = "X-Agent-Owner-ID";
const INTERNAL_TOKEN_HEADER = "X-Agent-Internal-Token";
const RESPONSE_SCHEMA_HEADER = "X-Agent-Response-Schema";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
const OWNER_ID_PATTERN = /^[a-f0-9]{64}$/;
const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/;
const DEFAULT_TIMEOUT_MS = 90_000;
const MIN_TIMEOUT_MS = 100;
const MAX_TIMEOUT_MS = 300_000;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_REQUEST_BYTES = 64 * 1024;

export interface AgentProxyRuntime {
  baseUrl: URL;
  timeoutMs: number;
  maxResponseBytes: number;
  fetchImpl: typeof fetch;
  internalToken: string;
}

interface AgentProxyEnvironment {
  [key: string]: string | undefined;
  AGENT_INTERNAL_URL?: string;
  AGENT_REQUEST_TIMEOUT_MS?: string;
  AGENT_BFF_SHARED_SECRET?: string;
  AGENT_ANONYMOUS_SESSION_SECRET?: string;
  AGENT_ALLOW_PRIVATE_HTTP?: string;
  NODE_ENV?: string;
  WEB_APP_ENV?: string;
  WEB_PUBLIC_ORIGIN?: string;
}

export interface AgentProxyRequest {
  request: Request;
  method: "GET" | "POST" | "DELETE";
  upstreamPath: string;
  successStatuses: readonly number[];
  successSchema?: ZodType;
  requestSchema?: ZodType;
  errorSchema?: ZodType;
  allowEmptySuccess?: boolean;
  responseSchemaVersion?: string;
  idempotentMutation?: boolean;
}

interface ValidatedBody {
  ok: true;
  raw: string;
}

interface BodyFailure {
  ok: false;
  response: Response;
}

export function loadAgentProxyRuntime(
  environment: AgentProxyEnvironment = process.env,
  fetchImpl: typeof fetch = fetch,
): AgentProxyRuntime {
  const rawUrl = environment.AGENT_INTERNAL_URL;
  if (rawUrl === undefined || rawUrl.trim() === "") {
    throw new Error("AGENT_INTERNAL_URL is required.");
  }

  let baseUrl: URL;
  try {
    baseUrl = new URL(rawUrl);
  } catch {
    throw new Error("AGENT_INTERNAL_URL must be an absolute URL.");
  }
  if (!['http:', 'https:'].includes(baseUrl.protocol)) {
    throw new Error("AGENT_INTERNAL_URL must use HTTP(S).");
  }
  if (baseUrl.username !== "" || baseUrl.password !== "") {
    throw new Error("AGENT_INTERNAL_URL must not contain credentials.");
  }
  if (baseUrl.pathname !== "/" || baseUrl.search !== "" || baseUrl.hash !== "") {
    throw new Error("AGENT_INTERNAL_URL must be an origin without a path, query, or fragment.");
  }
  const privateHttpAllowed = environment.AGENT_ALLOW_PRIVATE_HTTP === "true";
  if (
    environment.AGENT_ALLOW_PRIVATE_HTTP !== undefined
    && !["true", "false"].includes(environment.AGENT_ALLOW_PRIVATE_HTTP)
  ) {
    throw new Error("AGENT_ALLOW_PRIVATE_HTTP must be true or false.");
  }
  if (
    ["staging", "production"].includes(environment.WEB_APP_ENV ?? "")
    && baseUrl.protocol !== "https:"
    && !privateHttpAllowed
  ) {
    throw new Error(
      "Hosted AGENT_INTERNAL_URL must use HTTPS unless private HTTP is explicitly allowed.",
    );
  }

  const timeoutMs = parseBoundedInteger(
    environment.AGENT_REQUEST_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    "AGENT_REQUEST_TIMEOUT_MS",
  );

  return {
    baseUrl,
    timeoutMs,
    maxResponseBytes: DEFAULT_MAX_RESPONSE_BYTES,
    fetchImpl,
    internalToken: readRequiredServerSecret(
      environment,
      "AGENT_BFF_SHARED_SECRET",
      { headerSafe: true },
    ),
  };
}

export async function proxyAgentRequestFromEnvironment(
  options: AgentProxyRequest,
  environment: AgentProxyEnvironment = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  let runtime: AgentProxyRuntime;
  let session: ReturnType<typeof resolveAnonymousSession>;
  try {
    runtime = loadAgentProxyRuntime(environment, fetchImpl);
    const sessionConfig = loadAnonymousSessionConfig(environment);
    if (runtime.internalToken === sessionConfig.secret) {
      throw new Error("BFF and anonymous session secrets must be different.");
    }
    if (isCrossSiteMutation(options.request, options.method, sessionConfig)) {
      return createBffErrorResponse(
        options.request,
        403,
        "BFF_CSRF_REJECTED",
        "요청 출처를 확인할 수 없습니다.",
        false,
      );
    }
    session = resolveAnonymousSession(options.request, sessionConfig);
  } catch {
    return createBffErrorResponse(
      options.request,
      500,
      "BFF_CONFIGURATION_ERROR",
      "서비스 연결 설정을 확인할 수 없습니다.",
      false,
    );
  }
  const response = await proxyAgentRequest(options, runtime, session.ownerId);
  return appendAnonymousSessionCookie(response, session);
}

function isCrossSiteMutation(
  request: Request,
  method: AgentProxyRequest["method"],
  sessionConfig: ReturnType<typeof loadAnonymousSessionConfig>,
): boolean {
  if (method === "GET") {
    return false;
  }
  const fetchSite = request.headers.get("sec-fetch-site")?.toLowerCase();
  if (fetchSite !== undefined && !["same-origin", "none"].includes(fetchSite)) {
    return true;
  }
  const origin = request.headers.get("origin");
  if (origin === null) {
    return sessionConfig.hosted;
  }
  try {
    const expectedOrigin = sessionConfig.publicOrigin ?? new URL(request.url).origin;
    return new URL(origin).origin !== expectedOrigin;
  } catch {
    return true;
  }
}

export async function proxyAgentRequest(
  options: AgentProxyRequest,
  runtime: AgentProxyRuntime,
  ownerId: string,
): Promise<Response> {
  const requestId = getOrCreateRequestId(options.request);
  if (!OWNER_ID_PATTERN.test(ownerId)) {
    return createBffErrorResponse(
      options.request,
      500,
      "BFF_OWNER_CONFIGURATION_ERROR",
      "사용자 세션을 안전하게 확인할 수 없습니다.",
      false,
      requestId,
    );
  }
  if (!isSafeUpstreamPath(options.upstreamPath)) {
    return createBffErrorResponse(
      options.request,
      500,
      "BFF_ROUTE_CONFIGURATION_ERROR",
      "요청 경로를 구성할 수 없습니다.",
      false,
      requestId,
    );
  }
  if (options.idempotentMutation === true && options.method !== "POST") {
    return createBffErrorResponse(
      options.request,
      500,
      "BFF_ROUTE_CONFIGURATION_ERROR",
      "요청 경로를 구성하지 못했습니다.",
      false,
      requestId,
    );
  }

  let body: string | undefined;
  if (options.requestSchema !== undefined) {
    let bodyResult: ValidatedBody | BodyFailure;
    try {
      bodyResult = await validateRequestBody(
        options.request,
        options.requestSchema,
        requestId,
      );
    } catch {
      return createBffErrorResponse(
        options.request,
        422,
        "BFF_REQUEST_BODY_UNREADABLE",
        "요청 본문을 읽을 수 없습니다.",
        false,
        requestId,
      );
    }
    if (!bodyResult.ok) {
      return bodyResult.response;
    }
    body = bodyResult.raw;
  }

  let idempotencyKey: string | undefined;
  if (options.idempotentMutation === true) {
    const suppliedKey = options.request.headers.get(IDEMPOTENCY_KEY_HEADER);
    if (suppliedKey !== null && !IDEMPOTENCY_KEY_PATTERN.test(suppliedKey)) {
      return createBffErrorResponse(
        options.request,
        422,
        "BFF_IDEMPOTENCY_KEY_INVALID",
        "요청 중복 방지 키의 형식이 올바르지 않습니다.",
        false,
        requestId,
      );
    }
    idempotencyKey = suppliedKey ?? randomUUID();
  }

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, runtime.timeoutMs);

  let upstream: Response;
  let bytes: Uint8Array<ArrayBuffer>;
  try {
    const headers = new Headers({
      accept: "application/json",
      [INTERNAL_TOKEN_HEADER]: runtime.internalToken,
      [OWNER_ID_HEADER]: ownerId,
      [REQUEST_ID_HEADER]: requestId,
    });
    if (options.responseSchemaVersion !== undefined) {
      headers.set(RESPONSE_SCHEMA_HEADER, options.responseSchemaVersion);
    }
    if (idempotencyKey !== undefined) {
      headers.set(IDEMPOTENCY_KEY_HEADER, idempotencyKey);
    }
    if (body !== undefined) {
      headers.set("content-type", "application/json");
    }
    const upstreamInit: RequestInit = {
      method: options.method,
      headers,
      cache: "no-store",
      signal: controller.signal,
    };
    if (body !== undefined) {
      upstreamInit.body = body;
    }
    upstream = await runtime.fetchImpl(new URL(options.upstreamPath, runtime.baseUrl), upstreamInit);
    const declaredLength = parseContentLength(upstream.headers.get("content-length"));
    if (declaredLength !== null && declaredLength > runtime.maxResponseBytes) {
      return contractFailure(options.request, requestId);
    }
    bytes = new Uint8Array(await upstream.arrayBuffer());
    if (bytes.byteLength > runtime.maxResponseBytes) {
      return contractFailure(options.request, requestId);
    }
  } catch {
    const mutationOutcomeUnknown = options.method === "POST";
    let code = "AGENT_UPSTREAM_UNAVAILABLE";
    let message = "이동 정보 서비스를 일시적으로 사용할 수 없습니다.";
    if (mutationOutcomeUnknown) {
      code = "AGENT_MUTATION_OUTCOME_UNKNOWN";
      message = "요청 처리 결과를 확인할 수 없습니다. 같은 요청을 자동으로 다시 보내지 마세요.";
    } else if (timedOut) {
      code = "AGENT_UPSTREAM_TIMEOUT";
      message = "이동 정보 확인 시간이 초과되었습니다.";
    }
    return createBffErrorResponse(
      options.request,
      timedOut ? 504 : 503,
      code,
      message,
      !mutationOutcomeUnknown,
      requestId,
    );
  } finally {
    clearTimeout(timer);
  }

  const isSuccess = options.successStatuses.includes(upstream.status);
  if (isSuccess && options.allowEmptySuccess === true) {
    if (bytes.byteLength !== 0) {
      return contractFailure(options.request, requestId);
    }
    return mirrorResponse(upstream, bytes, requestId, null);
  }

  const schema = isSuccess
    ? options.successSchema
    : (options.errorSchema ?? AgentErrorResponseSchema);
  if (schema === undefined || bytes.byteLength === 0 || !isJson(upstream.headers)) {
    return contractFailure(options.request, requestId);
  }

  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return contractFailure(options.request, requestId);
  }
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    return contractFailure(options.request, requestId);
  }

  const bodyRequestId = extractRequestId(parsed.data);
  const headerRequestId = validRequestId(upstream.headers.get(REQUEST_ID_HEADER));
  if (bodyRequestId !== null && headerRequestId !== null && bodyRequestId !== headerRequestId) {
    return contractFailure(options.request, requestId);
  }

  const responseBytes = isSuccess
    ? bytes
    : serializeValidatedError(parsed.data, options.request, requestId);
  if (responseBytes instanceof Response) {
    return responseBytes;
  }

  return mirrorResponse(
    upstream,
    responseBytes,
    requestId,
    headerRequestId ?? bodyRequestId,
  );
}

export function createBffErrorResponse(
  request: Request,
  status: number,
  code: string,
  message: string,
  retryable: boolean,
  explicitRequestId?: string,
): Response {
  const requestId = explicitRequestId ?? getOrCreateRequestId(request);
  const payload: AgentErrorResponse = {
    schema_version: "2",
    request_id: requestId,
    error: {
      code,
      message,
      retryable,
      detail: {},
    },
  };
  return Response.json(payload, {
    status,
    headers: {
      "cache-control": "no-store",
      [REQUEST_ID_HEADER]: requestId,
    },
  });
}

async function validateRequestBody(
  request: Request,
  schema: ZodType,
  requestId: string,
): Promise<ValidatedBody | BodyFailure> {
  const contentLength = parseContentLength(request.headers.get("content-length"));
  if (contentLength !== null && contentLength > MAX_REQUEST_BYTES) {
    return {
      ok: false,
      response: createBffErrorResponse(
        request,
        413,
        "BFF_REQUEST_TOO_LARGE",
        "요청 본문이 너무 큽니다.",
        false,
        requestId,
      ),
    };
  }
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_REQUEST_BYTES) {
    return {
      ok: false,
      response: createBffErrorResponse(
        request,
        413,
        "BFF_REQUEST_TOO_LARGE",
        "요청 본문이 너무 큽니다.",
        false,
        requestId,
      ),
    };
  }

  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return requestValidationFailure(request, requestId);
  }
  if (!schema.safeParse(payload).success) {
    return requestValidationFailure(request, requestId);
  }
  return { ok: true, raw };
}

function requestValidationFailure(request: Request, requestId: string): BodyFailure {
  return {
    ok: false,
    response: createBffErrorResponse(
      request,
      422,
      "BFF_REQUEST_VALIDATION_ERROR",
      "요청 형식이 올바르지 않습니다.",
      false,
      requestId,
    ),
  };
}

function contractFailure(request: Request, requestId: string): Response {
  return createBffErrorResponse(
    request,
    502,
    "AGENT_CONTRACT_INVALID",
    "이동 정보 서비스의 응답을 안전하게 확인할 수 없습니다.",
    false,
    requestId,
  );
}

function serializeValidatedError(
  payload: unknown,
  request: Request,
  requestId: string,
): Uint8Array<ArrayBuffer> | Response {
  try {
    const serialized = JSON.stringify(payload);
    if (serialized === undefined) {
      return contractFailure(request, requestId);
    }
    return new TextEncoder().encode(serialized);
  } catch {
    return contractFailure(request, requestId);
  }
}

function mirrorResponse(
  upstream: Response,
  bytes: Uint8Array<ArrayBuffer>,
  fallbackRequestId: string,
  upstreamRequestId: string | null,
): Response {
  const headers = new Headers({ "cache-control": "no-store" });
  const contentType = upstream.headers.get("content-type");
  if (contentType !== null) {
    headers.set("content-type", contentType);
  }
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter !== null) {
    headers.set("retry-after", retryAfter);
  }
  headers.set(REQUEST_ID_HEADER, upstreamRequestId ?? fallbackRequestId);
  return new Response(bytes.byteLength === 0 ? null : bytes, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

function getOrCreateRequestId(request: Request): string {
  return validRequestId(request.headers.get(REQUEST_ID_HEADER)) ?? randomUUID();
}

function validRequestId(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  return RequestIdSchema.safeParse(value).success ? value : null;
}

function extractRequestId(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("request_id" in value)) {
    return null;
  }
  const requestId = (value as { request_id?: unknown }).request_id;
  return typeof requestId === "string" ? validRequestId(requestId) : null;
}

function isSafeUpstreamPath(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//") && !path.includes("?") && !path.includes("#");
}

function isJson(headers: Headers): boolean {
  const contentType = headers.get("content-type");
  return contentType !== null && /^application\/json(?:\s*;|$)/i.test(contentType);
}

function parseContentLength(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) {
    return null;
  }
  return Number(value);
}

function parseBoundedInteger(
  value: string | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined || value === "") {
    return fallback;
  }
  if (!/^\d+$/.test(value)) {
    throw new Error(`${name} must be an integer.`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}.`);
  }
  return parsed;
}
