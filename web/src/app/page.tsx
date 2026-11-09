import { BookOpenIcon, CircleHelpIcon, EyeIcon, ListChecksIcon, RouteIcon } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { AppShell } from "@/components/shell/app-shell";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CurrentSupportSummary } from "@/features/public-status";

export const metadata: Metadata = {
  description: "확인된 정보와 한계를 바탕으로 교통약자의 이동 경로 접근성을 안내합니다.",
};

const PRINCIPLES = [
  {
    icon: CircleHelpIcon,
    title: "부족한 조건을 확인합니다",
    description: "역이나 이동 조건이 분명하지 않으면 임의로 정하지 않고 사용자에게 다시 묻습니다.",
  },
  {
    icon: EyeIcon,
    title: "미확인 정보를 숨기지 않습니다",
    description: "일부 정보만 확인됐거나 조회하지 못한 항목을 성공 결과와 구분해 표시합니다.",
  },
  {
    icon: ListChecksIcon,
    title: "근거와 확인 시각을 제공합니다",
    description: "공개할 수 있는 정보 출처, 제한 사항과 확인 시각을 결과와 함께 제공합니다.",
  },
] as const;

export default function HomePage() {
  return (
    <AppShell currentPath="/">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-10">
        <section aria-labelledby="product-entry-title" className="flex flex-col gap-4 pt-2 sm:pt-6">
          <h1
            id="product-entry-title"
            className="max-w-3xl text-3xl font-semibold tracking-tight text-balance sm:text-4xl"
          >
            교통약자의 이동 가능성을 확인된 정보와 한계를 함께 안내합니다.
          </h1>
          <p className="max-w-2xl text-lg leading-8 text-muted-foreground">
            출발역, 도착역과 필요한 이동 조건을 자연어로 질문하세요. 확인된 경로 정보와 주의할
            점, 확인하지 못한 항목을 구분해 보여드립니다.
          </p>
          <div className="mt-2 flex flex-wrap gap-3">
            <Link className={buttonVariants({ size: "lg" })} href="/assistant">
              <RouteIcon data-icon="inline-start" aria-hidden="true" />
              이동 조회 시작
            </Link>
            <Link className={buttonVariants({ size: "lg", variant: "outline" })} href="/how-it-works">
              <BookOpenIcon data-icon="inline-start" aria-hidden="true" />
              작동 방식 확인
            </Link>
          </div>
        </section>

        <section aria-labelledby="safety-principles-title" className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <h2 id="safety-principles-title" className="text-xl font-semibold tracking-tight">
              안전한 안내 원칙
            </h2>
            <p className="text-muted-foreground">
              확신을 과장하기보다 사용자가 판단에 필요한 근거와 한계를 함께 확인하도록 돕습니다.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {PRINCIPLES.map(({ description, icon: Icon, title }) => (
              <article key={title}>
                <Card className="h-full">
                  <CardHeader className="gap-2">
                    <span
                      aria-hidden="true"
                      className="mb-1 flex size-10 items-center justify-center rounded-lg bg-muted"
                    >
                      <Icon className="size-5" />
                    </span>
                    <CardTitle>
                      <h3 className="text-base font-semibold">{title}</h3>
                    </CardTitle>
                    <CardDescription className="leading-6">{description}</CardDescription>
                  </CardHeader>
                </Card>
              </article>
            ))}
          </div>
        </section>

        <CurrentSupportSummary />
      </div>
    </AppShell>
  );
}
