import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import type { ReactNode } from "react";

import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";

import "./globals.css";

// 외부 font host 와 읽는 사람 기기의 설치 폰트에 기대지 않도록 빌드 때 함께 배포한다.
// 가변 폰트 하나로 모든 굵기를 쓴다.
const pretendard = localFont({
  src: "./fonts/PretendardVariable.woff2",
  weight: "45 920",
  display: "swap",
  variable: "--font-pretendard",
  fallback: ["Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", "sans-serif"],
});

export const metadata: Metadata = {
  title: {
    default: "Barrier-Free Mobility",
    template: "%s | Barrier-Free Mobility",
  },
  description: "교통약자의 이동 조건을 확인하는 대화형 이동 정보 서비스",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#020618" },
  ],
};

type RootLayoutProps = Readonly<{
  children: ReactNode;
}>;

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="ko" suppressHydrationWarning className={`${pretendard.variable} antialiased`}>
      <body>
        <ThemeProvider>
          <TooltipProvider>{children}</TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
