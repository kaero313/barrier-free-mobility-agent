import { startRun } from "../../../_handlers";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

interface ThreadRunsRouteContext {
  params: Promise<{ threadId: string }>;
}

export async function POST(
  request: Request,
  context: ThreadRunsRouteContext,
): Promise<Response> {
  const { threadId } = await context.params;
  return startRun(request, threadId);
}
