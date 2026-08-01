import {
  PublicServiceStatusSchema,
  type PublicServiceStatus,
} from "@/contracts/public-status";

export class PublicStatusClientError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "PublicStatusClientError";
  }
}

export async function getPublicStatus(signal?: AbortSignal): Promise<PublicServiceStatus> {
  let response: Response;
  try {
    response = await fetch("/api/agent/public-status", {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      ...(signal === undefined ? {} : { signal }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new PublicStatusClientError("현재 서비스 상태를 확인하지 못했습니다.", {
      cause: error,
    });
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    throw new PublicStatusClientError("서비스 상태 응답을 안전하게 확인하지 못했습니다.", {
      cause: error,
    });
  }

  const parsed = PublicServiceStatusSchema.safeParse(body);
  if (!parsed.success) {
    throw new PublicStatusClientError("서비스 상태 응답을 안전하게 확인하지 못했습니다.", {
      cause: parsed.error,
    });
  }
  return parsed.data;
}
