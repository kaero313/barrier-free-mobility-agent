import type { Metadata } from "next";

import { AppShell } from "@/components/shell/app-shell";
import { CurrentSupportSummary } from "@/features/public-status";

import styles from "./public-page.module.css";

export const metadata: Metadata = {
  description: "확인된 정보와 한계를 바탕으로 교통약자의 이동 경로 접근성을 안내합니다.",
};

export default function HomePage() {
  return (
    <AppShell currentPath="/">
      <div className={styles.page}>
        <section aria-labelledby="product-entry-title" className={styles.hero}>
          <p className="eyebrow">이동 접근성 정보</p>
          <h1 id="product-entry-title">
            교통약자의 이동 가능성을 확인된 정보와 한계를 함께 안내합니다.
          </h1>
          <p className={styles.heroText}>
            출발역, 도착역과 필요한 이동 조건을 자연어로 질문하세요. 확인된 경로 정보와 주의할
            점, 확인하지 못한 항목을 구분해 보여드립니다.
          </p>
          <div className={styles.actions}>
            <a className={styles.primaryAction} href="/assistant">
              이동 조회 시작
            </a>
            <a className={styles.secondaryAction} href="/how-it-works">
              작동 방식 확인
            </a>
          </div>
        </section>

        <section aria-labelledby="safety-principles-title" className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 id="safety-principles-title">안전한 안내 원칙</h2>
            <p>확신을 과장하기보다 사용자가 판단에 필요한 근거와 한계를 함께 확인하도록 돕습니다.</p>
          </div>
          <div className={styles.cardGrid}>
            <article className={styles.card}>
              <h3>부족한 조건을 확인합니다</h3>
              <p>역이나 이동 조건이 분명하지 않으면 임의로 정하지 않고 사용자에게 다시 묻습니다.</p>
            </article>
            <article className={styles.card}>
              <h3>미확인 정보를 숨기지 않습니다</h3>
              <p>일부 정보만 확인됐거나 조회하지 못한 항목을 성공 결과와 구분해 표시합니다.</p>
            </article>
            <article className={styles.card}>
              <h3>근거와 확인 시각을 제공합니다</h3>
              <p>공개할 수 있는 정보 출처, 제한 사항과 확인 시각을 결과와 함께 제공합니다.</p>
            </article>
          </div>
        </section>

        <CurrentSupportSummary />
      </div>
    </AppShell>
  );
}
