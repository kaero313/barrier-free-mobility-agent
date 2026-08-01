import type { ComponentPropsWithoutRef, ReactNode } from "react";

import type { StatusTone } from "@/theme/tokens";
import { cx } from "@/lib/class-names";

import styles from "./status-badge.module.css";

const TONE_ICON: Record<StatusTone, string> = {
  success: "✓",
  partial: "!",
  danger: "×",
  info: "i",
  unknown: "?",
};

export type StatusBadgeProps = ComponentPropsWithoutRef<"span"> & {
  children: ReactNode;
  tone: StatusTone;
};

export function StatusBadge({ children, className, tone, ...props }: StatusBadgeProps) {
  return (
    <span {...props} className={cx(styles.badge, styles[tone], className)} data-ui="status-badge">
      <span aria-hidden="true" className={styles.icon}>
        {TONE_ICON[tone]}
      </span>
      {children}
    </span>
  );
}
