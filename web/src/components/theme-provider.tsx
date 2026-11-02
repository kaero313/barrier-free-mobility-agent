"use client";

import { MoonIcon, SunIcon } from "lucide-react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import { useSyncExternalStore, type ComponentProps } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// 참고 프로젝트의 "d" 단일 키 전환은 두지 않는다. 끄거나 바꿀 수 없는 문자 단축키는
// 음성 입력 사용자에게 오작동을 일으킨다(WCAG 2.1.4).
export function ThemeProvider(props: ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      {...props}
    />
  );
}

const subscribe = () => () => undefined;

// 서버 렌더링에서는 테마를 알 수 없으므로 hydration 뒤에만 현재 테마를 반영한다.
function useHasHydrated() {
  return useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
}

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const hasHydrated = useHasHydrated();
  const isDark = hasHydrated && resolvedTheme === "dark";
  const label = isDark ? "라이트 모드로 전환" : "다크 모드로 전환";

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            aria-label={label}
            onClick={() => setTheme(isDark ? "light" : "dark")}
          />
        }
      >
        {isDark ? <SunIcon aria-hidden="true" /> : <MoonIcon aria-hidden="true" />}
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
