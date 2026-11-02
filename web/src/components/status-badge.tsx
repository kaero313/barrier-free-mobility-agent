import {
  CircleCheckIcon,
  CircleHelpIcon,
  CircleXIcon,
  InfoIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type StatusTone = "success" | "partial" | "danger" | "info" | "unknown";

// 기본 Badge variant 만 쓴다. 색만으로 상태를 구분하지 않도록 아이콘과 완전한 문구를 항상 함께 둔다.
const TONES: Record<
  StatusTone,
  Readonly<{ variant: "default" | "secondary" | "destructive" | "outline"; icon: LucideIcon }>
> = {
  success: { variant: "default", icon: CircleCheckIcon },
  partial: { variant: "secondary", icon: TriangleAlertIcon },
  danger: { variant: "destructive", icon: CircleXIcon },
  info: { variant: "secondary", icon: InfoIcon },
  unknown: { variant: "outline", icon: CircleHelpIcon },
};

// 아이콘 색은 보조 신호다. 상태는 아이콘 모양과 곁의 문구가 말한다.
const TONE_ICON_COLORS: Record<StatusTone, string> = {
  success: "text-success",
  partial: "text-warning",
  danger: "text-destructive",
  info: "text-info",
  unknown: "text-muted-foreground",
};

// 제목이 이미 상태를 문장으로 말할 때는 배지 대신 아이콘만 붙인다.
export function StatusIcon({ className, tone }: { className?: string; tone: StatusTone }) {
  const { icon: Icon } = TONES[tone];
  return (
    <Icon
      aria-hidden="true"
      className={cn("size-5 shrink-0", TONE_ICON_COLORS[tone], className)}
      data-tone={tone}
    />
  );
}

export type StatusBadgeProps =Omit<ComponentProps<typeof Badge>, "children" | "variant"> & {
  children: ReactNode;
  tone: StatusTone;
};

export function StatusBadge({ children, className, tone, ...props }: StatusBadgeProps) {
  const { icon: Icon, variant } = TONES[tone];
  return (
    <Badge
      {...props}
      className={cn("h-6 px-2.5 text-sm", className)}
      data-tone={tone}
      data-ui="status-badge"
      variant={variant}
    >
      <Icon aria-hidden="true" data-icon="inline-start" />
      {children}
    </Badge>
  );
}
