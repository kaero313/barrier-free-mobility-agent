import { deleteThread } from "../../_handlers";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

interface ThreadRouteContext {
  params: Promise<{ threadId: string }>;
}

export async function DELETE(
  request: Request,
  context: ThreadRouteContext,
): Promise<Response> {
  const { threadId } = await context.params;
  return deleteThread(request, threadId);
}
