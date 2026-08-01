import { Card as AstryxCard } from "@astryxdesign/core/Card";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cx } from "@/lib/class-names";

import styles from "./astryx-adapter.module.css";

/**
 * Deliberately narrow Astryx boundary.
 *
 * Astryx Card 0.1.4 always renders a `div`, so this adapter is only valid for
 * presentational grouping. Use the native `Surface` primitive whenever the
 * container needs section/article/aside semantics or an accessible name.
 */
export type AstryxPanelProps = Pick<
  ComponentPropsWithoutRef<"div">,
  "aria-describedby" | "aria-label" | "aria-labelledby" | "children" | "className" | "id"
> & {
  children: ReactNode;
};

export function AstryxPanel({ children, className, ...props }: AstryxPanelProps) {
  return (
    <AstryxCard
      {...props}
      className={cx(styles.panel, className)}
      data-astryx-adapter="panel"
      padding={0}
      variant="transparent"
    >
      {children}
    </AstryxCard>
  );
}
