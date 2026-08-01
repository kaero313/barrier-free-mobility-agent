from __future__ import annotations

import pytest
from tests.factories import trip_question_result

from barrier_free_mobility_agent.domain.models import (
    AnswerOutcome,
    FailedSource,
    FailedSourceCode,
    ResponseStatus,
    project_failed_source_summaries,
)


@pytest.mark.parametrize(
    ("internal_name", "source_code", "display_name"),
    [
        ("shortest_route", FailedSourceCode.ROUTE_INFORMATION, "이동 경로 정보"),
        ("station_resolution", FailedSourceCode.STATION_INFORMATION, "역 정보"),
        ("elevator_status", FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
        ("elevator_info", FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
        ("facility_info", FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
        ("restroom", FailedSourceCode.FACILITY_INFORMATION, "역 시설 정보"),
    ],
)
def test_known_failed_sources_use_stable_public_terms(
    internal_name: str,
    source_code: FailedSourceCode,
    display_name: str,
) -> None:
    summary = project_failed_source_summaries(
        [
            FailedSource(
                source_name=internal_name,
                reason="private upstream failure",
                recoverable=True,
            )
        ]
    )[0]

    assert summary.source_name == source_code
    assert summary.display_name == display_name
    assert summary.recoverable is True


def test_unknown_failed_source_fails_closed_to_generic_mobility_term() -> None:
    internal_name = "private_tool_v42"
    private_reason = "secret upstream topology"

    payload = project_failed_source_summaries(
        [
            FailedSource(
                source_name=internal_name,
                reason=private_reason,
                recoverable=False,
            )
        ]
    )[0].model_dump(mode="json")
    serialized = str(payload)

    assert payload == {
        "source_name": "mobility_information",
        "display_name": "이동 정보",
        "recoverable": False,
    }
    assert internal_name not in serialized
    assert private_reason not in serialized
    assert "reason" not in payload


def test_mcp_outcome_projects_failed_source_before_public_serialization() -> None:
    outcome = AnswerOutcome.from_mcp_result(trip_question_result(status=ResponseStatus.PARTIAL))
    payload = outcome.model_dump(mode="json")

    assert payload["evidence"]["failed_sources"] == [
        {
            "source_name": "facility_information",
            "display_name": "역 시설 정보",
            "recoverable": True,
        }
    ]
    assert "elevator_status" not in str(payload)
    assert "temporary failure" not in str(payload)
