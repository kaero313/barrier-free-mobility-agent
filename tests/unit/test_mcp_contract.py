from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest
from mcp.types import Tool

from barrier_free_mobility_agent.domain.errors import (
    McpContractError,
    McpDependencyDegradedError,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityResult,
    McpCapability,
    MobilityProfile,
    StationResolutionResult,
)
from barrier_free_mobility_agent.integrations.mcp.contract import (
    fingerprint_tools,
    validate_answer_question_tool,
    validate_generate_accessibility_brief_tool,
    validate_health_payload,
    validate_required_tools,
    validate_resolve_station_tool,
)


def answer_tool(
    *,
    question_type: str = "string",
    include_required: bool = True,
) -> Tool:
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"question": {"type": question_type}},
    }
    if include_required:
        input_schema["required"] = ["question"]
    return Tool(
        name="answer_accessibility_question",
        description="Answer a question",
        inputSchema=input_schema,
        outputSchema=valid_output_schema(),
    )


def valid_output_schema() -> dict[str, object]:
    response_status = {
        "type": "string",
        "enum": ["SUCCESS", "PARTIAL", "FAILED", "NEEDS_CLARIFICATION"],
    }
    mobility_profile = {"type": "object", "properties": {}}
    string_array = {"type": "array", "items": {"type": "string"}}
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    facility_type = {
        "type": "string",
        "enum": [
            "ELEVATOR",
            "ESCALATOR",
            "ACCESSIBLE_RESTROOM",
            "RESTROOM",
            "WHEELCHAIR_CHARGER",
            "UNKNOWN",
        ],
    }
    trip_result = {
        "type": "object",
        "properties": {
            "status": response_status,
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "mobility_profile": mobility_profile,
            "risk_level": {
                "type": "string",
                "enum": ["LOW", "CAUTION", "HIGH", "UNKNOWN"],
            },
            "risk_score": {"type": "integer"},
            "route_summary": {"type": "string"},
            "confidence_level": {
                "type": "string",
                "enum": ["HIGH", "MEDIUM", "LOW"],
            },
            "failed_sources": {"type": "array", "items": {"type": "object"}},
            "limitations": string_array,
            "unverified_parts": string_array,
            "safety_notice": {"type": "string"},
            "user_message": {"type": "string"},
        },
    }
    facility_result = {
        "type": "object",
        "properties": {
            "status": response_status,
            "station_name": {"type": "string"},
            "line": nullable_string,
            "question_kind": {
                "type": "string",
                "enum": ["status", "location", "existence", "overview"],
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "facility_type": facility_type,
                        "answer_state": {
                            "type": "string",
                            "enum": [
                                "AVAILABLE",
                                "MIXED",
                                "MAINTENANCE",
                                "UNAVAILABLE",
                                "NOT_FOUND",
                                "UNKNOWN",
                            ],
                        },
                        "facilities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "station_name": {"type": "string"},
                                    "line": nullable_string,
                                    "facility_type": facility_type,
                                    "status": {
                                        "type": "string",
                                        "enum": [
                                            "AVAILABLE",
                                            "UNAVAILABLE",
                                            "MAINTENANCE",
                                            "UNKNOWN",
                                        ],
                                    },
                                    "location_description": nullable_string,
                                    "inside_gate": {
                                        "anyOf": [
                                            {"type": "boolean"},
                                            {"type": "null"},
                                        ]
                                    },
                                    "open_time": nullable_string,
                                    "has_emergency_bell": {
                                        "anyOf": [
                                            {"type": "boolean"},
                                            {"type": "null"},
                                        ]
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "last_checked_at": nullable_string,
            "evidence_sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_name": {"type": "string"},
                        "display_name": {"type": "string"},
                        "source_type": {
                            "type": "string",
                            "enum": ["public_api", "cache", "fixture", "internal"],
                        },
                        "checked_at": nullable_string,
                        "success": {"type": "boolean"},
                    },
                },
            },
            "failed_sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_name": {"type": "string"},
                        "reason": {"type": "string"},
                        "recoverable": {"type": "boolean"},
                    },
                },
            },
            "limitations": string_array,
            "unverified_parts": string_array,
            "safety_notice": {"type": "string"},
            "user_message": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "status": response_status,
            "intent": {
                "type": "string",
                "enum": [
                    "trip_accessibility",
                    "facility_status",
                    "alternative_request",
                    "unknown",
                ],
            },
            "parsed": {
                "type": "object",
                "properties": {
                    "mobility_profile": mobility_profile,
                    "target_station": nullable_string,
                    "target_line": nullable_string,
                    "facility_types": {"type": "array", "items": facility_type},
                    "facility_question_kind": {
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": ["status", "location", "existence", "overview"],
                            },
                            {"type": "null"},
                        ]
                    },
                    "alternative_request_kind": {
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": ["station_facility", "route", "current_route"],
                            },
                            {"type": "null"},
                        ]
                    },
                    "station_mentions": string_array,
                    "missing_fields": string_array,
                },
            },
            "result": {"anyOf": [trip_result, {"type": "null"}]},
            "facility_result": {"anyOf": [facility_result, {"type": "null"}]},
            "user_message": {"type": "string"},
            "clarification_needed": {"type": "boolean"},
            "questions": string_array,
            "available_partial_info": string_array,
        },
        "required": ["question", "status"],
    }


def resolve_station_tool() -> Tool:
    return Tool(
        name="resolve_station",
        description="Resolve a station",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        outputSchema=StationResolutionResult.model_json_schema(),
    )


def generate_accessibility_brief_tool() -> Tool:
    profile_schema = MobilityProfile.model_json_schema()
    profile_definitions = cast(dict[str, object], profile_schema.pop("$defs"))
    return Tool(
        name="generate_accessibility_brief",
        description="Generate a brief",
        inputSchema={
            "type": "object",
            "$defs": profile_definitions,
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "mobility_profile": profile_schema,
            },
            "required": ["origin", "destination", "mobility_profile"],
        },
        outputSchema=AccessibilityResult.model_json_schema(),
    )


def required_tools() -> list[Tool]:
    return [answer_tool(), resolve_station_tool(), generate_accessibility_brief_tool()]


def test_fingerprint_is_independent_of_tool_and_json_key_order() -> None:
    first = answer_tool()
    second = Tool(
        name="resolve_station",
        inputSchema={"properties": {"query": {"type": "string"}}, "type": "object"},
        outputSchema={"properties": {}, "type": "object"},
    )
    reordered_first = Tool(
        name=first.name,
        description="Changed description is excluded",
        inputSchema={
            "required": ["question"],
            "properties": {"question": {"type": "string"}},
            "type": "object",
        },
        outputSchema=first.outputSchema,
    )

    assert fingerprint_tools([first, second]) == fingerprint_tools([second, reordered_first])


def test_required_tool_contract_is_accepted() -> None:
    assert validate_answer_question_tool([answer_tool()]) is True


def test_phase2_typed_tool_contracts_are_accepted() -> None:
    tools = required_tools()

    assert validate_required_tools(tools) == (
        McpCapability.TRIP_ACCESSIBILITY,
        McpCapability.FACILITY_QUESTION,
    )
    validate_resolve_station_tool(tools)
    validate_generate_accessibility_brief_tool(tools)


@pytest.mark.parametrize(
    ("tools", "reason"),
    [
        ([answer_tool(), generate_accessibility_brief_tool()], "resolve_station_tool_missing"),
        ([answer_tool(), resolve_station_tool()], "generate_accessibility_brief_tool_missing"),
    ],
)
def test_startup_contract_requires_phase2_tools(
    tools: list[Tool],
    reason: str,
) -> None:
    with pytest.raises(McpContractError) as captured:
        validate_required_tools(tools)

    assert captured.value.context["reason"] == reason


def test_resolve_station_rejects_incompatible_nested_station_schema() -> None:
    tool = resolve_station_tool()
    schema = cast(dict[str, object], tool.outputSchema)
    definitions = cast(dict[str, object], schema["$defs"])
    station = cast(dict[str, object], definitions["Station"])
    properties = cast(dict[str, object], station["properties"])
    properties["confidence"] = {"type": "string"}

    with pytest.raises(McpContractError) as captured:
        validate_resolve_station_tool([tool])

    assert captured.value.context["reason"] == "output_confidence_not_number"


def test_brief_rejects_incompatible_mobility_profile_schema() -> None:
    tool = generate_accessibility_brief_tool()
    schema = cast(dict[str, object], tool.inputSchema)
    properties = cast(dict[str, object], schema["properties"])
    profile = cast(dict[str, object], properties["mobility_profile"])
    profile_properties = cast(dict[str, object], profile["properties"])
    profile_properties.pop("can_use_stairs")

    with pytest.raises(McpContractError) as captured:
        validate_generate_accessibility_brief_tool([tool])

    assert captured.value.context["reason"] == "mobility_profile_properties_missing"


def test_published_trip_only_contract_is_accepted_without_facility_extension() -> None:
    schema = valid_output_schema()
    properties = cast(dict[str, object], schema["properties"])
    properties.pop("facility_result")
    tool = answer_tool()
    tool.outputSchema = schema

    assert validate_answer_question_tool([tool]) is False


def test_facility_capability_rejects_non_object_items() -> None:
    schema = valid_output_schema()
    properties = cast(dict[str, object], schema["properties"])
    facility_union = cast(dict[str, object], properties["facility_result"])
    facility = cast(list[dict[str, object]], facility_union["anyOf"])[0]
    facility_properties = cast(dict[str, object], facility["properties"])
    facility_properties["items"] = {"type": "array", "items": {"type": "string"}}
    tool = answer_tool()
    tool.outputSchema = schema

    with pytest.raises(McpContractError) as captured:
        validate_answer_question_tool([tool])

    assert captured.value.context["reason"] == "output_items_items_not_object"


def test_facility_capability_rejects_provider_enum_expansion() -> None:
    schema = valid_output_schema()
    properties = cast(dict[str, object], schema["properties"])
    facility_union = cast(dict[str, object], properties["facility_result"])
    facility = cast(list[dict[str, object]], facility_union["anyOf"])[0]
    facility_properties = cast(dict[str, object], facility["properties"])
    items = cast(dict[str, object], facility_properties["items"])
    item = cast(dict[str, object], items["items"])
    item_properties = cast(dict[str, object], item["properties"])
    answer_state = cast(dict[str, object], item_properties["answer_state"])
    enum_values = cast(list[str], answer_state["enum"])
    enum_values.append("FUTURE_STATE")
    tool = answer_tool()
    tool.outputSchema = schema

    with pytest.raises(McpContractError) as captured:
        validate_answer_question_tool([tool])

    assert captured.value.context["reason"] == "output_answer_state_enum_incompatible"


def test_facility_capability_requires_parsed_facility_fields() -> None:
    schema = valid_output_schema()
    properties = cast(dict[str, object], schema["properties"])
    parsed = cast(dict[str, object], properties["parsed"])
    parsed_properties = cast(dict[str, object], parsed["properties"])
    parsed_properties.pop("target_station")
    tool = answer_tool()
    tool.outputSchema = schema

    with pytest.raises(McpContractError) as captured:
        validate_answer_question_tool([tool])

    assert captured.value.context["reason"] == "output_target_station_not_string"


@pytest.mark.parametrize(
    ("tools", "reason"),
    [
        ([], "required_tool_missing"),
        ([answer_tool(question_type="integer")], "question_input_not_string"),
        ([answer_tool(include_required=False)], "question_input_not_required"),
    ],
)
def test_incompatible_tool_contract_is_rejected(
    tools: list[Tool],
    reason: str,
) -> None:
    with pytest.raises(McpContractError) as captured:
        validate_answer_question_tool(tools)

    assert captured.value.context["reason"] == reason


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda schema: schema.update({"required": ["question"]}),
            "required_output_fields_incompatible",
        ),
        (
            lambda schema: schema["properties"]["intent"].update({"enum": ["unknown"]}),
            "output_intent_enum_incompatible",
        ),
        (
            lambda schema: schema["properties"]["result"]["anyOf"][0]["properties"][
                "risk_score"
            ].update({"type": "string"}),
            "output_risk_score_not_integer",
        ),
    ],
)
def test_incompatible_output_contract_is_rejected(
    mutation: object,
    reason: str,
) -> None:
    schema = deepcopy(valid_output_schema())
    assert callable(mutation)
    mutation(schema)
    tool = answer_tool()
    tool.outputSchema = schema

    with pytest.raises(McpContractError) as captured:
        validate_answer_question_tool([tool])

    assert captured.value.context["reason"] == reason


def test_healthy_mcp_payload_is_accepted() -> None:
    validate_health_payload(
        {
            "status": "ok",
            "mcp": {"tool_input_max_chars": 120, "auth_enabled": False},
        },
        expected_tool_input_max_chars=120,
        expected_auth_enabled=False,
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "status": "ok",
                "mcp": {"tool_input_max_chars": 121, "auth_enabled": False},
            },
            "tool_input_limit_mismatch",
        ),
        (
            {
                "status": "ok",
                "mcp": {"tool_input_max_chars": 120, "auth_enabled": True},
            },
            "auth_mode_mismatch",
        ),
    ],
)
def test_unhealthy_or_mismatched_health_is_rejected(
    payload: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(McpContractError) as captured:
        validate_health_payload(
            payload,
            expected_tool_input_max_chars=120,
            expected_auth_enabled=False,
        )

    assert captured.value.context["reason"] == reason


def test_degraded_health_is_distinct_from_contract_mismatch() -> None:
    with pytest.raises(McpDependencyDegradedError):
        validate_health_payload(
            {
                "status": "degraded",
                "mcp": {"tool_input_max_chars": 120, "auth_enabled": False},
            },
            expected_tool_input_max_chars=120,
            expected_auth_enabled=False,
        )
