import { CircleAlertIcon } from "lucide-react";
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { PageHeader } from "@/components/page-header";
import { AppShell } from "@/components/shell/app-shell";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "접근성 안내",
  description: "키보드와 화면 읽기 프로그램 사용법, 접근성 지원 기준을 안내합니다.",
};

function GuideSection({ children, id, title }: { children: ReactNode; id: string; title: string }) {
  return (
    <section aria-labelledby={id}>
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id={id} className="text-lg font-semibold">
              {title}
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 text-base leading-7">{children}</CardContent>
      </Card>
    </section>
  );
}

export default function AccessibilityPage() {
  return (
    <AppShell currentPath="/accessibility">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
        <PageHeader
          title="화면을 이용하는 방법"
          description="키보드, 화면 읽기 프로그램과 화면 확대 환경에서도 핵심 이동 조회 흐름을 사용할 수 있도록 설계하고 검증합니다."
        />

        <GuideSection id="keyboard-guide-title" title="키보드로 이용하기">
          <ul className="ml-5 list-disc space-y-2">
            <li>Tab과 Shift+Tab으로 링크, 입력창과 버튼 사이를 이동할 수 있습니다.</li>
            <li>
              페이지 첫 포커스 항목인 <strong>본문으로 건너뛰기</strong> 링크로 주요 내용으로 이동할
              수 있습니다.
            </li>
            <li>
              화면이 좁을 때는 왼쪽 위 메뉴 버튼으로 탐색 메뉴를 열고 Esc로 닫을 수 있습니다. 넓은
              화면에서는 Ctrl+B 또는 Command+B로 사이드바를 접고 펼 수 있습니다.
            </li>
            <li>질문 입력창에서 Enter는 줄바꿈이며, Ctrl+Enter 또는 Command+Enter로 제출합니다.</li>
            <li>추가 질문의 선택지는 기본 선택 없이 제공되며 방향키와 Space로 선택할 수 있습니다.</li>
          </ul>
        </GuideSection>

        <GuideSection id="screen-reader-guide-title" title="화면 읽기 프로그램 안내">
          <ul className="ml-5 list-disc space-y-2">
            <li>header, navigation, main과 footer landmark로 페이지 영역을 구분합니다.</li>
            <li>조회 상태 변경은 짧은 polite 알림으로 전달하고 전체 답변을 반복해 읽지 않습니다.</li>
            <li>추가 확인이 시작될 때만 현재 질문 제목으로 포커스를 옮깁니다.</li>
            <li>상태와 위험 수준은 색상뿐 아니라 아이콘과 완전한 문구로 함께 표시합니다.</li>
          </ul>
        </GuideSection>

        <GuideSection id="display-support-title" title="확대, 대비와 움직임">
          <p>
            WCAG 2.2 AA를 기본 설계·검증 기준으로 사용합니다. 320 CSS px와 400% 확대에서 한 열로
            재배치하고, 라이트·다크 테마와 Windows 고대비(forced-colors), 움직임 감소 설정을
            지원합니다. 테마는 기기 설정을 따르며 화면 오른쪽 위 버튼으로 바꿀 수 있습니다.
          </p>
        </GuideSection>

        <GuideSection id="feedback-title" title="접근성 피드백">
          <Alert role="note">
            <CircleAlertIcon aria-hidden="true" />
            <AlertTitle>현재 공개 피드백 접수 채널은 아직 연결되어 있지 않습니다.</AlertTitle>
            <AlertDescription className="text-sm leading-6">
              존재하지 않는 연락처를 안내하지 않기 위해 미구성 상태를 명확히 표시합니다. 접수 채널이
              준비되면 이 페이지에 주소와 처리 범위를 안내하겠습니다.
            </AlertDescription>
          </Alert>
          <p>
            채널이 연결된 뒤 피드백을 보낼 때는 사용한 브라우저와 보조 기술, 문제가 발생한 화면,
            재현 순서를 함께 알려주면 확인에 도움이 됩니다.
          </p>
        </GuideSection>
      </div>
    </AppShell>
  );
}
