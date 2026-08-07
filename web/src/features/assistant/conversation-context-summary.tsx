import type { ConversationContextView } from "@/contracts/agent";

import styles from "./assistant-workspace.module.css";

type Location = NonNullable<ConversationContextView["origin"]>;

const facilityTypeLabels = {
  ELEVATOR: "엘리베이터",
  ESCALATOR: "에스컬레이터",
  WHEELCHAIR_LIFT: "휠체어리프트",
  ACCESSIBLE_RESTROOM: "장애인 화장실",
  RESTROOM: "화장실",
  WHEELCHAIR_CHARGER: "휠체어 충전기",
  UNKNOWN: "시설 유형 미확인",
} as const;

function LocationValue({ label, location }: { label: string; location: Location }) {
  return (
    <div className={styles.contextLocation}>
      <dt>{label}</dt>
      <dd>
        <strong>{location.label}</strong>
        <span className={styles.contextStatus}>
          {location.status === "resolved" ? "확인됨" : "입력됨"}
        </span>
      </dd>
    </div>
  );
}

export function ConversationContextSummary({
  context,
}: {
  context: ConversationContextView;
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
    <section className={styles.contextSummary} aria-labelledby="conversation-context-title">
      <div className={styles.contextHeading}>
        <p className={styles.eyebrow}>현재 조회 기준</p>
        <h2 id="conversation-context-title">확인한 이동 조건</h2>
      </div>

      {hasTrip ? (
        <dl className={styles.contextRoute}>
          {context.origin ? <LocationValue label="출발" location={context.origin} /> : null}
          {context.destination ? (
            <LocationValue label="도착" location={context.destination} />
          ) : null}
        </dl>
      ) : null}

      {hasMobility ? (
        <ul className={styles.contextConditions} aria-label="확인한 이동 조건 목록">
          {context.mobility_conditions.map((condition) => (
            <li key={condition.field}>
              <span>
                {condition.label}: {condition.value ? "해당" : "해당 없음"}
              </span>
              <small>{condition.provenance_label}</small>
            </li>
          ))}
        </ul>
      ) : null}

      {hasFacility && context.facility ? (
        <div className={styles.facilityContext}>
          {context.facility.target ? (
            <p>
              조회 역 <strong>{context.facility.target.label}</strong>
              {context.facility.target_provenance_label ? (
                <small>{context.facility.target_provenance_label}</small>
              ) : null}
            </p>
          ) : null}
          {context.facility.requested_types.length > 0 ? (
            <p>
              확인 시설{" "}
              <strong>
                {context.facility.requested_types
                  .map((facilityType) => facilityTypeLabels[facilityType])
                  .join(", ")}
              </strong>
              {context.facility.requested_types_provenance_label ? (
                <small>{context.facility.requested_types_provenance_label}</small>
              ) : null}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
