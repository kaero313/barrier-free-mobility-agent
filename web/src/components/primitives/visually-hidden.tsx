import type { ComponentPropsWithoutRef } from "react";

import { cx } from "@/lib/class-names";

export function VisuallyHidden({ className, ...props }: ComponentPropsWithoutRef<"span">) {
  return <span {...props} className={cx("visually-hidden", className)} />;
}
