from __future__ import annotations

import pytest

from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    MobilityFlag,
    SlotSource,
    TriState,
    project_mobility_context,
)


def test_unknown_safety_fields_are_not_projected_to_mcp_defaults() -> None:
    with pytest.raises(ValueError, match="can_use_stairs, can_use_escalator"):
        project_mobility_context(MobilityContext())


def test_projection_populates_every_mcp_field_without_confirming_unknown_state() -> None:
    context = MobilityContext().with_flags(
        can_use_stairs=MobilityFlag.known(False, SlotSource.USER_CONFIRMED),
        can_use_escalator=MobilityFlag.known(False, SlotSource.USER_CONFIRMED),
    )

    projection = project_mobility_context(context)

    assert projection.profile.model_dump(mode="json") == {
        "wheelchair": False,
        "stroller": False,
        "cane_or_walker": False,
        "can_use_stairs": False,
        "can_use_escalator": False,
        "need_elevator_only": False,
        "need_accessible_restroom": False,
        "accessible_restroom_requirement": "all_key_stations",
        "need_wheelchair_charger": False,
        "avoid_many_transfers": True,
        "max_transfer_count": None,
    }
    assert context.wheelchair.value == TriState.UNKNOWN
    assert context.avoid_many_transfers.value == TriState.UNKNOWN
    assert "wheelchair" in projection.current_turn_assumptions
    assert "avoid_many_transfers=true_conservative" in projection.current_turn_assumptions


def test_wheelchair_alone_does_not_confirm_unknown_safety_fields() -> None:
    context = MobilityContext().with_flags(
        wheelchair=MobilityFlag.known(True, SlotSource.USER_EXPLICIT)
    )

    with pytest.raises(ValueError, match="can_use_stairs, can_use_escalator"):
        project_mobility_context(context)


def test_wheelchair_projection_preserves_explicit_independent_abilities() -> None:
    context = MobilityContext().with_flags(
        wheelchair=MobilityFlag.known(True, SlotSource.USER_EXPLICIT),
        can_use_stairs=MobilityFlag.known(True, SlotSource.USER_EXPLICIT),
        can_use_escalator=MobilityFlag.known(False, SlotSource.USER_EXPLICIT),
    )

    projection = project_mobility_context(context)

    assert projection.profile.wheelchair is True
    assert projection.profile.can_use_stairs is True
    assert projection.profile.can_use_escalator is False
    assert projection.profile.need_elevator_only is False
