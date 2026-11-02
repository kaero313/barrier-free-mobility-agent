import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type PageHeaderProps = Readonly<{
  actions?: ReactNode;
  className?: string;
  description?: ReactNode;
  title: ReactNode;
}>;

export function PageHeader({ actions, className, description, title }: PageHeaderProps) {
  return (
    <header className={cn("flex flex-col gap-2", className)}>
      <h1 className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl">{title}</h1>
      {description ? (
        <p className="max-w-3xl text-base leading-7 text-muted-foreground">{description}</p>
      ) : null}
      {actions ? <div className="mt-2 flex flex-wrap gap-3">{actions}</div> : null}
    </header>
  );
}
