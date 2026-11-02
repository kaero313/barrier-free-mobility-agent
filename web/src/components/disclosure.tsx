import { ChevronDownIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type DisclosureProps = Readonly<{
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  summary: ReactNode;
}>;

// 근거·신고 정보처럼 접어 두는 내용은 native details 로 둔다. JavaScript 없이도 열리고
// 닫힌 동안에도 내용이 DOM 에 남아 페이지 내 검색과 보조 기술에서 찾을 수 있다.
export function Disclosure({ children, className, contentClassName, summary }: DisclosureProps) {
  return (
    <details className={cn("group/disclosure rounded-lg border", className)}>
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-3 text-sm font-medium hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
        <span>{summary}</span>
        <ChevronDownIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-open/disclosure:rotate-180"
        />
      </summary>
      <div
        className={cn(
          "flex flex-col gap-4 border-t px-3 py-3 text-sm leading-6",
          contentClassName,
        )}
      >
        {children}
      </div>
    </details>
  );
}
