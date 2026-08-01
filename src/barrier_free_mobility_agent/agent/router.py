from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    FacilityContext,
    PendingClarification,
    TripContext,
)
from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    MobilityFlag,
    SlotSource,
)
from barrier_free_mobility_agent.domain.routing import (
    AgentIntent,
    DecisionSource,
    IntentDecision,
    required_capabilities_for_intent,
)

_ROUTE_PATTERN = re.compile(
    r"(?P<origin>[가-힣A-Za-z0-9·]+?)(?:역?\s*에서|서)\s*"
    r"(?P<destination>[가-힣A-Za-z0-9·]+?)역?\s*까지"
)
_MAX_TRANSFER_PATTERN = re.compile(r"환승\s*(?P<count>\d+)\s*번")
_WHEELCHAIR_NEGATIVE_PATTERN = re.compile(
    r"(?:휠체어\s*없이|"
    r"휠체어(?:를|는|가)?\s*(?:사용|이용)\s*(?:하지|안)|"
    r"휠체어(?:를|는|가)?\s*안\s*(?:써|타)|"
    r"휠체어(?:를|는|가)?\s*아니)"
)
_NEGATIVE_MARKERS = ("못", "불가", "어려", "안 돼", "안돼", "피하고", "없이", "없")
_POSITIVE_MARKERS = ("가능", "할 수", "쓸 수", "이용해도", "괜찮")
_SUPPORTED_FACILITY_TERMS = ("엘리베이터", "승강기", "화장실")
_ALTERNATIVE_TERMS = ("다른 경로", "대안 경로", "우회 경로", "이 경로 말고")


@dataclass(frozen=True, slots=True)
class RoutingResult:
    normalized_input: str
    decision: IntentDecision
    trip: TripContext
    mobility: MobilityContext
    pending: PendingClarification | None


class DeterministicPreRouter:
    def route(
        self,
        text: str,
        *,
        trip: TripContext,
        mobility: MobilityContext,
        facility: FacilityContext | None = None,
        pending: PendingClarification | None = None,
    ) -> RoutingResult:
        normalized = normalize_input(text)
        source = DecisionSource.RESUME if pending is not None else DecisionSource.DETERMINISTIC
        updated_trip = self._extract_trip(normalized, trip)
        updated_mobility = self._extract_mobility(
            normalized,
            mobility,
            source=(
                SlotSource.USER_CONFIRMED
                if source == DecisionSource.RESUME
                else SlotSource.USER_EXPLICIT
            ),
            pending=pending,
        )

        signal_codes: tuple[str, ...]
        has_route_pattern = _ROUTE_PATTERN.search(normalized) is not None
        if pending is not None and pending.intent == AgentIntent.FACILITY_STATUS:
            intent = AgentIntent.FACILITY_STATUS
            signal_codes = ("facility_clarification_resume",)
        elif any(term in normalized for term in _ALTERNATIVE_TERMS):
            intent = AgentIntent.ALTERNATIVE_REQUEST
            signal_codes = ("alternative_term",)
        elif has_route_pattern:
            intent = AgentIntent.TRIP_ACCESSIBILITY
            signal_codes = ("trip_slots",)
        elif any(term in normalized for term in _SUPPORTED_FACILITY_TERMS) and (
            "역" in normalized or (facility is not None and facility.target_station is not None)
        ):
            intent = AgentIntent.FACILITY_STATUS
            signal_codes = (
                ("facility_term", "station_marker")
                if "역" in normalized
                else ("facility_term", "facility_context")
            )
        elif (
            pending is not None and pending.kind != ClarificationKind.UNSUPPORTED_INTENT
        ) or _has_mobility_signal(normalized):
            intent = AgentIntent.TRIP_ACCESSIBILITY
            signal_codes = ("trip_slots",)
        else:
            intent = AgentIntent.UNKNOWN
            signal_codes = ("no_deterministic_match",)

        missing: list[str] = []
        if intent == AgentIntent.TRIP_ACCESSIBILITY:
            if updated_trip.origin_query is None:
                missing.append("origin")
            if updated_trip.destination_query is None:
                missing.append("destination")
            missing.extend(updated_mobility.safety_missing_fields())

        decision = IntentDecision(
            intent=intent,
            source=source,
            confidence=0.99 if intent != AgentIntent.UNKNOWN else 0.0,
            required_capabilities=required_capabilities_for_intent(intent),
            missing_fields=tuple(dict.fromkeys(missing)),
            signal_codes=signal_codes,
        )
        return RoutingResult(
            normalized_input=normalized,
            decision=decision,
            trip=updated_trip,
            mobility=updated_mobility,
            pending=None,
        )

    def _extract_trip(self, text: str, current: TripContext) -> TripContext:
        match = _ROUTE_PATTERN.search(text)
        if match is None:
            return current
        raw_origin = _clean_station_query(match.group("origin"))
        destination = _clean_station_query(match.group("destination"))
        contextual_origin = raw_origin in {"거기", "그곳", "그쪽"}
        origin = (
            current.destination_station or current.destination_query
            if contextual_origin
            else raw_origin
        )
        return TripContext(
            origin_query=origin,
            destination_query=destination,
            origin_station=None if origin != current.origin_query else current.origin_station,
            destination_station=(
                None if destination != current.destination_query else current.destination_station
            ),
            origin_line=(
                current.destination_line
                if contextual_origin
                else current.origin_line
                if origin == current.origin_query
                else None
            ),
            destination_line=(
                current.destination_line if destination == current.destination_query else None
            ),
        )

    def _extract_mobility(
        self,
        text: str,
        current: MobilityContext,
        *,
        source: SlotSource,
        pending: PendingClarification | None,
    ) -> MobilityContext:
        updates: dict[str, MobilityFlag] = {}
        if "휠체어" in text:
            updates["wheelchair"] = MobilityFlag.known(
                _WHEELCHAIR_NEGATIVE_PATTERN.search(text) is None,
                source,
            )
        if "유모차" in text:
            updates["stroller"] = MobilityFlag.known(True, source)
        if "지팡이" in text or "보행기" in text or "워커" in text:
            updates["cane_or_walker"] = MobilityFlag.known(True, source)

        for term, field in (
            ("계단", "can_use_stairs"),
            ("에스컬레이터", "can_use_escalator"),
        ):
            value = _explicit_ability(text, term)
            if value is not None:
                updates[field] = MobilityFlag.known(value, source)

        pending_fields = set(pending.fields if pending is not None else ())
        generic_value = _generic_confirmation(text)
        for field in (
            "wheelchair",
            "stroller",
            "cane_or_walker",
            "can_use_stairs",
            "can_use_escalator",
            "need_elevator_only",
            "need_accessible_restroom",
            "need_wheelchair_charger",
            "avoid_many_transfers",
        ):
            if field in pending_fields and field not in updates and generic_value is not None:
                updates[field] = MobilityFlag.known(generic_value, source)

        if "엘리베이터만" in text or "승강기만" in text:
            updates["need_elevator_only"] = MobilityFlag.known(True, source)
        if "장애인 화장실" in text:
            updates["need_accessible_restroom"] = MobilityFlag.known(True, source)
        if "휠체어 충전" in text:
            updates["need_wheelchair_charger"] = MobilityFlag.known(True, source)
        if "환승 적" in text or "환승을 줄" in text or "환승 없이" in text:
            updates["avoid_many_transfers"] = MobilityFlag.known(True, source)

        max_transfer = _MAX_TRANSFER_PATTERN.search(text)
        result = current.with_flags(**updates) if updates else current
        if max_transfer is not None:
            result = result.model_copy(
                update={"max_transfer_count": int(max_transfer.group("count"))}
            )
        return result


def normalize_input(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return " ".join(normalized.split())


def _clean_station_query(value: str) -> str:
    return value.strip().removesuffix("역")


def _explicit_ability(text: str, term: str) -> bool | None:
    if term not in text:
        return None
    clauses = re.split(r"하지만|그러나|반면|[;/]", text)
    relevant = next((clause for clause in clauses if term in clause), text)
    start = relevant.index(term)
    following_terms = [
        position
        for other in ("계단", "에스컬레이터")
        if other != term and (position := relevant.find(other, start + len(term))) >= 0
    ]
    end = min(following_terms, default=len(relevant))
    ability_phrase = relevant[start:end]
    value = _ability_value(ability_phrase)
    if value is not None:
        return value
    linker = relevant[start + len(term) : end]
    if following_terms and any(marker in linker for marker in ("과", "와", "도", "및", ",")):
        return _ability_value(relevant[end:])
    return None


def _ability_value(phrase: str) -> bool | None:
    if any(marker in phrase for marker in _NEGATIVE_MARKERS):
        return False
    if any(marker in phrase for marker in _POSITIVE_MARKERS):
        return True
    return None


def _generic_confirmation(text: str) -> bool | None:
    if text in {"네", "예", "맞아요", "맞습니다", "그래요"}:
        return True
    if text in {"아니요", "아뇨", "아니에요", "아닙니다"}:
        return False
    if any(marker in text for marker in _NEGATIVE_MARKERS):
        return False
    if any(marker in text for marker in _POSITIVE_MARKERS):
        return True
    return None


def _has_mobility_signal(text: str) -> bool:
    return any(
        term in text
        for term in (
            "휠체어",
            "유모차",
            "지팡이",
            "보행기",
            "워커",
            "계단",
            "엘리베이터만",
            "승강기만",
        )
    )
