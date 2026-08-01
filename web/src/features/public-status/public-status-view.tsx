"use client";

import { StatusBadge } from "@/components/primitives/status-badge";
import type { PublicServiceStatus } from "@/contracts/public-status";
import type { StatusTone } from "@/theme/tokens";

import styles from "./public-status.module.css";
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

function StatusRows({ status }: Readonly<{ status: PublicServiceStatus }>) {
  return (
    <div className={styles.statusColumns}>
      <section aria-labelledby="service-status-heading" className={styles.statusGroup}>
        <h3 id="service-status-heading">연결 상태</h3>
        <dl className={styles.statusList}>
          {status.services.map((service) => {
            const presentation = SERVICE_STATUS[service.status];
            return (
              <div className={styles.statusRow} key={service.id}>
                <dt>{service.label}</dt>
                <dd>
                  <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
                </dd>
              </div>
            );
          })}
        </dl>
      </section>

      <section aria-labelledby="feature-status-heading" className={styles.statusGroup}>
        <h3 id="feature-status-heading">지원 범위</h3>
        <dl className={styles.statusList}>
          {status.features.map((feature) => {
            const presentation = FEATURE_STATUS[feature.status];
            return (
              <div className={styles.statusRow} key={feature.id}>
                <dt>{feature.label}</dt>
                <dd>
                  <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
                </dd>
              </div>
            );
          })}
        </dl>
      </section>
    </div>
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
      className={styles.statusPanel}
      data-state={state.kind}
      data-ui="public-status"
    >
      <p aria-atomic="true" aria-live="polite" className="visually-hidden">
        {announcement}
      </p>

      <div className={styles.statusHeader}>
        <div>
          <p className="eyebrow">현재 이용 상태</p>
          <h2 id="current-service-status">이동 조회 상태</h2>
        </div>
        {overall === null ? null : <StatusBadge tone={overall.tone}>{overall.label}</StatusBadge>}
      </div>

      {state.kind === "checking" ? (
        <div className={styles.stateMessage}>
          <StatusBadge tone="info">확인 중</StatusBadge>
          <p>지금 이동 조회를 시작할 수 있는지 확인하고 있습니다.</p>
        </div>
      ) : null}

      {state.kind === "unavailable" ? (
        <div className={styles.stateMessage}>
          <StatusBadge tone="unknown">상태 미확인</StatusBadge>
          <p>상태 조회에 실패했습니다. 이전 결과를 현재 상태로 추정하지 않습니다.</p>
        </div>
      ) : null}

      {loaded === null ? null : (
        <>
          <p className={styles.statusMessage}>{loaded.message}</p>
          <StatusRows status={loaded} />
          <p className={styles.checkedAt}>
            <strong>마지막 확인:</strong> <CheckedAt value={loaded.checked_at} />
          </p>
        </>
      )}

      <div className={styles.actions}>
        <button disabled={state.kind === "checking"} onClick={refresh} type="button">
          {state.kind === "checking" ? "확인 중…" : "상태 새로 확인"}
        </button>
        <a href="/assistant">이동 조회 화면으로 이동</a>
      </div>
    </section>
  );
}
