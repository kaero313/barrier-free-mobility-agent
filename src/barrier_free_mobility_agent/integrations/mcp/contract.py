from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Never

from mcp.types import Tool

from barrier_free_mobility_agent.domain.errors import (
    McpContractError,
    McpDependencyDegradedError,
)
from barrier_free_mobility_agent.domain.models import McpCapability

ANSWER_QUESTION_TOOL = "answer_accessibility_question"
RESOLVE_STATION_TOOL = "resolve_station"
GENERATE_ACCESSIBILITY_BRIEF_TOOL = "generate_accessibility_brief"
_RESPONSE_STATUSES = {"SUCCESS", "PARTIAL", "FAILED", "NEEDS_CLARIFICATION"}
_QUESTION_INTENTS = {
    "trip_accessibility",
    "facility_status",
    "alternative_request",
    "unknown",
}
_RISK_LEVELS = {"LOW", "CAUTION", "HIGH", "UNKNOWN"}
_CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW"}
_FACILITY_QUESTION_KINDS = {"status", "location", "existence", "overview"}
_FACILITY_TYPES = {
    "ELEVATOR",
    "ESCALATOR",
    "WHEELCHAIR_LIFT",
    "ACCESSIBLE_RESTROOM",
    "RESTROOM",
    "WHEELCHAIR_CHARGER",
    "UNKNOWN",
}
_FACILITY_ANSWER_STATES = {
    "AVAILABLE",
    "MIXED",
    "MAINTENANCE",
    "UNAVAILABLE",
    "NOT_FOUND",
    "UNSUPPORTED",
    "UNKNOWN",
}
_FACILITY_STATUSES = {"AVAILABLE", "UNAVAILABLE", "MAINTENANCE", "UNKNOWN"}
_ALTERNATIVE_REQUEST_KINDS = {"station_facility", "route", "current_route"}
_SOURCE_TYPES = {"public_api", "cache", "fixture", "internal"}
_RESTROOM_REQUIREMENTS = {
    "any_route_station",
    "origin",
    "transfer",
    "destination",
    "origin_or_destination",
    "all_key_stations",
}
_MOBILITY_BOOLEAN_FIELDS = {
    "wheelchair",
    "stroller",
    "cane_or_walker",
    "can_use_stairs",
    "can_use_escalator",
    "need_elevator_only",
    "need_accessible_restroom",
    "need_wheelchair_charger",
    "avoid_many_transfers",
}


def fingerprint_tools(tools: Sequence[Tool]) -> str:
    contracts = [
        {
            "name": tool.name,
            "inputSchema": tool.inputSchema,
            "outputSchema": tool.outputSchema,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    canonical = json.dumps(
        contracts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_required_tools(tools: Sequence[Tool]) -> tuple[McpCapability, ...]:
    facility_capable = validate_answer_question_tool(tools)
    validate_resolve_station_tool(tools)
    validate_generate_accessibility_brief_tool(tools)
    capabilities = [McpCapability.TRIP_ACCESSIBILITY]
    if facility_capable:
        capabilities.append(McpCapability.FACILITY_QUESTION)
    return tuple(capabilities)


def validate_answer_question_tool(tools: Sequence[Tool]) -> bool:
    tool = next((item for item in tools if item.name == ANSWER_QUESTION_TOOL), None)
    if tool is None:
        _raise_contract("required_tool_missing")

    input_schema = tool.inputSchema
    if input_schema.get("type") != "object":
        _raise_contract("input_schema_not_object")
    properties = _mapping(input_schema.get("properties"))
    question_schema = _mapping(properties.get("question"))
    if question_schema.get("type") != "string":
        _raise_contract("question_input_not_string")
    required = input_schema.get("required")
    if not isinstance(required, list) or "question" not in required:
        _raise_contract("question_input_not_required")

    output_schema = tool.outputSchema
    if not isinstance(output_schema, Mapping) or output_schema.get("type") != "object":
        _raise_contract("output_schema_not_object")
    output_properties = _mapping(output_schema.get("properties"))
    consumed_output_properties = {
        "question",
        "status",
        "intent",
        "parsed",
        "result",
        "user_message",
        "clarification_needed",
        "questions",
        "available_partial_info",
    }
    if not consumed_output_properties.issubset(output_properties):
        _raise_contract("required_output_properties_missing")

    required_output = _string_set(output_schema.get("required"))
    if not {"question", "status"}.issubset(required_output):
        _raise_contract("required_output_fields_incompatible")

    _require_type(output_properties, "question", "string", output_schema)
    _require_enum(
        output_properties,
        "status",
        _RESPONSE_STATUSES,
        output_schema,
    )
    _require_enum(output_properties, "intent", _QUESTION_INTENTS, output_schema)
    _require_type(output_properties, "user_message", "string", output_schema)
    _require_type(output_properties, "clarification_needed", "boolean", output_schema)
    _require_string_array(output_properties, "questions", output_schema)
    _require_string_array(output_properties, "available_partial_info", output_schema)

    parsed_schema = _require_object(output_properties, "parsed", output_schema)
    parsed_properties = _mapping(parsed_schema.get("properties"))
    _require_object(parsed_properties, "mobility_profile", output_schema)

    trip_schema = _require_object(output_properties, "result", output_schema)
    trip_properties = _mapping(trip_schema.get("properties"))
    _require_enum(trip_properties, "status", _RESPONSE_STATUSES, output_schema)
    _require_type(trip_properties, "origin", "string", output_schema)
    _require_type(trip_properties, "destination", "string", output_schema)
    _require_object(trip_properties, "mobility_profile", output_schema)
    _require_enum(trip_properties, "risk_level", _RISK_LEVELS, output_schema)
    _require_type(trip_properties, "risk_score", "integer", output_schema)
    _require_type(trip_properties, "route_summary", "string", output_schema)
    _require_enum(
        trip_properties,
        "confidence_level",
        _CONFIDENCE_LEVELS,
        output_schema,
    )
    _require_type(trip_properties, "failed_sources", "array", output_schema)
    _require_string_array(trip_properties, "limitations", output_schema)
    _require_string_array(trip_properties, "unverified_parts", output_schema)
    _require_type(trip_properties, "safety_notice", "string", output_schema)
    _require_type(trip_properties, "user_message", "string", output_schema)

    if "facility_result" not in output_properties:
        return False
    _validate_facility_extension(
        parsed_properties=parsed_properties,
        output_properties=output_properties,
        root=output_schema,
    )
    return True


def _validate_facility_extension(
    *,
    parsed_properties: Mapping[str, Any],
    output_properties: Mapping[str, Any],
    root: Mapping[str, Any],
) -> None:
    _require_nullable_type(parsed_properties, "target_station", "string", root)
    _require_nullable_type(parsed_properties, "target_line", "string", root)
    _require_enum_array(parsed_properties, "facility_types", _FACILITY_TYPES, root)
    _require_nullable_enum(
        parsed_properties,
        "facility_question_kind",
        _FACILITY_QUESTION_KINDS,
        root,
    )
    _require_nullable_enum(
        parsed_properties,
        "alternative_request_kind",
        _ALTERNATIVE_REQUEST_KINDS,
        root,
    )
    _require_string_array(parsed_properties, "station_mentions", root)
    _require_string_array(parsed_properties, "missing_fields", root)

    facility_schema = _require_nullable_type(
        output_properties,
        "facility_result",
        "object",
        root,
    )
    facility_properties = _mapping(facility_schema.get("properties"))
    _require_exact_enum(facility_properties, "status", _RESPONSE_STATUSES, root)
    _require_type(facility_properties, "station_name", "string", root)
    _require_nullable_type(facility_properties, "line", "string", root)
    _require_exact_enum(
        facility_properties,
        "question_kind",
        _FACILITY_QUESTION_KINDS,
        root,
    )
    item_schema = _require_array_items_object(facility_properties, "items", root)
    item_properties = _mapping(item_schema.get("properties"))
    _require_exact_enum(item_properties, "facility_type", _FACILITY_TYPES, root)
    _require_exact_enum(
        item_properties,
        "answer_state",
        _FACILITY_ANSWER_STATES,
        root,
    )
    child_schema = _require_array_items_object(item_properties, "facilities", root)
    child_properties = _mapping(child_schema.get("properties"))
    _require_type(child_properties, "station_name", "string", root)
    _require_nullable_type(child_properties, "line", "string", root)
    _require_exact_enum(child_properties, "facility_type", _FACILITY_TYPES, root)
    _require_exact_enum(child_properties, "status", _FACILITY_STATUSES, root)
    for field in ("location_description", "open_time"):
        _require_nullable_type(child_properties, field, "string", root)
    for field in ("inside_gate", "has_emergency_bell"):
        _require_nullable_type(child_properties, field, "boolean", root)

    _require_nullable_type(facility_properties, "last_checked_at", "string", root)
    evidence_schema = _require_array_items_object(
        facility_properties,
        "evidence_sources",
        root,
    )
    evidence_properties = _mapping(evidence_schema.get("properties"))
    _require_type(evidence_properties, "source_name", "string", root)
    _require_type(evidence_properties, "display_name", "string", root)
    _require_exact_enum(evidence_properties, "source_type", _SOURCE_TYPES, root)
    _require_nullable_type(evidence_properties, "checked_at", "string", root)
    _require_type(evidence_properties, "success", "boolean", root)

    failed_schema = _require_array_items_object(
        facility_properties,
        "failed_sources",
        root,
    )
    failed_properties = _mapping(failed_schema.get("properties"))
    _require_type(failed_properties, "source_name", "string", root)
    _require_type(failed_properties, "reason", "string", root)
    _require_type(failed_properties, "recoverable", "boolean", root)
    _require_string_array(facility_properties, "limitations", root)
    _require_string_array(facility_properties, "unverified_parts", root)
    _require_type(facility_properties, "safety_notice", "string", root)
    _require_type(facility_properties, "user_message", "string", root)


def validate_resolve_station_tool(tools: Sequence[Tool]) -> None:
    tool = next((item for item in tools if item.name == RESOLVE_STATION_TOOL), None)
    if tool is None:
        _raise_contract("resolve_station_tool_missing")

    input_schema = tool.inputSchema
    if input_schema.get("type") != "object":
        _raise_contract("resolve_station_input_schema_not_object")
    input_properties = _mapping(input_schema.get("properties"))
    if _find_type(_mapping(input_properties.get("query")), "string", input_schema) is None:
        _raise_contract("resolve_station_query_input_not_string")
    if "query" not in _string_set(input_schema.get("required")):
        _raise_contract("resolve_station_query_input_not_required")

    output_schema = tool.outputSchema
    if not isinstance(output_schema, Mapping) or output_schema.get("type") != "object":
        _raise_contract("resolve_station_output_schema_not_object")
    output_properties = _mapping(output_schema.get("properties"))
    consumed_properties = {
        "query",
        "matched_station",
        "candidates",
        "needs_clarification",
        "clarification_message",
    }
    if not consumed_properties.issubset(output_properties):
        _raise_contract("resolve_station_required_output_properties_missing")
    if not {"query", "matched_station"}.issubset(_string_set(output_schema.get("required"))):
        _raise_contract("resolve_station_required_output_fields_incompatible")

    _require_type(output_properties, "query", "string", output_schema)
    _require_type(output_properties, "needs_clarification", "boolean", output_schema)
    _require_type(output_properties, "clarification_message", "string", output_schema)

    matched_schema = _mapping(output_properties.get("matched_station"))
    matched_station = _find_type(matched_schema, "object", output_schema)
    if matched_station is None:
        _raise_contract("resolve_station_matched_station_not_object")
    if _find_type(matched_schema, "null", output_schema) is None:
        _raise_contract("resolve_station_matched_station_not_nullable")
    _validate_station_schema(matched_station, output_schema)

    candidates = _require_type(output_properties, "candidates", "array", output_schema)
    candidate = _find_type(_mapping(candidates.get("items")), "object", output_schema)
    if candidate is None:
        _raise_contract("resolve_station_candidates_items_not_object")
    _validate_station_schema(candidate, output_schema)


def validate_generate_accessibility_brief_tool(tools: Sequence[Tool]) -> None:
    tool = next(
        (item for item in tools if item.name == GENERATE_ACCESSIBILITY_BRIEF_TOOL),
        None,
    )
    if tool is None:
        _raise_contract("generate_accessibility_brief_tool_missing")

    input_schema = tool.inputSchema
    if input_schema.get("type") != "object":
        _raise_contract("generate_accessibility_brief_input_schema_not_object")
    input_properties = _mapping(input_schema.get("properties"))
    consumed_inputs = {"origin", "destination", "mobility_profile"}
    if not consumed_inputs.issubset(input_properties):
        _raise_contract("generate_accessibility_brief_input_properties_missing")
    if not consumed_inputs.issubset(_string_set(input_schema.get("required"))):
        _raise_contract("generate_accessibility_brief_inputs_not_required")
    _require_type(input_properties, "origin", "string", input_schema)
    _require_type(input_properties, "destination", "string", input_schema)
    profile_schema = _require_object(input_properties, "mobility_profile", input_schema)
    _validate_mobility_profile_schema(profile_schema, input_schema)

    output_schema = tool.outputSchema
    if not isinstance(output_schema, Mapping) or output_schema.get("type") != "object":
        _raise_contract("generate_accessibility_brief_output_schema_not_object")
    required_output = {
        "origin",
        "destination",
        "mobility_profile",
        "risk_level",
        "risk_score",
        "route_summary",
    }
    if not required_output.issubset(_string_set(output_schema.get("required"))):
        _raise_contract("generate_accessibility_brief_required_output_fields_incompatible")
    _validate_accessibility_result_schema(
        output_schema,
        output_schema,
        validate_mobility_profile=True,
    )


def validate_health_payload(
    payload: Mapping[str, Any],
    *,
    expected_tool_input_max_chars: int,
    expected_auth_enabled: bool,
) -> None:
    if payload.get("status") != "ok":
        raise McpDependencyDegradedError(context={"reason": "health_degraded"})

    mcp = _mapping(payload.get("mcp"))
    if mcp.get("tool_input_max_chars") != expected_tool_input_max_chars:
        _raise_contract("tool_input_limit_mismatch")
    if mcp.get("auth_enabled") is not expected_auth_enabled:
        _raise_contract("auth_mode_mismatch")


def _validate_station_schema(
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
) -> None:
    properties = _mapping(schema.get("properties"))
    consumed_properties = {"station_id", "station_name", "line", "aliases", "confidence"}
    if not consumed_properties.issubset(properties):
        _raise_contract("resolve_station_station_properties_missing")
    if "station_name" not in _string_set(schema.get("required")):
        _raise_contract("resolve_station_station_name_not_required")
    _require_type(properties, "station_id", "string", root)
    _require_type(properties, "station_name", "string", root)
    _require_type(properties, "line", "string", root)
    _require_string_array(properties, "aliases", root)
    _require_type(properties, "confidence", "number", root)


def _validate_mobility_profile_schema(
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
) -> None:
    properties = _mapping(schema.get("properties"))
    consumed_properties = _MOBILITY_BOOLEAN_FIELDS | {
        "accessible_restroom_requirement",
        "max_transfer_count",
    }
    if not consumed_properties.issubset(properties):
        _raise_contract("mobility_profile_properties_missing")
    for field in sorted(_MOBILITY_BOOLEAN_FIELDS):
        _require_type(properties, field, "boolean", root)
    _require_enum(
        properties,
        "accessible_restroom_requirement",
        _RESTROOM_REQUIREMENTS,
        root,
    )
    max_transfer_count = _mapping(properties.get("max_transfer_count"))
    if _find_type(max_transfer_count, "integer", root) is None:
        _raise_contract("output_max_transfer_count_not_integer")
    if _find_type(max_transfer_count, "null", root) is None:
        _raise_contract("output_max_transfer_count_not_nullable")


def _validate_accessibility_result_schema(
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    validate_mobility_profile: bool,
) -> None:
    properties = _mapping(schema.get("properties"))
    consumed_properties = {
        "status",
        "origin",
        "destination",
        "mobility_profile",
        "risk_level",
        "risk_score",
        "route_summary",
        "failed_sources",
        "limitations",
        "confidence_level",
        "confidence_reasons",
        "last_checked_at",
        "evidence_sources",
        "unverified_parts",
        "clarification_needed",
        "questions",
        "available_partial_info",
        "safety_notice",
        "user_message",
    }
    if not consumed_properties.issubset(properties):
        _raise_contract("accessibility_result_properties_missing")
    _require_enum(properties, "status", _RESPONSE_STATUSES, root)
    _require_type(properties, "origin", "string", root)
    _require_type(properties, "destination", "string", root)
    profile_schema = _require_object(properties, "mobility_profile", root)
    if validate_mobility_profile:
        _validate_mobility_profile_schema(profile_schema, root)
    _require_enum(properties, "risk_level", _RISK_LEVELS, root)
    _require_type(properties, "risk_score", "integer", root)
    _require_type(properties, "route_summary", "string", root)
    _require_type(properties, "failed_sources", "array", root)
    _require_string_array(properties, "limitations", root)
    _require_enum(properties, "confidence_level", _CONFIDENCE_LEVELS, root)
    _require_string_array(properties, "confidence_reasons", root)
    _require_type(properties, "last_checked_at", "string", root)
    _require_type(properties, "evidence_sources", "array", root)
    _require_string_array(properties, "unverified_parts", root)
    _require_type(properties, "clarification_needed", "boolean", root)
    _require_string_array(properties, "questions", root)
    _require_string_array(properties, "available_partial_info", root)
    _require_type(properties, "safety_notice", "string", root)
    _require_type(properties, "user_message", "string", root)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return value


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _require_type(
    properties: Mapping[str, Any],
    field: str,
    expected_type: str,
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    schema = _mapping(properties.get(field))
    resolved = _find_type(schema, expected_type, root)
    if resolved is None:
        _raise_contract(f"output_{field}_not_{expected_type}")
    return resolved


def _require_object(
    properties: Mapping[str, Any],
    field: str,
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _require_type(properties, field, "object", root)


def _require_enum(
    properties: Mapping[str, Any],
    field: str,
    expected_values: set[str],
    root: Mapping[str, Any],
) -> None:
    schema = _mapping(properties.get(field))
    resolved = _find_type(schema, "string", root)
    if resolved is None:
        _raise_contract(f"output_{field}_not_string")
    actual_values = _string_set(resolved.get("enum"))
    if not expected_values.issubset(actual_values):
        _raise_contract(f"output_{field}_enum_incompatible")


def _require_string_array(
    properties: Mapping[str, Any],
    field: str,
    root: Mapping[str, Any],
) -> None:
    array_schema = _require_type(properties, field, "array", root)
    item_schema = _mapping(array_schema.get("items"))
    if _find_type(item_schema, "string", root) is None:
        _raise_contract(f"output_{field}_items_not_string")


def _require_array_items_object(
    properties: Mapping[str, Any],
    field: str,
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    array_schema = _require_type(properties, field, "array", root)
    item_schema = _mapping(array_schema.get("items"))
    resolved = _find_type(item_schema, "object", root)
    if resolved is None:
        _raise_contract(f"output_{field}_items_not_object")
    return resolved


def _require_nullable_type(
    properties: Mapping[str, Any],
    field: str,
    expected_type: str,
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    schema = _mapping(properties.get(field))
    resolved = _find_type(schema, expected_type, root)
    if resolved is None:
        _raise_contract(f"output_{field}_not_{expected_type}")
    if _find_type(schema, "null", root) is None:
        _raise_contract(f"output_{field}_not_nullable")
    return resolved


def _require_exact_enum(
    properties: Mapping[str, Any],
    field: str,
    expected_values: set[str],
    root: Mapping[str, Any],
) -> None:
    schema = _mapping(properties.get(field))
    resolved = _find_type(schema, "string", root)
    if resolved is None:
        _raise_contract(f"output_{field}_not_string")
    if _string_set(resolved.get("enum")) != expected_values:
        _raise_contract(f"output_{field}_enum_incompatible")


def _require_nullable_enum(
    properties: Mapping[str, Any],
    field: str,
    expected_values: set[str],
    root: Mapping[str, Any],
) -> None:
    _require_nullable_type(properties, field, "string", root)
    _require_exact_enum(properties, field, expected_values, root)


def _require_enum_array(
    properties: Mapping[str, Any],
    field: str,
    expected_values: set[str],
    root: Mapping[str, Any],
) -> None:
    array_schema = _require_type(properties, field, "array", root)
    item_schema = _mapping(array_schema.get("items"))
    resolved = _find_type(item_schema, "string", root)
    if resolved is None:
        _raise_contract(f"output_{field}_items_not_string")
    if _string_set(resolved.get("enum")) != expected_values:
        _raise_contract(f"output_{field}_enum_incompatible")


def _find_type(
    schema: Mapping[str, Any],
    expected_type: str,
    root: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    resolved = _resolve_reference(schema, root)
    if resolved.get("type") == expected_type:
        return resolved
    for keyword in ("anyOf", "oneOf"):
        alternatives = resolved.get(keyword)
        if not isinstance(alternatives, list):
            continue
        for alternative in alternatives:
            candidate = _find_type(_mapping(alternative), expected_type, root)
            if candidate is not None:
                return candidate
    return None


def _resolve_reference(
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        return {}
    definition = reference.removeprefix(prefix)
    return _mapping(_mapping(root.get("$defs")).get(definition))


def _raise_contract(reason: str) -> Never:
    raise McpContractError(
        detail=f"Incompatible MCP contract: {reason}",
        context={"reason": reason},
    )
