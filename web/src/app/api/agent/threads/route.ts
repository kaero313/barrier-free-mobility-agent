import { createThread } from "../_handlers";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  return createThread(request);
}
