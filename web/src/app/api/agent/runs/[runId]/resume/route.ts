import { resumeRun } from "../../../_handlers";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

interface ResumeRunRouteContext {
  params: Promise<{ runId: string }>;
}

export async function POST(
  request: Request,
  context: ResumeRunRouteContext,
): Promise<Response> {
  const { runId } = await context.params;
  return resumeRun(request, runId);
}
