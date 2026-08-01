from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResponseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class QuestionIntent(StrEnum):
    TRIP_ACCESSIBILITY = "trip_accessibility"
    FACILITY_STATUS = "facility_status"
    ALTERNATIVE_REQUEST = "alternative_request"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "LOW"
    CAUTION = "CAUTION"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ContractStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    UNCHECKED = "unchecked"


class McpCapability(StrEnum):
    TRIP_ACCESSIBILITY = "trip_accessibility"
    FACILITY_QUESTION = "facility_question"


class McpModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AnswerSource(StrEnum):
    MCP = "mcp"
    AGENT = "agent"


class AccessibleRestroomRequirement(StrEnum):
    ANY_ROUTE_STATION = "any_route_station"
    ORIGIN = "origin"
    TRANSFER = "transfer"
    DESTINATION = "destination"
    ORIGIN_OR_DESTINATION = "origin_or_destination"
    ALL_KEY_STATIONS = "all_key_stations"


class FacilityType(StrEnum):
    ELEVATOR = "ELEVATOR"
    ESCALATOR = "ESCALATOR"
    ACCESSIBLE_RESTROOM = "ACCESSIBLE_RESTROOM"
    RESTROOM = "RESTROOM"
    WHEELCHAIR_CHARGER = "WHEELCHAIR_CHARGER"
    UNKNOWN = "UNKNOWN"


class FacilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN = "UNKNOWN"


class FacilityAnswerState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MIXED = "MIXED"
    MAINTENANCE = "MAINTENANCE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class FacilityQuestionKind(StrEnum):
    STATUS = "status"
    LOCATION = "location"
    EXISTENCE = "existence"
    OVERVIEW = "overview"


class AlternativeRequestKind(StrEnum):
    STATION_FACILITY = "station_facility"
    ROUTE = "route"
    CURRENT_ROUTE = "current_route"


class MobilityProfile(McpModel):
    wheelchair: bool
    stroller: bool
    cane_or_walker: bool
    can_use_stairs: bool
    can_use_escalator: bool
    need_elevator_only: bool
    need_accessible_restroom: bool
    accessible_restroom_requirement: AccessibleRestroomRequirement
    need_wheelchair_charger: bool
    avoid_many_transfers: bool
    max_transfer_count: int | None


class Station(McpModel):
    station_id: str | None = None
    station_name: str
    line: str | None = None
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class StationResolutionResult(McpModel):
    query: str
    matched_station: Station | None
    candidates: list[Station] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_message: str | None = None


class ParsedAccessibilityQuestion(McpModel):
    origin: str | None = None
    destination: str | None = None
    target_station: str | None = None
    target_line: str | None = None
    facility_types: list[FacilityType] = Field(default_factory=list)
    facility_question_kind: FacilityQuestionKind | None = None
    alternative_request_kind: AlternativeRequestKind | None = None
    mobility_profile: MobilityProfile
    station_mentions: list[str] = Field(default_factory=list)
    place_mentions: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class FailedSource(McpModel):
    source_name: str
    reason: str
    recoverable: bool = True


class EvidenceSource(McpModel):
    source_name: str
    display_name: str
    source_type: Literal["public_api", "cache", "fixture", "internal"]
    checked_at: datetime | None = None
    success: bool = True
    note: str | None = None


class AccessibleFacility(McpModel):
    facility_id: str | None = None
    station_name: str
    line: str | None = None
    facility_type: FacilityType
    status: FacilityStatus = FacilityStatus.UNKNOWN
    location_description: str | None = None
    inside_gate: bool | None = None
    open_time: str | None = None
    has_emergency_bell: bool | None = None
    raw_status_text: str | None = None


class FacilityQuestionItem(McpModel):
    facility_type: FacilityType
    answer_state: FacilityAnswerState = FacilityAnswerState.UNKNOWN
    facilities: list[AccessibleFacility] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_answer_state(self) -> Self:
        if any(facility.facility_type != self.facility_type for facility in self.facilities):
            raise ValueError("Facility item and child facility types must match.")
        if not self.facilities:
            if self.answer_state not in {
                FacilityAnswerState.NOT_FOUND,
                FacilityAnswerState.UNKNOWN,
            }:
                raise ValueError("Empty facility item must be NOT_FOUND or UNKNOWN.")
            return self
        statuses = {facility.status for facility in self.facilities}
        expected = (
            {
                FacilityStatus.AVAILABLE: FacilityAnswerState.AVAILABLE,
                FacilityStatus.MAINTENANCE: FacilityAnswerState.MAINTENANCE,
                FacilityStatus.UNAVAILABLE: FacilityAnswerState.UNAVAILABLE,
                FacilityStatus.UNKNOWN: FacilityAnswerState.UNKNOWN,
            }[next(iter(statuses))]
            if len(statuses) == 1
            else FacilityAnswerState.MIXED
        )
        if self.answer_state != expected:
            raise ValueError("Facility answer_state must match child facility statuses.")
        return self


class AccessibilityResult(McpModel):
    status: ResponseStatus
    origin: str
    destination: str
    mobility_profile: MobilityProfile
    risk_level: RiskLevel
    risk_score: int = Field(ge=0, le=100)
    route_summary: str
    failed_sources: list[FailedSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence_level: ConfidenceLevel
    confidence_reasons: list[str] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    evidence_sources: list[EvidenceSource] = Field(default_factory=list)
    unverified_parts: list[str] = Field(default_factory=list)
    clarification_needed: bool = False
    questions: list[str] = Field(default_factory=list)
    available_partial_info: list[str] = Field(default_factory=list)
    safety_notice: str
    user_message: str

    @model_validator(mode="after")
    def validate_canonical_message(self) -> Self:
        if not self.user_message.strip():
            raise ValueError("Trip result must include a non-empty user_message.")
        return self


class FacilityQuestionResult(McpModel):
    status: ResponseStatus = ResponseStatus.SUCCESS
    station_name: str
    line: str | None = None
    question_kind: FacilityQuestionKind = FacilityQuestionKind.OVERVIEW
    items: list[FacilityQuestionItem] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    evidence_sources: list[EvidenceSource] = Field(default_factory=list)
    failed_sources: list[FailedSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unverified_parts: list[str] = Field(default_factory=list)
    safety_notice: str
    user_message: str

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        item_types = [item.facility_type for item in self.items]
        if len(item_types) != len(set(item_types)):
            raise ValueError("Facility result cannot repeat a facility type.")
        return self

    @model_validator(mode="after")
    def validate_canonical_message(self) -> Self:
        if not self.user_message.strip():
            raise ValueError("Facility result must include a non-empty user_message.")
        return self


class AccessibilityQuestionResult(McpModel):
    question: str
    status: ResponseStatus
    intent: QuestionIntent
    parsed: ParsedAccessibilityQuestion
    result: AccessibilityResult | None = None
    facility_result: FacilityQuestionResult | None = None
    user_message: str
    clarification_needed: bool = False
    questions: list[str] = Field(default_factory=list)
    available_partial_info: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        if not self.user_message.strip():
            raise ValueError("MCP response must include a non-empty canonical user_message.")
        needs_clarification = self.status == ResponseStatus.NEEDS_CLARIFICATION
        if needs_clarification != self.clarification_needed:
            raise ValueError("Clarification status and clarification_needed must match.")
        if needs_clarification:
            if not self.questions:
                raise ValueError("Clarification responses require questions.")
            if self.result is not None or self.facility_result is not None:
                raise ValueError("Clarification response cannot contain a terminal result.")
        elif self.questions:
            raise ValueError("Terminal response cannot contain clarification questions.")
        if self.result is not None and self.result.status != self.status:
            raise ValueError("Nested trip result status must match the top-level status.")
        if self.facility_result is not None and self.facility_result.status != self.status:
            raise ValueError("Nested facility result status must match the top-level status.")
        if self.result is not None and self.facility_result is not None:
            raise ValueError("Trip and facility results cannot both be present.")
        if (
            self.intent == QuestionIntent.TRIP_ACCESSIBILITY
            and self.status in {ResponseStatus.SUCCESS, ResponseStatus.PARTIAL}
            and self.result is None
        ):
            raise ValueError("Trip result is required for a successful trip response.")
        if (
            self.intent == QuestionIntent.FACILITY_STATUS
            and not needs_clarification
            and self.facility_result is None
        ):
            raise ValueError("Terminal facility response requires a facility result.")
        if (
            self.intent == QuestionIntent.ALTERNATIVE_REQUEST
            and self.status in {ResponseStatus.SUCCESS, ResponseStatus.PARTIAL}
            and self.result is None
            and self.facility_result is None
        ):
            raise ValueError("Alternative response requires one structured nested result.")
        if self.intent == QuestionIntent.FACILITY_STATUS and self.facility_result is not None:
            if not self.facility_result.items:
                raise ValueError("Terminal facility result requires at least one item.")
            parsed_types = set(self.parsed.facility_types)
            item_types = {item.facility_type for item in self.facility_result.items}
            if parsed_types and parsed_types != item_types:
                raise ValueError("Parsed and result facility types must match.")
            if self.parsed.target_station is not None and (
                self.parsed.target_station.removesuffix("역").strip()
                != self.facility_result.station_name.removesuffix("역").strip()
            ):
                raise ValueError("Parsed and result facility stations must match.")
            if (
                self.parsed.target_line is not None
                and self.facility_result.line is not None
                and self.parsed.target_line != self.facility_result.line
            ):
                raise ValueError("Parsed and result facility lines must match.")
        if (
            self.intent == QuestionIntent.TRIP_ACCESSIBILITY
            and self.result is not None
            and self.user_message != self.result.user_message
        ):
            raise ValueError("Trip canonical messages must match exactly.")
        if (
            self.intent == QuestionIntent.FACILITY_STATUS
            and self.facility_result is not None
            and self.user_message != self.facility_result.user_message
        ):
            raise ValueError("Facility canonical messages must match exactly.")
        return self


class FailedSourceCode(StrEnum):
    ROUTE_INFORMATION = "route_information"
    STATION_INFORMATION = "station_information"
    FACILITY_INFORMATION = "facility_information"
    MOBILITY_INFORMATION = "mobility_information"


class FailedSourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: FailedSourceCode
    display_name: str = Field(min_length=1, max_length=80)
    recoverable: bool


_FAILED_SOURCE_PUBLIC_METADATA: dict[str, tuple[FailedSourceCode, str]] = {
    "shortest_route": (FailedSourceCode.ROUTE_INFORMATION, "이동 경로 정보"),
    "station_resolution": (FailedSourceCode.STATION_INFORMATION, "역 정보"),
    "elevator_status": (FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
    "elevator_info": (FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
    "facility_info": (FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
    "restroom": (FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
}


def project_failed_source_summaries(
    failed_sources: list[FailedSource],
) -> list[FailedSourceSummary]:
    """Project MCP-internal source names into stable public product terminology."""

    summaries: list[FailedSourceSummary] = []
    for failed in failed_sources:
        source_code, display_name = _FAILED_SOURCE_PUBLIC_METADATA.get(
            failed.source_name,
            (FailedSourceCode.MOBILITY_INFORMATION, "이동 정보"),
        )
        summaries.append(
            FailedSourceSummary(
                source_name=source_code,
                display_name=display_name,
                recoverable=failed.recoverable,
            )
        )
    return summaries


class ClarificationOption(BaseModel):
    """Agent-owned option safe to expose without leaking an MCP response model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ClarificationControl(StrEnum):
    CHOICE = "choice"
    FREE_TEXT = "free_text"


class ClarificationField(StrEnum):
    ORIGIN = "origin"
    DESTINATION = "destination"
    WHEELCHAIR = "wheelchair"
    STROLLER = "stroller"
    CANE_OR_WALKER = "cane_or_walker"
    CAN_USE_STAIRS = "can_use_stairs"
    CAN_USE_ESCALATOR = "can_use_escalator"
    NEED_ELEVATOR_ONLY = "need_elevator_only"
    NEED_ACCESSIBLE_RESTROOM = "need_accessible_restroom"
    NEED_WHEELCHAIR_CHARGER = "need_wheelchair_charger"
    AVOID_MANY_TRANSFERS = "avoid_many_transfers"
    TARGET_STATION = "target_station"
    TARGET_LINE = "target_line"
    FACILITY_TYPES = "facility_types"
    FACILITY_QUESTION_KIND = "facility_question_kind"
    ACCESSIBLE_RESTROOM_CONFIRMATION = "accessible_restroom_confirmation"
    INTENT = "intent"
    OTHER = "other"


class ClarificationPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1)
    fields: tuple[ClarificationField, ...] = ()


class ClarificationView(BaseModel):
    """One active clarification turn submitted as one natural-language answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    id: str = Field(min_length=1, max_length=200)
    control: ClarificationControl
    answer_mode: Literal["single_text"] = "single_text"
    prompts: tuple[ClarificationPrompt, ...] = Field(min_length=1)
    options: tuple[ClarificationOption, ...] = ()

    @model_validator(mode="after")
    def validate_control_shape(self) -> Self:
        if self.control == ClarificationControl.CHOICE:
            if len(self.prompts) != 1 or not self.options:
                raise ValueError("Choice clarification requires one prompt and options.")
        elif self.options:
            raise ValueError("Free-text clarification cannot expose choice options.")
        if len({prompt.id for prompt in self.prompts}) != len(self.prompts):
            raise ValueError("Clarification prompt IDs must be unique.")
        return self

    @classmethod
    def from_legacy(
        cls,
        *,
        questions: list[str],
        options: list[ClarificationOption],
    ) -> ClarificationView:
        if options and len(questions) != 1:
            raise ValueError("Choice options require exactly one question.")
        return cls(
            id="clarification",
            control=(ClarificationControl.CHOICE if options else ClarificationControl.FREE_TEXT),
            prompts=tuple(
                ClarificationPrompt(
                    id=f"prompt-{index}",
                    question=question,
                    fields=(ClarificationField.OTHER,),
                )
                for index, question in enumerate(questions, start=1)
            ),
            options=tuple(options),
        )


class ContextLocationStatus(StrEnum):
    PROVIDED = "provided"
    RESOLVED = "resolved"


class ContextProvenance(StrEnum):
    USER_PROVIDED = "user_provided"
    USER_CONFIRMED = "user_confirmed"
    PREVIOUS_CONTEXT = "previous_context"


class ConversationLocationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    station_name: str | None = None
    line: str | None = None
    label: str = Field(min_length=1)
    status: ContextLocationStatus

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.status == ContextLocationStatus.RESOLVED and self.station_name is None:
            raise ValueError("Resolved location requires station_name.")
        return self


class MobilityConditionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: ClarificationField
    label: str = Field(min_length=1)
    value: bool
    provenance: ContextProvenance
    provenance_label: str = Field(min_length=1)


class ActiveClarificationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    prompt_ids: tuple[str, ...] = Field(min_length=1)
    fields: tuple[ClarificationField, ...] = Field(min_length=1)


class FacilityContextView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: ConversationLocationView | None = None
    target_provenance: ContextProvenance | None = None
    target_provenance_label: str | None = None
    requested_types: tuple[FacilityType, ...] = ()
    requested_types_provenance: ContextProvenance | None = None
    requested_types_provenance_label: str | None = None
    question_kind: FacilityQuestionKind | None = None

    @model_validator(mode="after")
    def validate_provenance_labels(self) -> Self:
        if (self.target_provenance is None) != (self.target_provenance_label is None):
            raise ValueError("Target provenance and label must appear together.")
        if (self.requested_types_provenance is None) != (
            self.requested_types_provenance_label is None
        ):
            raise ValueError("Facility type provenance and label must appear together.")
        return self


class ConversationContextView(BaseModel):
    """Safe, immutable projection of the context used for one run response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    intent: QuestionIntent = QuestionIntent.UNKNOWN
    origin: ConversationLocationView | None = None
    destination: ConversationLocationView | None = None
    mobility_conditions: tuple[MobilityConditionView, ...] = ()
    facility: FacilityContextView | None = None
    active_clarification: ActiveClarificationSummary | None = None

    @model_validator(mode="after")
    def validate_intent_scope(self) -> Self:
        if self.intent == QuestionIntent.TRIP_ACCESSIBILITY and self.facility is not None:
            raise ValueError("Trip context cannot expose facility context as active.")
        if self.intent == QuestionIntent.FACILITY_STATUS and (
            self.origin is not None or self.destination is not None or self.mobility_conditions
        ):
            raise ValueError("Facility context cannot expose retained trip context as active.")
        if self.intent not in {
            QuestionIntent.TRIP_ACCESSIBILITY,
            QuestionIntent.FACILITY_STATUS,
        } and (
            self.origin is not None
            or self.destination is not None
            or self.mobility_conditions
            or self.facility is not None
        ):
            raise ValueError("Unsupported intent cannot expose an active domain context.")
        return self


class TripSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    destination: str
    route_summary: str


class EvidenceSourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str
    source_type: Literal["public_api", "cache"]
    checked_at: datetime | None = None
    success: bool


class PublicFacilityAnswerState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MIXED = "MIXED"
    MAINTENANCE = "MAINTENANCE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class FacilityDetailView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_name: str
    line: str | None = None
    facility_type: FacilityType
    status: FacilityStatus
    location_description: str | None = None
    inside_gate: bool | None = None
    open_time: str | None = None
    has_emergency_bell: bool | None = None


class FacilityItemView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facility_type: FacilityType
    answer_state: PublicFacilityAnswerState
    facilities: tuple[FacilityDetailView, ...] = ()
    total_count: int = Field(ge=0)
    truncated: bool


class FacilitySummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_name: str
    line: str | None = None
    question_kind: FacilityQuestionKind
    items: tuple[FacilityItemView, ...]


class TripAnswerPresentation(BaseModel):
    """Allowlisted trip fields intended for a document-style answer card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    kind: Literal["trip"] = "trip"
    trip: TripSummaryView
    safety_notice: str
    confidence_reasons: tuple[str, ...] = ()
    evidence_sources: tuple[EvidenceSourceSummary, ...] = ()


class FacilityAnswerPresentation(BaseModel):
    """Allowlisted facility fields without provider IDs or raw status text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    kind: Literal["facility"] = "facility"
    facility: FacilitySummaryView
    safety_notice: str
    evidence_sources: tuple[EvidenceSourceSummary, ...] = ()


AnswerPresentation = Annotated[
    TripAnswerPresentation | FacilityAnswerPresentation,
    Field(discriminator="kind"),
]

_PUBLIC_FACILITY_DETAIL_LIMIT = 5


def trip_answer_presentation(result: AccessibilityResult) -> TripAnswerPresentation:
    return TripAnswerPresentation(
        trip=TripSummaryView(
            origin=result.origin,
            destination=result.destination,
            route_summary=result.route_summary,
        ),
        safety_notice=result.safety_notice,
        confidence_reasons=tuple(result.confidence_reasons),
        evidence_sources=_public_evidence_sources(result.evidence_sources),
    )


def facility_answer_presentation(
    result: FacilityQuestionResult,
) -> FacilityAnswerPresentation:
    return FacilityAnswerPresentation(
        facility=FacilitySummaryView(
            station_name=result.station_name,
            line=result.line,
            question_kind=result.question_kind,
            items=tuple(
                FacilityItemView(
                    facility_type=item.facility_type,
                    answer_state=(
                        PublicFacilityAnswerState.NOT_CONFIRMED
                        if item.answer_state == FacilityAnswerState.NOT_FOUND
                        else PublicFacilityAnswerState(item.answer_state.value)
                    ),
                    facilities=tuple(
                        FacilityDetailView(
                            station_name=facility.station_name,
                            line=facility.line,
                            facility_type=facility.facility_type,
                            status=facility.status,
                            location_description=facility.location_description,
                            inside_gate=facility.inside_gate,
                            open_time=facility.open_time,
                            has_emergency_bell=facility.has_emergency_bell,
                        )
                        for facility in item.facilities[:_PUBLIC_FACILITY_DETAIL_LIMIT]
                    ),
                    total_count=len(item.facilities),
                    truncated=len(item.facilities) > _PUBLIC_FACILITY_DETAIL_LIMIT,
                )
                for item in result.items
            ),
        ),
        safety_notice=result.safety_notice,
        evidence_sources=_public_evidence_sources(result.evidence_sources),
    )


def _public_evidence_sources(
    sources: list[EvidenceSource],
) -> tuple[EvidenceSourceSummary, ...]:
    return tuple(
        EvidenceSourceSummary(
            display_name=source.display_name,
            source_type=source.source_type,
            checked_at=source.checked_at,
            success=source.success,
        )
        for source in sources
        if source.source_type in {"public_api", "cache"}
    )


class AnswerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: RiskLevel | None = None
    confidence_level: ConfidenceLevel | None = None
    failed_sources: list[FailedSourceSummary] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unverified_parts: list[str] = Field(default_factory=list)
    last_checked_at: datetime | None = None


class AnswerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3"] = "3"
    source: AnswerSource
    status: ResponseStatus
    intent: QuestionIntent
    user_message: str
    clarification_needed: bool = False
    clarification: ClarificationView | None = None
    questions: list[str] = Field(default_factory=list)
    clarification_options: list[ClarificationOption] = Field(default_factory=list)
    available_partial_info: list[str] = Field(default_factory=list)
    evidence: AnswerEvidence
    presentation: AnswerPresentation | None = None

    @model_validator(mode="after")
    def validate_public_projection(self) -> Self:
        needs_clarification = self.status == ResponseStatus.NEEDS_CLARIFICATION
        if needs_clarification != self.clarification_needed:
            raise ValueError("Clarification status and clarification_needed must match.")
        if self.clarification_options and len(self.questions) != 1:
            raise ValueError("Choice options require exactly one question.")
        if needs_clarification:
            if self.presentation is not None:
                raise ValueError("Clarification outcome cannot contain a presentation.")
            if not self.questions:
                raise ValueError("Clarification outcome requires questions.")
            if self.clarification is None:
                self.clarification = ClarificationView.from_legacy(
                    questions=self.questions,
                    options=self.clarification_options,
                )
            projected_questions = [prompt.question for prompt in self.clarification.prompts]
            if projected_questions != self.questions:
                raise ValueError("Clarification prompts must match legacy questions.")
            if list(self.clarification.options) != self.clarification_options:
                raise ValueError("Clarification options must match the nested projection.")
        elif self.clarification is not None or self.clarification_options or self.questions:
            raise ValueError("Terminal outcome cannot contain clarification controls.")
        if self.presentation is not None:
            if (
                self.intent == QuestionIntent.TRIP_ACCESSIBILITY
                and self.presentation.kind != "trip"
            ):
                raise ValueError("Trip outcome requires a trip presentation.")
            if (
                self.intent == QuestionIntent.FACILITY_STATUS
                and self.presentation.kind != "facility"
            ):
                raise ValueError("Facility outcome requires a facility presentation.")
            if self.intent not in {
                QuestionIntent.TRIP_ACCESSIBILITY,
                QuestionIntent.FACILITY_STATUS,
            }:
                raise ValueError("Unsupported intent cannot contain a presentation.")
        elif (
            self.source == AnswerSource.MCP
            and not needs_clarification
            and self.intent in {QuestionIntent.TRIP_ACCESSIBILITY, QuestionIntent.FACILITY_STATUS}
        ):
            raise ValueError("Terminal MCP outcome requires an intent-matched presentation.")
        return self

    @classmethod
    def from_mcp_result(cls, result: AccessibilityQuestionResult) -> AnswerOutcome:
        nested = result.result or result.facility_result
        return cls(
            source=AnswerSource.MCP,
            status=result.status,
            intent=result.intent,
            user_message=result.user_message,
            clarification_needed=result.clarification_needed,
            questions=result.questions,
            available_partial_info=result.available_partial_info,
            evidence=AnswerEvidence(
                risk_level=result.result.risk_level if result.result is not None else None,
                confidence_level=(
                    result.result.confidence_level if result.result is not None else None
                ),
                failed_sources=(
                    project_failed_source_summaries(nested.failed_sources)
                    if nested is not None
                    else []
                ),
                limitations=nested.limitations if nested is not None else [],
                unverified_parts=nested.unverified_parts if nested is not None else [],
                last_checked_at=nested.last_checked_at if nested is not None else None,
            ),
            presentation=(
                None
                if result.status == ResponseStatus.NEEDS_CLARIFICATION
                else trip_answer_presentation(result.result)
                if result.result is not None
                else facility_answer_presentation(result.facility_result)
                if result.facility_result is not None
                else None
            ),
        )

    @classmethod
    def question_too_complex(cls) -> AnswerOutcome:
        question = "출발지, 목적지와 필요한 이동 조건을 짧게 나누어 알려주시겠어요?"
        return cls(
            source=AnswerSource.AGENT,
            status=ResponseStatus.NEEDS_CLARIFICATION,
            intent=QuestionIntent.UNKNOWN,
            user_message=(
                "질문의 핵심 이동 조건을 한 번에 안전하게 확인하기 어렵습니다. " + question
            ),
            clarification_needed=True,
            questions=[question],
            evidence=AnswerEvidence(),
        )


class McpContractSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ContractStatus = ContractStatus.UNCHECKED
    checked_at: datetime | None = None
    tool_names: tuple[str, ...] = ()
    capabilities: tuple[McpCapability, ...] = ()
    schema_fingerprint: str | None = None
    error_code: str | None = None
