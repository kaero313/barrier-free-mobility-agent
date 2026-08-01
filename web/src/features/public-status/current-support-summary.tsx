"use client";

import styles from "./public-status.module.css";
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
    <section aria-labelledby="current-support-heading" className={styles.supportPanel}>
      <div className={styles.supportHeading}>
        <div>
          <p className="eyebrow">지원 범위</p>
          <h2 id="current-support-heading">현재 확인할 수 있는 정보</h2>
        </div>
        <a href="/status">서비스 상태 자세히 보기</a>
      </div>

      <p>
        출발역과 도착역, 이동 조건을 질문하면 이동 경로의 접근성과 주의 사항을 확인합니다.
      </p>

      <p aria-atomic="true" aria-live="polite" className={styles.availabilityMessage}>
        {state.kind === "checking"
          ? "현재 이용 가능 여부를 확인하고 있습니다."
          : state.kind === "unavailable"
            ? "현재 이용 가능 여부를 확인하지 못했습니다. 서비스 상태에서 다시 확인해 주세요."
            : trip?.status === "available"
              ? "현재 이동 경로 조회를 시작할 수 있습니다."
              : state.data.message}
      </p>

      {facility?.status === "available" ? (
        <p className={styles.facilityAvailability}>이 환경에서는 역 시설 상태 질문도 지원합니다.</p>
      ) : null}
    </section>
  );
}
