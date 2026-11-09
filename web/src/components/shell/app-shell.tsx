"use client";

import {
  AccessibilityIcon,
  ActivityIcon,
  BookOpenIcon,
  RouteIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { ThemeToggle } from "@/components/theme-provider";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { cn } from "@/lib/utils";

type ShellPath = "/" | "/accessibility" | "/assistant" | "/how-it-works" | "/status";

export type AppShellProps = Readonly<{
  children: ReactNode;
  className?: string;
  currentPath?: ShellPath;
  footer?: ReactNode;
  headerActions?: ReactNode;
  mainClassName?: string;
}>;

type NavigationItem = Readonly<{ href: Exclude<ShellPath, "/">; label: string; icon: LucideIcon }>;

const NAVIGATION_GROUPS: ReadonlyArray<
  Readonly<{ label: string; items: ReadonlyArray<NavigationItem> }>
> = [
  {
    label: "서비스",
    items: [
      { href: "/assistant", label: "이동 조회", icon: RouteIcon },
      { href: "/how-it-works", label: "작동 방식", icon: BookOpenIcon },
      { href: "/status", label: "서비스 상태", icon: ActivityIcon },
    ],
  },
  {
    label: "도움말",
    items: [{ href: "/accessibility", label: "접근성 안내", icon: AccessibilityIcon }],
  },
];

function currentLabel(path: ShellPath | undefined): string | null {
  if (path === "/") return "홈";
  for (const group of NAVIGATION_GROUPS) {
    const item = group.items.find((candidate) => candidate.href === path);
    if (item) return item.label;
  }
  return null;
}

// 셸은 페이지마다 다시 그려지지만 모바일 메뉴는 링크를 누른 즉시 닫아 이동한 화면을 가리지 않게 한다.
function ShellNavigation({ currentPath }: { currentPath: ShellPath | undefined }) {
  const { isMobile, setOpenMobile } = useSidebar();

  return (
    // 브랜드 링크까지 한 navigation landmark 에 둔다. 사이드바 안의 모든 내용이 landmark 안에 있다.
    <nav aria-label="주요 메뉴" className="flex min-h-0 flex-1 flex-col">
      <SidebarHeader className="relative px-2 py-4">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              size="lg"
              render={<Link href="/" />}
              aria-current={currentPath === "/" ? "page" : undefined}
              onClick={() => setOpenMobile(false)}
            >
              <span
                aria-hidden="true"
                className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground"
              >
                <AccessibilityIcon className="size-4" />
              </span>
              <span className="grid flex-1 text-left leading-tight">
                <span className="truncate font-semibold">Barrier-Free Mobility</span>
                <span className="truncate text-xs">이동 접근성 확인</span>
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        {isMobile ? (
          <Button
            variant="ghost"
            size="icon"
            className="absolute top-3 right-2"
            aria-label="메뉴 닫기"
            onClick={() => setOpenMobile(false)}
          >
            <XIcon aria-hidden="true" />
          </Button>
        ) : null}
      </SidebarHeader>
      <SidebarContent className="px-2">
        {NAVIGATION_GROUPS.map((group) => (
          <SidebarGroup key={group.label} className="p-0">
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {group.items.map(({ href, label, icon: Icon }) => (
                  <SidebarMenuItem key={href}>
                    <SidebarMenuButton
                      className="h-11"
                      render={<Link href={href} />}
                      isActive={currentPath === href}
                      aria-current={currentPath === href ? "page" : undefined}
                      onClick={() => setOpenMobile(false)}
                    >
                      <Icon aria-hidden="true" />
                      <span>{label}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
    </nav>
  );
}

export function AppShell({
  children,
  className,
  currentPath,
  footer,
  headerActions,
  mainClassName,
}: AppShellProps) {
  const pageLabel = currentLabel(currentPath);

  return (
    <SidebarProvider className={className} data-ui="app-shell">
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-lg focus:bg-background focus:px-4 focus:py-3 focus:text-sm focus:font-medium focus:shadow-lg"
        href="#main-content"
      >
        본문으로 건너뛰기
      </a>
      {/* ui.shadcn.com 대시보드 블록과 같은 구조다. SidebarInset 의 inset 스타일이 peer 선택자로
          걸려 있으므로 Sidebar 와 SidebarInset 은 SidebarProvider 아래 형제로 둔다. */}
      <Sidebar collapsible="offcanvas" variant="inset">
        <ShellNavigation currentPath={currentPath} />
      </Sidebar>
      <SidebarInset className="min-w-0">
        <header className="flex min-h-14 items-center gap-2 border-b px-4 sm:px-6">
          <SidebarTrigger className="-ml-2 size-11" />
          <Separator
            orientation="vertical"
            className="mr-1 data-vertical:h-4 data-vertical:self-center"
          />
          <Breadcrumb className="min-w-0 flex-1">
            <BreadcrumbList>
              {currentPath !== "/" ? (
                <>
                  <BreadcrumbItem className="hidden md:inline-flex">
                    <BreadcrumbLink render={<Link href="/" />}>Barrier-Free Mobility</BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                </>
              ) : null}
              {pageLabel ? (
                <BreadcrumbItem>
                  <BreadcrumbPage>{pageLabel}</BreadcrumbPage>
                </BreadcrumbItem>
              ) : null}
            </BreadcrumbList>
          </Breadcrumb>
          <div className="flex items-center gap-2">
            {headerActions}
            <ThemeToggle />
          </div>
        </header>

        <main
          className={cn("mx-auto w-full max-w-[90rem] flex-1 p-4 sm:p-6 lg:p-8", mainClassName)}
          id="main-content"
          tabIndex={-1}
        >
          {children}
        </main>

        <footer className="border-t px-4 py-4 text-sm text-muted-foreground sm:px-6">
          <div className="mx-auto flex w-full max-w-[90rem] flex-wrap items-center justify-between gap-x-6 gap-y-2">
            {footer ?? (
              <>
                <small className="text-sm">
                  제공되는 정보의 제한 사항과 확인 시각을 함께 확인해 주세요.
                </small>
                <nav aria-label="지원 메뉴">
                  <Link
                    className="inline-flex min-h-11 items-center font-medium text-foreground underline-offset-4 hover:underline"
                    href="/accessibility"
                  >
                    접근성 안내
                  </Link>
                </nav>
              </>
            )}
          </div>
        </footer>
      </SidebarInset>
    </SidebarProvider>
  );
}
