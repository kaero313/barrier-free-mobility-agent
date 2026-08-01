import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

// Vendor styles are loaded once at the App Router boundary. Astryx components
// themselves may only be imported through components/primitives/astryx-adapter.
import "@astryxdesign/core/astryx.css";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Barrier-Free Mobility",
    template: "%s | Barrier-Free Mobility",
  },
  description: "교통약자의 이동 조건을 확인하는 대화형 이동 정보 서비스",
};

export const viewport: Viewport = {
  colorScheme: "light",
  themeColor: "#f8fafc",
};

type RootLayoutProps = Readonly<{
  children: ReactNode;
}>;

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
