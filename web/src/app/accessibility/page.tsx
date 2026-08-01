import type { Metadata } from "next";

import { AppShell } from "@/components/shell/app-shell";

import styles from "../public-page.module.css";

export const metadata: Metadata = {
  title: "접근성 안내",
  description: "키보드와 화면 읽기 프로그램 사용법, 접근성 지원 기준을 안내합니다.",
};

export default function AccessibilityPage() {
  return (
    <AppShell currentPath="/accessibility">
      <div className={styles.page}>
        <header className={styles.pageHeader}>
          <p className="eyebrow">접근성 안내</p>
          <h1>화면을 이용하는 방법</h1>
          <p className={styles.pageHeaderText}>
            키보드, 화면 읽기 프로그램과 화면 확대 환경에서도 핵심 이동 조회 흐름을 사용할 수 있도록
            설계하고 검증합니다.
          </p>
        </header>

        <section aria-labelledby="keyboard-guide-title" className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 id="keyboard-guide-title">키보드로 이용하기</h2>
          </div>
          <div className={styles.prose}>
            <ul>
              <li>Tab과 Shift+Tab으로 링크, 입력창과 버튼 사이를 이동할 수 있습니다.</li>
              <li>
                페이지 첫 포커스 항목인 <strong>본문으로 건너뛰기</strong> 링크로 주요 내용으로 이동할
                수 있습니다.
              </li>
              <li>질문 입력창에서 Enter는 줄바꿈이며, Ctrl+Enter 또는 Command+Enter로 제출합니다.</li>
              <li>추가 질문의 선택지는 기본 선택 없이 제공되며 방향키와 Space로 선택할 수 있습니다.</li>
            </ul>
          </div>
        </section>

        <section aria-labelledby="screen-reader-guide-title" className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 id="screen-reader-guide-title">화면 읽기 프로그램 안내</h2>
          </div>
          <div className={styles.prose}>
            <ul>
              <li>header, navigation, main과 footer landmark로 페이지 영역을 구분합니다.</li>
              <li>조회 상태 변경은 짧은 polite 알림으로 전달하고 전체 답변을 반복해 읽지 않습니다.</li>
              <li>추가 확인이 시작될 때만 현재 질문 제목으로 포커스를 옮깁니다.</li>
              <li>상태와 위험 수준은 색상뿐 아니라 아이콘과 완전한 문구로 함께 표시합니다.</li>
            </ul>
          </div>
        </section>

        <section aria-labelledby="display-support-title" className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 id="display-support-title">확대, 대비와 움직임</h2>
          </div>
          <div className={styles.prose}>
            <p>
              WCAG 2.2 AA를 기본 설계·검증 기준으로 사용합니다. 320 CSS px와 400% 확대에서 한 열로
              재배치하고, Windows 고대비와 forced-colors, 움직임 감소 설정을 지원합니다.
            </p>
          </div>
        </section>

        <section aria-labelledby="feedback-title" className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 id="feedback-title">접근성 피드백</h2>
          </div>
          <div className={styles.feedbackNotice}>
            <strong>현재 공개 피드백 접수 채널은 아직 연결되어 있지 않습니다.</strong>
            <p>
              존재하지 않는 연락처를 안내하지 않기 위해 미구성 상태를 명확히 표시합니다. 접수 채널이
              준비되면 이 페이지에 주소와 처리 범위를 안내하겠습니다.
            </p>
          </div>
          <div className={styles.prose}>
            <p>
              채널이 연결된 뒤 피드백을 보낼 때는 사용한 브라우저와 보조 기술, 문제가 발생한 화면,
              재현 순서를 함께 알려주면 확인에 도움이 됩니다.
            </p>
          </div>
        </section>
      </div>
    </AppShell>
  );
}
