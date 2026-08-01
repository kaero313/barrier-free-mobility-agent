import {
  createUnknownPublicServiceStatus,
} from "@/server/public-status/mapper";
import { loadPublicServiceStatus } from "@/server/public-status/service";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  let status;
  try {
    status = await loadPublicServiceStatus();
  } catch {
    status = createUnknownPublicServiceStatus();
  }
  return Response.json(status, {
    status: 200,
    headers: { "cache-control": "no-store" },
  });
}

