"use client";

import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

import { usePublicStatus } from "./use-public-status";

export function CurrentSupportSummary() {
  const { state } = usePublicStatus();
  const facility =
    state.kind === "loaded"
      ? state.data.features.find((feature) => feature.id === "station_facility")
      : null;
  const trip =
    state.kind === "loaded"
      ? state.data.features.find((feature) => feature.id === "trip_accessibility")
      : null;

  return (
    <section aria-labelledby="current-support-heading">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="current-support-heading" className="text-lg font-semibold">
              현재 확인할 수 있는 정보
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-base leading-7">
          <p aria-atomic="true" aria-live="polite" className="font-medium">
            {state.kind === "checking"
              ? "현재 이용 가능 여부를 확인하고 있습니다."
              : state.kind === "unavailable"
                ? "현재 이용 가능 여부를 확인하지 못했습니다. 서비스 상태에서 다시 확인해 주세요."
                : trip?.status === "available"
                  ? "현재 이동 경로 조회를 시작할 수 있습니다."
                  : state.data.message}
          </p>

          {facility?.status === "available" ? (
            <p className="text-muted-foreground">이 환경에서는 역 시설 상태 질문도 지원합니다.</p>
          ) : null}

          <Link
            className={buttonVariants({ variant: "outline", className: "mt-1 self-start" })}
            href="/status"
          >
            서비스 상태 자세히 보기
            <ArrowRightIcon data-icon="inline-end" aria-hidden="true" />
          </Link>
        </CardContent>
      </Card>
    </section>
  );
}
