from __future__ import annotations

from typing import Protocol

from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    AnswerOutcome,
    McpContractSnapshot,
    MobilityProfile,
    StationResolutionResult,
)


class StructuredTripAgent(Protocol):
    def supports(self, question: str) -> bool: ...

    async def execute(self, question: str) -> AnswerOutcome: ...


class MobilityMcpGateway(Protocol):
    @property
    def contract_snapshot(self) -> McpContractSnapshot: ...

    async def initialize(self) -> McpContractSnapshot: ...

    async def refresh_if_stale(self) -> McpContractSnapshot: ...

    async def answer_accessibility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult: ...

    async def answer_facility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult: ...

    async def resolve_station(self, query: str) -> StationResolutionResult: ...

    async def generate_accessibility_brief(
        self,
        origin: str,
        destination: str,
        profile: MobilityProfile,
    ) -> AccessibilityResult: ...

    async def close(self) -> None: ...
