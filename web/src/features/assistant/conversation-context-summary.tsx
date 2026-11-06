import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ConversationContextView } from "@/contracts/agent";

import { conditionText, facilityTypeLabels } from "./labels";

type Location = NonNullable<ConversationContextView["origin"]>;

// 출발·도착은 점과 선으로 이은 짧은 경로로 보여 준다. 점은 장식이라 목록 구조(dl)에 요소를
// 더하지 않고 의사 요소로 그린다. 선은 다음 역의 점 위쪽(간격 0.75rem + 점 위치 0.3125rem)까지 잇는다.
const ROUTE_POINT = [
  "relative flex min-w-0 flex-col gap-0.5",
  "before:absolute before:top-1.25 before:-left-5 before:size-2.5 before:rounded-full before:border-2 before:border-foreground before:bg-card",
  "last:before:bg-foreground",
  "not-last:after:absolute not-last:after:top-3.75 not-last:after:-bottom-4.25 not-last:after:-left-3.75 not-last:after:w-px not-last:after:-translate-x-1/2 not-last:after:bg-border",
].join(" ");

function LocationValue({ label, location }: { label: string; location: Location }) {
  return (
    <div className={ROUTE_POINT}>
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="flex flex-wrap items-center gap-2">
        <strong className="font-semibold">{location.label}</strong>
        {/* 확인된 역은 표시하지 않고, 아직 확정되지 않은 예외만 알린다. */}
        {location.status === "resolved" ? null : <Badge variant="outline">확인 전</Badge>}
      </dd>
    </div>
  );
}

// 조건의 출처(provenance)는 결과의 근거 상세에서 보여 주고 여기서는 조건 자체만 둔다.
export function ConversationContextSummary({
  context,
  size = "sm",
}: {
  context: ConversationContextView;
  size?: "default" | "sm";
}) {
  const hasTrip = context.origin !== null || context.destination !== null;
  const hasMobility = context.mobility_conditions.length > 0;
  const hasFacility =
    context.facility !== null &&
    (context.facility.target !== null || context.facility.requested_types.length > 0);

  if (!hasTrip && !hasMobility && !hasFacility) {
    return null;
  }

  return (
    <section aria-labelledby="conversation-context-title">
      <Card size={size}>
        <CardHeader>
          <CardTitle>
            <h2 id="conversation-context-title" className="text-base font-semibold">
              확인한 이동 조건
            </h2>
          </CardTitle>
        </CardHeader>
        {/* 결과 화면에서는 좁은 오른쪽 칸에 놓이므로 화면 너비 대신 카드 너비로 열 수를 정한다. */}
        <CardContent className="@container flex flex-col gap-4">
          {hasTrip ? (
            <dl className="flex flex-col gap-3 pl-5">
              {context.origin ? <LocationValue label="출발" location={context.origin} /> : null}
              {context.destination ? (
                <LocationValue label="도착" location={context.destination} />
              ) : null}
            </dl>
          ) : null}

          {hasMobility ? (
            <ul className="flex flex-wrap gap-2" aria-label="확인한 이동 조건 목록">
              {context.mobility_conditions.map((condition) => (
                <li key={condition.field}>
                  <Badge variant="secondary" className="h-7 px-3 text-sm">
                    {conditionText(condition)}
                  </Badge>
                </li>
              ))}
            </ul>
          ) : null}

          {hasFacility && context.facility ? (
            <dl className="grid gap-3 @sm:grid-cols-2">
              {context.facility.target ? (
                <div className="flex flex-col gap-1">
                  <dt className="text-sm text-muted-foreground">조회 역</dt>
                  <dd className="font-semibold">{context.facility.target.label}</dd>
                </div>
              ) : null}
              {context.facility.requested_types.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <dt className="text-sm text-muted-foreground">확인 시설</dt>
                  <dd className="font-semibold">
                    {context.facility.requested_types
                      .map((facilityType) => facilityTypeLabels[facilityType])
                      .join(", ")}
                  </dd>
                </div>
              ) : null}
            </dl>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
