from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from barrier_free_mobility_agent.domain.models import (
    AccessibleRestroomRequirement,
    MobilityProfile,
)


class TriState(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class SlotSource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    USER_CONFIRMED = "user_confirmed"
    DERIVED_CONSERVATIVE = "derived_conservative"
    THREAD_CONTEXT = "thread_context"


class MobilityFlag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: TriState = TriState.UNKNOWN
    source: SlotSource | None = None

    @classmethod
    def known(cls, value: bool, source: SlotSource) -> MobilityFlag:
        return cls(value=TriState.TRUE if value else TriState.FALSE, source=source)


class MobilityContext(BaseModel):
    """Agent-owned mobility state. Unknown values are never MCP boolean defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wheelchair: MobilityFlag = Field(default_factory=MobilityFlag)
    stroller: MobilityFlag = Field(default_factory=MobilityFlag)
    cane_or_walker: MobilityFlag = Field(default_factory=MobilityFlag)
    can_use_stairs: MobilityFlag = Field(default_factory=MobilityFlag)
    can_use_escalator: MobilityFlag = Field(default_factory=MobilityFlag)
    need_elevator_only: MobilityFlag = Field(default_factory=MobilityFlag)
    need_accessible_restroom: MobilityFlag = Field(default_factory=MobilityFlag)
    need_wheelchair_charger: MobilityFlag = Field(default_factory=MobilityFlag)
    avoid_many_transfers: MobilityFlag = Field(default_factory=MobilityFlag)
    accessible_restroom_requirement: AccessibleRestroomRequirement | None = None
    max_transfer_count: int | None = None

    def with_flags(self, **updates: MobilityFlag) -> Self:
        return self.model_copy(update=updates)

    def safety_missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.can_use_stairs.value == TriState.UNKNOWN:
            missing.append("can_use_stairs")
        if self.can_use_escalator.value == TriState.UNKNOWN:
            missing.append("can_use_escalator")
        return tuple(missing)


class MobilityProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: MobilityProfile
    current_turn_assumptions: tuple[str, ...]


def project_mobility_context(context: MobilityContext) -> MobilityProjection:
    """Build every MCP field explicitly without mutating confirmed Agent state."""

    missing = context.safety_missing_fields()
    if missing:
        raise ValueError("Mobility safety fields require clarification: " + ", ".join(missing))

    wheelchair = context.wheelchair.value == TriState.TRUE
    can_use_stairs = context.can_use_stairs.value == TriState.TRUE
    can_use_escalator = context.can_use_escalator.value == TriState.TRUE
    assumptions = [
        name
        for name, flag in (
            ("wheelchair", context.wheelchair),
            ("stroller", context.stroller),
            ("cane_or_walker", context.cane_or_walker),
            ("need_accessible_restroom", context.need_accessible_restroom),
            ("need_wheelchair_charger", context.need_wheelchair_charger),
        )
        if flag.value == TriState.UNKNOWN
    ]
    if context.avoid_many_transfers.value == TriState.UNKNOWN:
        assumptions.append("avoid_many_transfers=true_conservative")

    return MobilityProjection(
        profile=MobilityProfile(
            wheelchair=wheelchair,
            stroller=context.stroller.value == TriState.TRUE,
            cane_or_walker=context.cane_or_walker.value == TriState.TRUE,
            can_use_stairs=can_use_stairs,
            can_use_escalator=can_use_escalator,
            need_elevator_only=(context.need_elevator_only.value == TriState.TRUE),
            need_accessible_restroom=(context.need_accessible_restroom.value == TriState.TRUE),
            accessible_restroom_requirement=(
                context.accessible_restroom_requirement
                or AccessibleRestroomRequirement.ALL_KEY_STATIONS
            ),
            need_wheelchair_charger=(context.need_wheelchair_charger.value == TriState.TRUE),
            avoid_many_transfers=(context.avoid_many_transfers.value != TriState.FALSE),
            max_transfer_count=context.max_transfer_count,
        ),
        current_turn_assumptions=tuple(assumptions),
    )
