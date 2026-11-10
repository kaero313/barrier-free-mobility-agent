import { InfoIcon, ShieldAlertIcon } from "lucide-react";

import { Disclosure } from "@/components/disclosure";
import { StatusIcon, type StatusTone } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import type { ConversationRunResponse } from "@/contracts/agent";
import { cn } from "@/lib/utils";

import { CanonicalMarkdown } from "./canonical-markdown";
import { ConversationContextSummary } from "./conversation-context-summary";
import { conditionText, facilityTypeLabels } from "./labels";

type Presentation = NonNullable<ConversationRunResponse["presentation"]>;
type FacilityPresentation = Extract<Presentation, { kind: "facility" }>;
type FacilityItem = FacilityPresentation["facility"]["items"][number];
type EvidenceSource = Presentation["evidence_sources"][number];

type ResultCopy = Readonly<{ title: string; tone: StatusTone }>;

const statusCopy = {
  PARTIAL: { title: "일부 정보만 확인되었습니다", tone: "partial" },
  FAILED: { title: "현재 확인하지 못했습니다", tone: "danger" },
  NEEDS_CLARIFICATION: { title: "추가 정보가 필요합니다", tone: "info" },
} as const satisfies Record<Exclude<ConversationRunResponse["status"], "SUCCESS">, ResultCopy>;

// SUCCESS 는 조회를 끝냈다는 뜻일 뿐 이동 가능이 아니다. 완료 표시 대신 위험도에 맞는 제목을 쓰고,
// 위험도를 주지 않는 결과(시설 질문 등)는 중립 제목으로 둔다.
const successCopyByRisk = {
  LOW: { title: "확인된 범위에서 큰 위험 요소는 없습니다", tone: "success" },
  CAUTION: { title: "출발 전에 확인할 점이 있습니다", tone: "partial" },
  HIGH: { title: "이동에 어려움이 예상됩니다", tone: "danger" },
  UNKNOWN: { title: "이동 가능 여부를 판단하지 못했습니다", tone: "unknown" },
} as const satisfies Record<
  NonNullable<ConversationRunResponse["evidence"]["risk_level"]>,
  ResultCopy
>;

// 결과 제목은 답변 첫 줄(큰 제목) 위에 붙는 상태 표시다. 색은 옅은 배경·테두리와 아이콘에만 쓰고
// 글자는 본문 색으로 둔다.
const TONE_SURFACES: Record<StatusTone, string> = {
  success: "border-success/30 bg-success/10",
  partial: "border-warning/35 bg-warning/10",
  danger: "border-destructive/30 bg-destructive/10",
  info: "border-info/30 bg-info/10",
  unknown: "border-border bg-muted",
};

// 위험도로 제목을 정한 결과는 제목이 이미 위험도를 말하므로 근거 영역에서 다시 쓰지 않는다.
function titleStatesRisk(run: ConversationRunResponse): boolean {
  return run.run_status !== "failed" && run.status === "SUCCESS" && run.evidence.risk_level !== null;
}

function resultCopy(run: ConversationRunResponse): ResultCopy {
  if (run.run_status === "failed") {
    return { title: "조회가 중단되었습니다", tone: "danger" };
  }
  if (run.status !== "SUCCESS") {
    return statusCopy[run.status];
  }
  const risk = run.evidence.risk_level;
  return risk ? successCopyByRisk[risk] : { title: "조회를 마쳤습니다", tone: "info" };
}

const riskLabels = {
  LOW: "낮음",
  CAUTION: "주의",
  HIGH: "높음",
  UNKNOWN: "확인되지 않음",
} as const;

const confidenceLabels = {
  HIGH: "높음",
  MEDIUM: "보통",
  LOW: "낮음",
} as const;

const facilityStateLabels = {
  AVAILABLE: "이용 가능",
  MIXED: "상태 혼재",
  MAINTENANCE: "점검 중",
  UNAVAILABLE: "이용 불가",
  NOT_CONFIRMED: "확인되지 않음",
  UNSUPPORTED: "제공 범위 아님",
  UNKNOWN: "상태 미확인",
} as const;

const facilityStatusLabels = {
  AVAILABLE: "이용 가능",
  UNAVAILABLE: "이용 불가",
  MAINTENANCE: "점검 중",
  UNKNOWN: "상태 미확인",
} as const;

function formatCheckedAt(value: string | null): string {
  if (value === null) {
    return "확인 시각이 제공되지 않았습니다";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "확인 시각 형식을 읽을 수 없습니다";
  }
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function CheckedAt({ value }: { value: string | null }) {
  if (value === null || Number.isNaN(new Date(value).getTime())) {
    return <>{formatCheckedAt(value)}</>;
  }
  return <time dateTime={value}>{formatCheckedAt(value)}</time>;
}

function laterCheckedAt(first: string | null, second: string | null): string | null {
  if (first === null) return second;
  if (second === null) return first;
  return new Date(second).getTime() > new Date(first).getTime() ? second : first;
}

// 한 응답 안에서 같은 출처를 다시 읽은 캐시 항목은 사용자에게 별개 출처가 아니므로 이름 기준으로 합친다.
function uniqueSources(sources: readonly EvidenceSource[]) {
  const merged = new Map<string, { success: boolean; checkedAt: string | null }>();
  for (const source of sources) {
    const current = merged.get(source.display_name);
    merged.set(source.display_name, {
      success: (current?.success ?? false) || source.success,
      checkedAt: laterCheckedAt(current?.checkedAt ?? null, source.checked_at),
    });
  }
  return Array.from(merged, ([displayName, value]) => ({ displayName, ...value }));
}

function SafetyNotice({ notice }: { notice: string }) {
  return (
    // 결과와 함께 읽는 안내라 알림(role=alert)으로 다시 읽히지 않게 한다.
    <Alert role="note">
      <ShieldAlertIcon aria-hidden="true" />
      <AlertTitle>안전 안내</AlertTitle>
      <AlertDescription className="text-sm leading-6 text-foreground">{notice}</AlertDescription>
    </Alert>
  );
}

function FacilityItemView({ item }: { item: FacilityItem }) {
  return (
    <li className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <strong className="font-semibold">{facilityTypeLabels[item.facility_type]}</strong>
        <Badge variant="outline" className="h-6 px-2.5 text-sm">
          {facilityStateLabels[item.answer_state]}
        </Badge>
      </div>
      {item.facilities.length > 0 ? (
        <ul className="flex flex-col gap-3 text-sm leading-6">
          {item.facilities.map((facility, index) => (
            <li
              key={`${facility.station_name}-${facility.facility_type}-${index}`}
              className="border-l-2 pl-3"
            >
              <p>
                <strong className="font-medium">{facilityStatusLabels[facility.status]}</strong>
                {facility.location_description ? ` · ${facility.location_description}` : ""}
              </p>
              <p className="text-muted-foreground">
                {facility.line ? `${facility.line} · ` : ""}
                개찰구 {facility.inside_gate === null ? "위치 미확인" : facility.inside_gate ? "안쪽" : "바깥쪽"}
                {facility.open_time ? ` · 운영 ${facility.open_time}` : ""}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">상세 시설 목록은 제공되지 않았습니다.</p>
      )}
      {item.truncated ? (
        <p className="text-sm text-muted-foreground">
          전체 {item.total_count}개 중 {item.facilities.length}개 표시
        </p>
      ) : null}
    </li>
  );
}

function FacilityPresentationView({
  presentation,
  showStation,
}: {
  presentation: FacilityPresentation;
  showStation: boolean;
}) {
  return (
    <section className="flex flex-col gap-4" aria-labelledby="facility-presentation-title">
      <h3 id="facility-presentation-title" className="flex flex-wrap items-center gap-2 text-base font-semibold">
        <span>확인된 시설 정보</span>
        {/* 조회 역은 보통 위의 현재 이동 조건 카드가 보여 준다. 그 카드가 없을 때만 여기서 밝힌다. */}
        {showStation ? (
          <>
            <span className="text-muted-foreground">·</span>
            <span>{presentation.facility.station_name}</span>
            {presentation.facility.line ? (
              <Badge variant="secondary" className="h-6 px-2.5 text-sm">
                {presentation.facility.line}
              </Badge>
            ) : null}
          </>
        ) : null}
      </h3>
      {presentation.facility.items.length > 0 ? (
        <ul className="flex flex-col gap-3">
          {presentation.facility.items.map((item) => (
            <FacilityItemView key={item.facility_type} item={item} />
          ))}
        </ul>
      ) : (
        <p>시설 상세 정보가 제공되지 않았습니다.</p>
      )}
    </section>
  );
}

function DetailList({ title, items }: { title: string; items: readonly string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <h4 className="font-semibold">{title}</h4>
      <ul className="ml-5 list-disc space-y-1">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function Evidence({ run }: { run: ConversationRunResponse }) {
  const evidence = run.evidence;
  const presentation = run.presentation;
  const confidenceReasons = presentation?.kind === "trip" ? presentation.confidence_reasons : [];
  // 같은 문장이 제한 사항과 미확인 항목에 함께 오면 한 번만 보여 준다.
  const unverifiedParts = evidence.unverified_parts.filter(
    (part) => !evidence.limitations.includes(part),
  );
  // 경로를 찾지 못한 답변처럼 본문이 부분 확인 정보를 이미 그대로 담으면 상세에서 반복하지 않는다.
  const partialInfo = run.available_partial_info.filter((info) => !run.user_message.includes(info));
  // 판단하지 못한 결과는 제목이 이미 그렇게 말하므로 위험도·자료 신뢰도를 늘어놓지 않는다.
  const judged = run.status !== "FAILED";
  const riskLevel = judged && !titleStatesRisk(run) ? evidence.risk_level : null;
  const confidenceLevel = judged ? evidence.confidence_level : null;
  // MCP 답변 본문은 출처별 기준 시각과 제한 사항을 항상 "기준 시각·주의사항"에 담는다(MCP 답변 규칙).
  // 그런 답변에서는 같은 내용을 접힌 상세에 다시 늘어놓지 않고, Agent가 만든 답변에서만 보여 준다.
  const answerCarriesBasis = run.source === "mcp";
  const sources = answerCarriesBasis ? [] : uniqueSources(presentation?.evidence_sources ?? []);
  const facilityContext = run.context.facility;
  const conditionProvenance = [
    ...run.context.mobility_conditions.map(
      (condition) => `${conditionText(condition)} · ${condition.provenance_label}`,
    ),
    ...(facilityContext?.target && facilityContext.target_provenance_label
      ? [`조회 역 ${facilityContext.target.label} · ${facilityContext.target_provenance_label}`]
      : []),
    ...(facilityContext && facilityContext.requested_types.length > 0 && facilityContext.requested_types_provenance_label
      ? [
          `확인 시설 ${facilityContext.requested_types.map((type) => facilityTypeLabels[type]).join(", ")} · ${facilityContext.requested_types_provenance_label}`,
        ]
      : []),
  ];

  return (
    <aside className="min-w-0" aria-labelledby="evidence-title">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="evidence-title" className="text-base font-semibold">
              판단 근거와 정보 한계
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {/* 시설 질문처럼 위험·신뢰 판단이 없는 결과에는 빈 값을 늘어놓지 않는다. */}
          {riskLevel || confidenceLevel ? (
            // 항목 이름(위험도·자료 신뢰도)만으로 뜻이 통하므로 화면에 소제목을 따로 두지 않는다.
            <section aria-label="판단 수준">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-lg bg-muted/50 p-3 text-sm">
                {riskLevel ? (
                  <>
                    <dt className="text-muted-foreground">위험도</dt>
                    <dd className="font-medium">{riskLabels[riskLevel]}</dd>
                  </>
                ) : null}
                {confidenceLevel ? (
                  <>
                    <dt className="text-muted-foreground">자료 신뢰도</dt>
                    <dd className="flex flex-col gap-1">
                      <span className="font-medium">{confidenceLabels[confidenceLevel]}</span>
                      {confidenceReasons.map((reason) => (
                        <span key={reason} className="text-muted-foreground">
                          {reason}
                        </span>
                      ))}
                    </dd>
                  </>
                ) : null}
              </dl>
            </section>
          ) : null}
          <Disclosure summary="근거와 제한 사항 자세히 보기">
            <DetailList title="부분적으로 확인된 정보" items={partialInfo} />
            {answerCarriesBasis ? null : (
              <DetailList title="제한 사항" items={evidence.limitations} />
            )}
            <DetailList title="확인하지 못한 부분" items={unverifiedParts} />
            <DetailList
              title="확인에 실패한 출처"
              items={evidence.failed_sources.map(
                (source) =>
                  `${source.display_name} · ${source.recoverable ? "다시 확인 가능" : "현재 복구 불가"}`,
              )}
            />
            <DetailList title="판단에 사용한 조건" items={conditionProvenance} />
            {sources.length > 0 ? (
              <div className="flex flex-col gap-1">
                <h4 className="font-semibold">확인 출처</h4>
                <ul className="ml-5 list-disc space-y-1">
                  {sources.map((source) => (
                    <li key={source.displayName}>
                      {source.displayName} · {source.success ? "확인 성공" : "확인 실패"} ·{" "}
                      <CheckedAt value={source.checkedAt} />
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {answerCarriesBasis ? null : (
              <p className="text-muted-foreground">
                마지막 확인: <CheckedAt value={evidence.last_checked_at} />
              </p>
            )}
            <div className="flex flex-col gap-1 border-t pt-3 first:border-t-0 first:pt-0">
              <h4 className="font-semibold">문제 신고용 정보</h4>
              <p className="text-muted-foreground">
                요청 ID: <code className="font-mono text-xs break-all">{run.request_id}</code>
              </p>
            </div>
          </Disclosure>
        </CardContent>
      </Card>
    </aside>
  );
}

export function ResultSummary({ run }: { run: ConversationRunResponse }) {
  const isExecutionFailure = run.run_status === "failed";
  const copy = resultCopy(run);
  // 경로 결과는 따로 그릴 구조화 정보가 없다. 출발·도착은 이동 조건 카드가, 환승 경로는 답변의
  // "환승 정보"가 보여 주므로 경로 요약 띠를 두지 않는다. 판단에 실패한 경로 결과에는 참고 안내와
  // 안전 안내 상자도 붙이지 않는다. 시설 결과는 실패해도 시설 목록이 참고 정보로 남는다.
  const presentation =
    run.status === "FAILED" && run.presentation?.kind === "trip" ? null : run.presentation;
  // MCP 시설 답변 본문은 시설·위치·상태 표를 이미 담는다(MCP 답변 규칙). 같은 목록을 아래에
  // 다시 그리지 않고, 본문에 그 표가 없는 Agent 답변에서만 구조화 목록을 보여 준다.
  const facilityPresentation =
    presentation?.kind === "facility" && run.source !== "mcp" ? presentation : null;
  // 대부분의 답변은 안전 안내 문장을 본문에 이미 담고 있다. 본문에 없을 때만 따로 보여 준다.
  const safetyNotice = presentation?.safety_notice.trim() ?? "";
  const showSafetyNotice = safetyNotice !== "" && !run.user_message.includes(safetyNotice);

  return (
    // 결과 본문 2 : 이동 조건·근거 1(12열 기준 8:4)로 나누고, 좁은 화면에서는 같은 DOM 순서로 한 열이 된다.
    // 역별 표(최소 36rem)가 본문 칸에 다 들어가는 xl부터 나눈다. 그보다 좁으면 "추가 확인" 열이
    // 가로 스크롤 뒤로 숨는다.
    <article
      className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] xl:items-start xl:gap-6"
      aria-label="이동 접근성 확인 결과"
      data-result-state={isExecutionFailure ? "execution-failed" : run.status.toLowerCase()}
      data-ui="result-summary"
    >
      <section className="min-w-0" aria-labelledby="result-title">
        <Card className="gap-5">
          <CardHeader>
            <CardTitle>
              <h2
                id="result-title"
                tabIndex={-1}
                className={cn(
                  "inline-flex w-fit items-center gap-2 rounded-full border px-3 py-1 text-sm font-semibold",
                  TONE_SURFACES[copy.tone],
                )}
              >
                <StatusIcon tone={copy.tone} className="size-4" />
                {copy.title}
              </h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-6 text-base leading-7">
            <CanonicalMarkdown message={run.user_message} />

            {run.status === "FAILED" && facilityPresentation !== null ? (
              <Alert role="note">
                <InfoIcon aria-hidden="true" />
                <AlertDescription className="text-sm text-foreground">
                  판단은 완료되지 않았지만 아래 정보는 참고용으로 제공합니다.
                </AlertDescription>
              </Alert>
            ) : null}

            {showSafetyNotice ? <SafetyNotice notice={safetyNotice} /> : null}

            {facilityPresentation !== null ? (
              <>
                <Separator />
                <FacilityPresentationView
                  presentation={facilityPresentation}
                  showStation={run.context.facility?.target == null}
                />
              </>
            ) : null}
          </CardContent>
        </Card>
      </section>

      {/* 확인한 이동 조건은 근거와 함께 오른쪽 칸에 둔다. 답변이 화면 위에서 바로 시작하고, 좁은
          화면에서는 답변 다음에 이어진다. */}
      <div className="flex min-w-0 flex-col gap-4" data-ui="result-aside">
        <ConversationContextSummary context={run.context} size="default" />
        <Evidence run={run} />
      </div>
    </article>
  );
}
