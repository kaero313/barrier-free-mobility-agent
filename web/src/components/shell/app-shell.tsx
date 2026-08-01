import type { ReactNode } from "react";
import Link from "next/link";

import { cx } from "@/lib/class-names";

import styles from "./app-shell.module.css";

export type AppShellProps = Readonly<{
  children: ReactNode;
  className?: string;
  currentPath?: "/" | "/accessibility" | "/assistant" | "/how-it-works" | "/status";
  footer?: ReactNode;
  headerActions?: ReactNode;
  mainClassName?: string;
}>;

const PRIMARY_NAVIGATION = [
  { href: "/assistant", label: "이동 조회" },
  { href: "/how-it-works", label: "작동 방식" },
  { href: "/status", label: "서비스 상태" },
] as const;

export function AppShell({
  children,
  className,
  currentPath,
  footer,
  headerActions,
  mainClassName,
}: AppShellProps) {
  return (
    <div className={cx(styles.shell, className)} data-ui="app-shell">
      <a className={styles.skipLink} href="#main-content">
        본문으로 건너뛰기
      </a>

      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link
            aria-current={currentPath === "/" ? "page" : undefined}
            className={styles.brand}
            href="/"
          >
            <span aria-hidden="true" className={styles.brandMark}>
              BF
            </span>
            <span className={styles.brandText}>
              <span className={styles.brandName}>Barrier-Free Mobility</span>
              <span className={styles.brandPurpose}>이동 접근성 확인</span>
            </span>
          </Link>

          <nav aria-label="주요 메뉴" className={styles.primaryNavigation}>
            <ul className={styles.navigationList}>
              {PRIMARY_NAVIGATION.map((item) => (
                <li key={item.href}>
                  <a
                    aria-current={currentPath === item.href ? "page" : undefined}
                    className={cx(
                      styles.navigationLink,
                      currentPath === item.href && styles.navigationLinkCurrent,
                    )}
                    href={item.href}
                  >
                    {item.label}
                  </a>
                </li>
              ))}
              <li className={styles.mobileSupportItem}>
                <a
                  aria-current={currentPath === "/accessibility" ? "page" : undefined}
                  className={cx(
                    styles.navigationLink,
                    currentPath === "/accessibility" && styles.navigationLinkCurrent,
                  )}
                  href="/accessibility"
                >
                  접근성 안내
                </a>
              </li>
            </ul>
          </nav>

          {headerActions ? <div className={styles.headerActions}>{headerActions}</div> : null}
        </div>
      </header>

      <main className={cx(styles.main, mainClassName)} id="main-content" tabIndex={-1}>
        {children}
      </main>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          {footer ?? (
            <>
              <small>제공되는 정보의 제한 사항과 확인 시각을 함께 확인해 주세요.</small>
              <nav aria-label="지원 메뉴" className={styles.footerNavigation}>
                <a href="/accessibility">접근성 안내</a>
              </nav>
            </>
          )}
        </div>
      </footer>
    </div>
  );
}
