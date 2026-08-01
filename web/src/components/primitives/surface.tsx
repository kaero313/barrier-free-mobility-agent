import type { ElementType, HTMLAttributes } from "react";

import { cx } from "@/lib/class-names";

import styles from "./surface.module.css";

export type SurfaceProps = HTMLAttributes<HTMLElement> & {
  as?: Extract<ElementType, "article" | "aside" | "div" | "section">;
  elevated?: boolean;
  tone?: "default" | "subtle" | "inverse";
};

export function Surface({
  as: Component = "div",
  className,
  elevated = false,
  tone = "default",
  ...props
}: SurfaceProps) {
  return (
    <Component
      {...props}
      className={cx(styles.surface, styles[tone], elevated && styles.elevated, className)}
      data-ui="surface"
    />
  );
}
