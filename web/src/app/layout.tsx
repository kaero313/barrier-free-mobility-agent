import type { Metadata, Viewport } from "next";
import { Noto_Sans_KR } from "next/font/google";
import type { ReactNode } from "react";

// Vendor styles are loaded once at the App Router boundary. Astryx components
// themselves may only be imported through components/primitives/astryx-adapter.
import "@astryxdesign/core/astryx.css";
import "./globals.css";

// Self-hosted by Next.js at build time, so the product does not depend on a
// third-party font host at runtime or on the reader having the font installed.
// The Korean subset is large, so it is not preloaded; `swap` keeps the first
// paint readable with the metric-adjusted fallback.
const notoSansKr = Noto_Sans_KR({
  subsets: ["latin"],
  variable: "--font-noto-sans-kr",
  display: "swap",
  preload: false,
});

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
    <html lang="ko" className={notoSansKr.variable}>
      <body>{children}</body>
    </html>
  );
}
