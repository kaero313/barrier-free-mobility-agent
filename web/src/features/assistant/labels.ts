import type { ConversationContextView } from "@/contracts/agent";

type MobilityCondition = ConversationContextView["mobility_conditions"][number];

export const facilityTypeLabels = {
  ELEVATOR: "엘리베이터",
  ESCALATOR: "에스컬레이터",
  WHEELCHAIR_LIFT: "휠체어리프트",
  ACCESSIBLE_RESTROOM: "장애인 화장실",
  RESTROOM: "화장실",
  WHEELCHAIR_CHARGER: "휠체어 충전기",
  UNKNOWN: "시설 유형 미확인",
} as const;

// "계단 이용 가능: 해당 없음"처럼 거짓 값이 '관계없음'으로 읽히지 않도록 조건마다 문장을 정한다.
const CONDITION_TEXT: Partial<Record<MobilityCondition["field"], readonly [yes: string, no: string]>> = {
  wheelchair: ["휠체어 사용", "휠체어 사용 안 함"],
  stroller: ["유모차 이용", "유모차 이용 안 함"],
  cane_or_walker: ["지팡이·보행보조기 사용", "지팡이·보행보조기 사용 안 함"],
  can_use_stairs: ["계단 이용 가능", "계단 이용 불가"],
  can_use_escalator: ["에스컬레이터 이용 가능", "에스컬레이터 이용 불가"],
  need_elevator_only: ["엘리베이터로만 이동", "엘리베이터 외 이동 가능"],
  need_accessible_restroom: ["장애인 화장실 필요", "장애인 화장실 필요 없음"],
  need_wheelchair_charger: ["휠체어 충전기 필요", "휠체어 충전기 필요 없음"],
  avoid_many_transfers: ["환승 적은 경로 선호", "환승 횟수 상관없음"],
};

export function conditionText(condition: MobilityCondition): string {
  const text = CONDITION_TEXT[condition.field];
  if (text) {
    return condition.value ? text[0] : text[1];
  }
  return `${condition.label}: ${condition.value ? "예" : "아니요"}`;
}
