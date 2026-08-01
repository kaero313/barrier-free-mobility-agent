import { getRun } from "../../_handlers";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

interface RunRouteContext {
  params: Promise<{ runId: string }>;
}

export async function GET(
  request: Request,
  context: RunRouteContext,
): Promise<Response> {
  const { runId } = await context.params;
  return getRun(request, runId);
}
