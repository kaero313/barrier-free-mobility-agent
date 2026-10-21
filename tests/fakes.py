from __future__ import annotations

from barrier_free_mobility_agent.domain.errors import AgentError
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    ContractStatus,
    McpCapability,
    McpContractSnapshot,
    MobilityProfile,
    Station,
    StationResolutionResult,
)
from tests.factories import trip_question_result


class FakeMcpGateway:
    def __init__(
        self,
        *,
        result: AccessibilityQuestionResult | None = None,
        snapshot: McpContractSnapshot | None = None,
        initialize_error: AgentError | None = None,
        answer_error: BaseException | None = None,
        station_results: dict[str, StationResolutionResult] | None = None,
        brief_result: AccessibilityResult | None = None,
        resolve_error: BaseException | None = None,
        brief_error: BaseException | None = None,
    ) -> None:
        self.result = result or trip_question_result()
        self._contract_snapshot = snapshot or McpContractSnapshot(
            status=ContractStatus.READY,
            tool_names=("answer_accessibility_question",),
            capabilities=(
                McpCapability.TRIP_ACCESSIBILITY,
                McpCapability.FACILITY_QUESTION,
            ),
            schema_fingerprint="test-fingerprint",
        )
        self.initialize_error = initialize_error
        self.answer_error = answer_error
        self.station_results = station_results or {}
        self.brief_result = brief_result
        self.resolve_error = resolve_error
        self.brief_error = brief_error
        self.initialize_calls = 0
        self.answer_calls: list[str] = []
        self.resolve_calls: list[str] = []
        self.brief_calls: list[tuple[str, str, MobilityProfile]] = []
        self.brief_compare_alternatives: list[bool] = []
        self.closed = False

    @property
    def contract_snapshot(self) -> McpContractSnapshot:
        return self._contract_snapshot

    async def initialize(self) -> McpContractSnapshot:
        self.initialize_calls += 1
        if self.initialize_error is not None:
            raise self.initialize_error
        return self._contract_snapshot

    async def refresh_if_stale(self) -> McpContractSnapshot:
        return await self.initialize()

    async def answer_accessibility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult:
        self.answer_calls.append(question)
        if self.answer_error is not None:
            raise self.answer_error
        return self.result

    async def answer_facility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult:
        return await self.answer_accessibility_question(question)

    async def resolve_station(self, query: str) -> StationResolutionResult:
        self.resolve_calls.append(query)
        if self.resolve_error is not None:
            raise self.resolve_error
        if query in self.station_results:
            return self.station_results[query]
        return StationResolutionResult(
            query=query,
            matched_station=Station(station_name=query),
        )

    async def generate_accessibility_brief(
        self,
        origin: str,
        destination: str,
        profile: MobilityProfile,
        *,
        compare_alternatives: bool = False,
    ) -> AccessibilityResult:
        self.brief_calls.append((origin, destination, profile))
        self.brief_compare_alternatives.append(compare_alternatives)
        if self.brief_error is not None:
            raise self.brief_error
        if self.brief_result is not None:
            return self.brief_result
        result = self.result.result
        if result is None:
            raise AssertionError("Fake result does not contain a trip accessibility result.")
        return result

    async def close(self) -> None:
        self.closed = True

    def set_snapshot(self, snapshot: McpContractSnapshot) -> None:
        self._contract_snapshot = snapshot
