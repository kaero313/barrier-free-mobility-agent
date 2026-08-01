import "server-only";

import { ReadinessResponseSchema } from "@/contracts/agent";
import {
  PublicServiceStatusSchema,
  type PublicServiceStatus,
} from "@/contracts/public-status";
import { loadAgentProxyRuntime } from "@/server/agent-api/client-core";

import {
  createUnknownPublicServiceStatus,
  mapReadinessToPublicStatus,
} from "./mapper";

export const PUBLIC_STATUS_TIMEOUT_MS = 5_000;
export const PUBLIC_STATUS_MAX_RESPONSE_BYTES = 64 * 1024;

const PUBLIC_STATUS_OWNER_ID = "0".repeat(64);
const JSON_CONTENT_TYPE = /^application\/json(?:\s*;|$)/i;

export interface PublicStatusRuntime {
  baseUrl: URL;
  fetchImpl: typeof fetch;
  internalToken: string;
}

export async function loadPublicServiceStatus(
  environment: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<PublicServiceStatus> {
  try {
    const agentRuntime = loadAgentProxyRuntime(environment, fetchImpl);
    return await fetchPublicServiceStatus({
      baseUrl: agentRuntime.baseUrl,
      fetchImpl: agentRuntime.fetchImpl,
      internalToken: agentRuntime.internalToken,
    });
  } catch {
    return createUnknownPublicServiceStatus();
  }
}

export async function fetchPublicServiceStatus(
  runtime: PublicStatusRuntime,
): Promise<PublicServiceStatus> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PUBLIC_STATUS_TIMEOUT_MS);

  try {
    const response = await runtime.fetchImpl(
      new URL("/health/ready", runtime.baseUrl),
      {
        method: "GET",
        headers: {
          accept: "application/json",
          "X-Agent-Internal-Token": runtime.internalToken,
          "X-Agent-Owner-ID": PUBLIC_STATUS_OWNER_ID,
        },
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
      },
    );
    const readiness = await parseReadinessResponse(response);
    if (readiness === null) {
      return createUnknownPublicServiceStatus();
    }

    const projected = PublicServiceStatusSchema.safeParse(
      mapReadinessToPublicStatus(readiness),
    );
    return projected.success
      ? projected.data
      : createUnknownPublicServiceStatus();
  } catch {
    return createUnknownPublicServiceStatus();
  } finally {
    clearTimeout(timer);
  }
}

async function parseReadinessResponse(
  response: Response,
): Promise<ReturnType<typeof ReadinessResponseSchema.parse> | null> {
  if (response.status !== 200 && response.status !== 503) {
    return null;
  }
  const contentType = response.headers.get("content-type");
  if (contentType === null || !JSON_CONTENT_TYPE.test(contentType)) {
    return null;
  }

  const bytes = await readLimitedBody(response);
  if (bytes === null || bytes.byteLength === 0) {
    return null;
  }

  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
  const parsed = ReadinessResponseSchema.safeParse(payload);
  if (!parsed.success) {
    return null;
  }
  if (
    (response.status === 200 && parsed.data.status !== "ready") ||
    (response.status === 503 && parsed.data.status !== "not_ready")
  ) {
    return null;
  }
  return parsed.data;
}

async function readLimitedBody(response: Response): Promise<Uint8Array | null> {
  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null) {
    if (!/^\d+$/.test(declaredLength)) {
      return null;
    }
    const parsedLength = Number(declaredLength);
    if (
      !Number.isSafeInteger(parsedLength) ||
      parsedLength > PUBLIC_STATUS_MAX_RESPONSE_BYTES
    ) {
      return null;
    }
  }
  if (response.body === null) {
    return null;
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) {
        break;
      }
      total += next.value.byteLength;
      if (total > PUBLIC_STATUS_MAX_RESPONSE_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(next.value);
    }
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }

  const combined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return combined;
}

