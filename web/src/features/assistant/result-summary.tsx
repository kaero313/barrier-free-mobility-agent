import type { ConversationRunResponse } from "@/contracts/agent";

import { CanonicalMarkdown } from "./canonical-markdown";
import styles from "./assistant-workspace.module.css";

type Presentation = NonNullable<ConversationRunResponse["presentation"]>;
type FacilityPresentation = Extract<Presentation, { kind: "facility" }>;
type FacilityItem = FacilityPresentation["facility"]["items"][number];

const statusCopy = {
  SUCCESS: { title: "확인이 완료되었습니다", icon: "✓", tone: "success" },
  PARTIAL: { title: "일부 정보만 확인되었습니다", icon: "!", tone: "partial" },
  FAILED: { title: "현재 확인하지 못했습니다", icon: "×", tone: "danger" },
  NEEDS_CLARIFICATION: { title: "추가 정보가 필요합니다", icon: "i", tone: "info" },
} as const;

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

const facilityTypeLabels = {
  ELEVATOR: "엘리베이터",
  ESCALATOR: "에스컬레이터",
  WHEELCHAIR_LIFT: "휠체어리프트",
  ACCESSIBLE_RESTROOM: "장애인 화장실",
  RESTROOM: "화장실",
  WHEELCHAIR_CHARGER: "휠체어 충전기",
  UNKNOWN: "시설 유형 미확인",
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

function StatusBadge({ run }: { run: ConversationRunResponse }) {
  if (run.run_status === "failed") {
    return (
      <span className={`${styles.statusBadge} ${styles.danger}`}>
        <span aria-hidden="true">×</span> 조회 중단
      </span>
    );
  }
  const copy = statusCopy[run.status];
  return (
    <span className={`${styles.statusBadge} ${styles[copy.tone]}`}>
      <span aria-hidden="true">{copy.icon}</span> {copy.title}
    </span>
  );
}

function TripPresentation({ presentation }: { presentation: Extract<Presentation, { kind: "trip" }> }) {
  return (
    <section className={styles.resultSection} aria-labelledby="trip-presentation-title">
      <h3 id="trip-presentation-title">확인된 이동 정보</h3>
      <p className={styles.routeHeading}>
        <span>{presentation.trip.origin}</span>
        <span aria-hidden="true">→</span>
        <span>{presentation.trip.destination}</span>
      </p>
      <p>{presentation.trip.route_summary || "경로 요약이 제공되지 않았습니다."}</p>
      <div className={styles.safetyNotice}>
        <strong>안전 안내</strong>
        <p>{presentation.safety_notice || "추가 안전 안내가 제공되지 않았습니다."}</p>
      </div>
      {presentation.confidence_reasons.length > 0 ? (
        <>
          <h4>판단 근거</h4>
          <ul className={styles.compactList}>
            {presentation.confidence_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </>
      ) : null}
      <EvidenceSources sources={presentation.evidence_sources} />
    </section>
  );
}

function FacilityItemView({ item }: { item: FacilityItem }) {
  return (
    <li className={styles.facilityItem}>
      <div className={styles.facilityItemHeader}>
        <strong>{facilityTypeLabels[item.facility_type]}</strong>
        <span>{facilityStateLabels[item.answer_state]}</span>
      </div>
      {item.facilities.length > 0 ? (
        <ul className={styles.facilityDetails}>
          {item.facilities.map((facility, index) => (
            <li key={`${facility.station_name}-${facility.facility_type}-${index}`}>
              <p>
                <strong>{facilityStatusLabels[facility.status]}</strong>
                {facility.location_description ? ` · ${facility.location_description}` : ""}
              </p>
              <p className={styles.secondaryText}>
                {facility.line ? `${facility.line} · ` : ""}
                개찰구 {facility.inside_gate === null ? "위치 미확인" : facility.inside_gate ? "안쪽" : "바깥쪽"}
                {facility.open_time ? ` · 운영 ${facility.open_time}` : ""}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.secondaryText}>상세 시설 목록은 제공되지 않았습니다.</p>
      )}
      {item.truncated ? (
        <p className={styles.secondaryText}>
          전체 {item.total_count}개 중 {item.facilities.length}개 표시
        </p>
      ) : null}
    </li>
  );
}

function FacilityPresentationView({ presentation }: { presentation: FacilityPresentation }) {
  return (
    <section className={styles.resultSection} aria-labelledby="facility-presentation-title">
      <h3 id="facility-presentation-title">확인된 시설 정보</h3>
      <p className={styles.routeHeading}>
        <span>{presentation.facility.station_name}</span>
        {presentation.facility.line ? <span>{presentation.facility.line}</span> : null}
      </p>
      {presentation.facility.items.length > 0 ? (
        <ul className={styles.facilityList}>
          {presentation.facility.items.map((item) => (
            <FacilityItemView key={item.facility_type} item={item} />
          ))}
        </ul>
      ) : (
        <p>시설 상세 정보가 제공되지 않았습니다.</p>
      )}
      <div className={styles.safetyNotice}>
        <strong>안전 안내</strong>
        <p>{presentation.safety_notice || "추가 안전 안내가 제공되지 않았습니다."}</p>
      </div>
      <EvidenceSources sources={presentation.evidence_sources} />
    </section>
  );
}

function EvidenceSources({
  sources,
}: {
  sources: Presentation["evidence_sources"];
}) {
  if (sources.length === 0) {
    return null;
  }
  return (
    <div className={styles.sourceList}>
      <h4>확인 출처</h4>
      <ul className={styles.compactList}>
        {sources.map((source, index) => (
          <li key={`${source.display_name}-${index}`}>
            {source.display_name} · {source.success ? "확인 성공" : "확인 실패"} ·{" "}
            <CheckedAt value={source.checked_at} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function Evidence({ run }: { run: ConversationRunResponse }) {
  const evidence = run.evidence;
  const hasDetails =
    run.available_partial_info.length > 0 ||
    evidence.failed_sources.length > 0 ||
    evidence.limitations.length > 0 ||
    evidence.unverified_parts.length > 0 ||
    evidence.last_checked_at !== null;

  return (
    <aside className={styles.evidencePanel} aria-labelledby="evidence-title">
      <div className={styles.evidenceHeader}>
        <p className={styles.eyebrow}>공개된 조회 근거</p>
        <h2 id="evidence-title">판단 근거와 정보 한계</h2>
      </div>

      <section className={styles.evidenceBlock} aria-labelledby="evidence-level-title">
        <h3 id="evidence-level-title">판단 수준</h3>
        <dl className={styles.definitionGrid}>
          <dt>위험</dt>
          <dd>{evidence.risk_level ? riskLabels[evidence.risk_level] : "제공되지 않음"}</dd>
          <dt>신뢰</dt>
          <dd>{evidence.confidence_level ? confidenceLabels[evidence.confidence_level] : "제공되지 않음"}</dd>
        </dl>
      </section>
      {hasDetails ? (
        <details className={styles.evidenceDetails}>
          <summary>근거와 제한 사항 자세히 보기</summary>
          {run.available_partial_info.length > 0 ? (
            <EvidenceList title="부분적으로 확인된 정보" items={run.available_partial_info} />
          ) : null}
          {evidence.limitations.length > 0 ? (
            <EvidenceList title="제한 사항" items={evidence.limitations} />
          ) : null}
          {evidence.unverified_parts.length > 0 ? (
            <EvidenceList title="확인하지 못한 부분" items={evidence.unverified_parts} />
          ) : null}
          {evidence.failed_sources.length > 0 ? (
            <div>
              <h4>확인에 실패한 출처</h4>
              <ul className={styles.compactList}>
                {evidence.failed_sources.map((source, index) => (
                  <li key={`${source.source_name}-${index}`}>
                    {source.display_name} · {source.recoverable ? "다시 확인 가능" : "현재 복구 불가"}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <p>
            마지막 확인: <CheckedAt value={evidence.last_checked_at} />
          </p>
        </details>
      ) : null}
      <details className={styles.requestDetails}>
        <summary>문제 신고용 정보</summary>
        <p>요청 ID: <code>{run.request_id}</code></p>
      </details>
    </aside>
  );
}

function EvidenceList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <h4>{title}</h4>
      <ul className={styles.compactList}>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function ResultSummary({ run }: { run: ConversationRunResponse }) {
  const isExecutionFailure = run.run_status === "failed";
  const copy = isExecutionFailure
    ? { title: "조회가 중단되었습니다", tone: "danger" as const }
    : statusCopy[run.status];

  return (
    <article
      className={styles.resultLayout}
      aria-label="이동 접근성 확인 결과"
      data-result-state={isExecutionFailure ? "execution-failed" : run.status.toLowerCase()}
      data-ui="result-summary"
    >
      <section
        className={`${styles.resultCard} ${styles[`card_${copy.tone}`]}`}
        aria-labelledby="result-title"
      >
        <header className={styles.resultHeader}>
          <div>
            <p className={styles.eyebrow}>확인 결과</p>
            <h2 id="result-title" tabIndex={-1}>
              {copy.title}
            </h2>
          </div>
          <StatusBadge run={run} />
        </header>

        <div className={styles.canonicalMessage}>
          <CanonicalMarkdown message={run.user_message} />
        </div>

        {run.status === "FAILED" && run.presentation !== null ? (
          <p className={styles.referenceNotice}>
            판단은 완료되지 않았지만 아래 정보는 참고용으로 제공합니다.
          </p>
        ) : null}

        {run.presentation?.kind === "trip" ? (
          <TripPresentation presentation={run.presentation} />
        ) : null}
        {run.presentation?.kind === "facility" ? (
          <FacilityPresentationView presentation={run.presentation} />
        ) : null}
      </section>

      <Evidence run={run} />
    </article>
  );
}
