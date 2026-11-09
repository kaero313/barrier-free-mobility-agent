import type { Metadata } from "next";

import { PageHeader } from "@/components/page-header";
import { AppShell } from "@/components/shell/app-shell";
import { PublicStatusView } from "@/features/public-status";

export const metadata: Metadata = {
  title: "서비스 상태",
  description: "현재 이동 조회 가능 여부, 지원 범위와 마지막 확인 시각을 확인합니다.",
};

export default function StatusPage() {
  return (
    <AppShell currentPath="/status">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
        <PageHeader
          title="서비스 상태"
          description="지금 이동 조회를 시작할 수 있는지와 현재 지원하는 질문 범위를 확인합니다."
        />
        <PublicStatusView />
      </div>
    </AppShell>
  );
}
