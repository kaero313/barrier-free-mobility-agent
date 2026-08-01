import type { Metadata } from "next";

import { AppShell } from "@/components/shell/app-shell";
import { PublicStatusView } from "@/features/public-status";

import styles from "../public-page.module.css";

export const metadata: Metadata = {
  title: "서비스 상태",
  description: "현재 이동 조회 가능 여부, 지원 범위와 마지막 확인 시각을 확인합니다.",
};

export default function StatusPage() {
  return (
    <AppShell currentPath="/status">
      <div className={styles.page}>
        <header className={styles.pageHeader}>
          <p className="eyebrow">서비스 상태</p>
          <h1>서비스 상태</h1>
          <p className={styles.pageHeaderText}>
            지금 이동 조회를 시작할 수 있는지와 현재 지원하는 질문 범위를 확인합니다.
          </p>
        </header>
        <PublicStatusView />
      </div>
    </AppShell>
  );
}
