import type { Metadata } from "next";

import { AppShell } from "@/components/shell/app-shell";
import { AssistantWorkspace } from "@/features/assistant/assistant-workspace";

import styles from "./page.module.css";

export const metadata: Metadata = {
  title: "이동 경로 접근성 확인",
  description: "자연어 질문으로 이동 경로와 교통약자 접근성을 확인합니다.",
};

export default function AssistantPage() {
  return (
    <AppShell currentPath="/assistant" mainClassName={styles.main!}>
      <AssistantWorkspace />
    </AppShell>
  );
}
