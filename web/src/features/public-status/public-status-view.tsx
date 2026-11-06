"use client";

import { RefreshCwIcon, RouteIcon } from "lucide-react";
import Link from "next/link";

import { StatusBadge, type StatusTone } from "@/components/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import type { PublicServiceStatus } from "@/contracts/public-status";

import { usePublicStatus } from "./use-public-status";

const OVERALL_STATUS: Record<
  PublicServiceStatus["status"],
  Readonly<{ label: string; tone: StatusTone }>
> = {
  operational: { label: "이동 경로 조회 가능", tone: "success" },
  degraded: { label: "일부 질문만 이용 가능", tone: "partial" },
  unavailable: { label: "현재 새 조회 이용 불가", tone: "danger" },
  unknown: { label: "현재 상태 미확인", tone: "unknown" },
};

const SERVICE_STATUS = {
  available: { label: "이용 가능", tone: "success" },
  unavailable: { label: "이용 불가", tone: "danger" },
  unknown: { label: "상태 미확인", tone: "unknown" },
} as const satisfies Record<
  PublicServiceStatus["services"][number]["status"],
  Readonly<{ label: string; tone: StatusTone }>
>;

const FEATURE_STATUS = {
  available: { label: "지원됨", tone: "success" },
  not_supported: { label: "현재 지원하지 않음", tone: "unknown" },
  temporarily_unavailable: { label: "일시적으로 확인 불가", tone: "partial" },
  unknown: { label: "상태 미확인", tone: "unknown" },
} as const satisfies Record<
  PublicServiceStatus["features"][number]["status"],
  Readonly<{ label: string; tone: StatusTone }>
>;

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium",
  timeStyle: "medium",
});

function CheckedAt({ value }: Readonly<{ value: string | null }>) {
  if (value === null) {
    return <span>확인 시각을 제공받지 못했습니다.</span>;
  }
  return <time dateTime={value}>{DATE_TIME_FORMATTER.format(new Date(value))}</time>;
}

function StatusGroup({
  id,
  rows,
  title,
}: Readonly<{
  id: string;
  rows: ReadonlyArray<Readonly<{ id: string; label: string; status: { label: string; tone: StatusTone } }>>;
  title: string;
}>) {
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-2">
      <h3 id={id} className="text-sm font-semibold">
        {title}
      </h3>
      <dl className="divide-y rounded-lg border">
        {rows.map((row) => (
          <div
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 py-3"
            key={row.id}
          >
            <dt className="text-sm">{row.label}</dt>
            <dd>
              <StatusBadge tone={row.status.tone}>{row.status.label}</StatusBadge>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function PublicStatusView() {
  const { refresh, state } = usePublicStatus();
  const loaded = state.kind === "loaded" ? state.data : null;
  const overall = loaded === null ? null : OVERALL_STATUS[loaded.status];
  const announcement =
    state.kind === "checking"
      ? "서비스 상태를 확인하고 있습니다."
      : state.kind === "unavailable"
        ? "현재 서비스 상태를 확인하지 못했습니다."
        : `${overall?.label ?? "상태 미확인"}. ${state.data.message}`;

  return (
    <section
      aria-busy={state.kind === "checking"}
      aria-labelledby="current-service-status"
      data-state={state.kind}
      data-ui="public-status"
    >
      <p aria-atomic="true" aria-live="polite" className="sr-only">
        {announcement}
      </p>

      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="current-service-status" className="text-lg font-semibold">
              이동 조회 상태
            </h2>
          </CardTitle>
          {overall === null ? null : (
            <CardAction>
              <StatusBadge tone={overall.tone}>{overall.label}</StatusBadge>
            </CardAction>
          )}
        </CardHeader>

        <CardContent className="flex flex-col gap-6">
          {state.kind === "checking" ? (
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge tone="info">확인 중</StatusBadge>
              <p className="text-muted-foreground">지금 이동 조회를 시작할 수 있는지 확인하고 있습니다.</p>
            </div>
          ) : null}

          {state.kind === "unavailable" ? (
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge tone="unknown">상태 미확인</StatusBadge>
              <p className="text-muted-foreground">
                상태 조회에 실패했습니다. 이전 결과를 현재 상태로 추정하지 않습니다.
              </p>
            </div>
          ) : null}

          {loaded === null ? null : (
            <>
              <p className="text-base">{loaded.message}</p>
              <div className="grid gap-6 md:grid-cols-2">
                <StatusGroup
                  id="service-status-heading"
                  title="연결 상태"
                  rows={loaded.services.map((service) => ({
                    id: service.id,
                    label: service.label,
                    status: SERVICE_STATUS[service.status],
                  }))}
                />
                <StatusGroup
                  id="feature-status-heading"
                  title="지원 범위"
                  rows={loaded.features.map((feature) => ({
                    id: feature.id,
                    label: feature.label,
                    status: FEATURE_STATUS[feature.status],
                  }))}
                />
              </div>
              <p className="text-sm text-muted-foreground">
                <strong className="font-medium text-foreground">마지막 확인:</strong>{" "}
                <CheckedAt value={loaded.checked_at} />
              </p>
            </>
          )}
        </CardContent>

        <CardFooter className="flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={state.kind === "checking"}
            onClick={refresh}
          >
            {state.kind === "checking" ? (
              <Spinner data-icon="inline-start" aria-hidden="true" />
            ) : (
              <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
            )}
            {state.kind === "checking" ? "확인 중…" : "상태 새로 확인"}
          </Button>
          <Link className={buttonVariants({ variant: "ghost" })} href="/assistant">
            <RouteIcon data-icon="inline-start" aria-hidden="true" />
            이동 조회 화면으로 이동
          </Link>
        </CardFooter>
      </Card>
    </section>
  );
}
