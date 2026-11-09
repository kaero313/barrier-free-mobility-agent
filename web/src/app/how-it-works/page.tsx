import { ActivityIcon, CheckIcon, InfoIcon, RouteIcon } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/page-header";
import { AppShell } from "@/components/shell/app-shell";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export const metadata: Metadata = {
  title: "작동 방식",
  description: "이동 질문을 확인하고 근거와 한계를 포함한 결과를 제공하는 방식을 설명합니다.",
};

const STEPS = [
  {
    title: "이동 질문을 입력합니다",
    description: "출발역, 도착역과 엘리베이터 필요 여부 같은 이동 조건을 자연어로 입력합니다.",
  },
  {
    title: "부족한 조건을 확인합니다",
    description: "역 이름이나 조건이 불분명하면 후보를 제시하거나 추가 질문을 하고 답을 기다립니다.",
  },
  {
    title: "이동 정보를 확인합니다",
    description: "사용자가 확인한 조건을 기준으로 연결된 이동 정보에서 경로와 접근성 정보를 찾습니다.",
  },
  {
    title: "결과와 한계를 함께 보여드립니다",
    description: "확인 결과, 주의 사항, 공개된 근거와 확인하지 못한 항목을 구분해 제공합니다.",
  },
] as const;

const UNCERTAINTY_RULES = [
  "일부 정보만 확인되면 전체 확인과 구분해 안내합니다.",
  "연결된 정보 중 실패하거나 확인하지 못한 항목을 제한 사항으로 표시합니다.",
  "시설과 운행 정보는 바뀔 수 있으므로 출발 전에 운영 기관 정보를 다시 확인해야 합니다.",
] as const;

export default function HowItWorksPage() {
  return (
    <AppShell currentPath="/how-it-works">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-8">
        <PageHeader
          title="이동 정보를 확인하는 방식"
          description="질문에 필요한 조건을 확인하고, 연결된 이동 정보에서 찾은 결과와 정보의 한계를 함께 제공합니다."
        />

        <ol aria-label="이동 정보 확인 단계" className="grid gap-4 sm:grid-cols-2">
          {STEPS.map((step, index) => (
            <li key={step.title}>
              <Card className="h-full">
                <CardHeader className="gap-2">
                  <span
                    aria-hidden="true"
                    className="mb-1 flex size-8 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground"
                  >
                    {index + 1}
                  </span>
                  <CardTitle>
                    <h2 className="text-base font-semibold">{step.title}</h2>
                  </CardTitle>
                  <CardDescription className="leading-6">{step.description}</CardDescription>
                </CardHeader>
              </Card>
            </li>
          ))}
        </ol>

        <section aria-labelledby="uncertainty-title">
          <Card>
            <CardHeader>
              <CardTitle>
                <h2 id="uncertainty-title" className="text-lg font-semibold">
                  불확실성을 다루는 원칙
                </h2>
              </CardTitle>
              <CardDescription>확인하지 못한 내용을 이동 가능으로 추정하지 않습니다.</CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-2 text-base leading-7">
                {UNCERTAINTY_RULES.map((rule) => (
                  <li key={rule} className="flex gap-2">
                    <CheckIcon aria-hidden="true" className="mt-1.5 size-4 shrink-0 text-muted-foreground" />
                    <span>{rule}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </section>

        <section aria-labelledby="data-and-limits-title">
          <Card>
            <CardHeader>
              <CardTitle>
                <h2 id="data-and-limits-title" className="text-lg font-semibold">
                  사용하는 정보와 현재 한계
                </h2>
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4 text-base leading-7">
              <p>
                현재 대화에서 확인된 출발역, 도착역과 이동 조건, 연결된 이동 정보가 제공한 공개 근거와
                확인 시각을 결과에 사용합니다.
              </p>
              <p>
                대체 경로 자동 제안은 아직 제공하지 않습니다. 역 시설 질문은 서비스 상태에서 지원됨으로
                확인되는 환경에서만 이동 조회 화면에 나타납니다.
              </p>
              <Alert role="note">
                <InfoIcon aria-hidden="true" />
                <AlertDescription className="text-sm leading-6 text-foreground">
                  안내 결과는 이동 계획을 돕는 참고 정보입니다. 현장 상황과 시설 운영 상태를 보장하지
                  않습니다.
                </AlertDescription>
              </Alert>
            </CardContent>
            <CardFooter className="flex-wrap gap-2">
              <Link className={buttonVariants()} href="/assistant">
                <RouteIcon data-icon="inline-start" aria-hidden="true" />
                이동 조회 시작
              </Link>
              <Link className={buttonVariants({ variant: "outline" })} href="/status">
                <ActivityIcon data-icon="inline-start" aria-hidden="true" />
                서비스 상태 확인
              </Link>
            </CardFooter>
          </Card>
        </section>
      </div>
    </AppShell>
  );
}
