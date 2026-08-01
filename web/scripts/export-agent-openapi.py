from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "web" / "openapi" / "agent.openapi.json"
sys.path.insert(0, str(ROOT / "src"))

from barrier_free_mobility_agent.api.app import create_app  # noqa: E402
from barrier_free_mobility_agent.config import (  # noqa: E402
    AppEnvironment,
    McpAuthMode,
    Settings,
)

REQUIRED_OPERATIONS = {
    "/v1/threads": {"post"},
    "/v1/threads/{thread_id}": {"delete"},
    "/v1/threads/{thread_id}/runs": {"post"},
    "/v1/runs/{run_id}": {"get"},
    "/v1/runs/{run_id}/resume": {"post"},
    "/health/ready": {"get"},
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export or verify the Agent OpenAPI snapshot consumed by the Web BFF."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the checked-in snapshot differs from the current FastAPI schema.",
    )
    args = parser.parse_args()

    document = _openapi_document()
    _validate_bff_surface(document)
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if not args.check:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"Wrote {SNAPSHOT.relative_to(ROOT)}")
        return 0

    if not SNAPSHOT.exists():
        print(
            "Agent OpenAPI snapshot is missing. Run "
            "`uv run python web/scripts/export-agent-openapi.py`.",
            file=sys.stderr,
        )
        return 1
    current = SNAPSHOT.read_text(encoding="utf-8")
    if current == rendered:
        print("Agent OpenAPI snapshot is current.")
        return 0

    print(
        "Agent OpenAPI drift detected. Regenerate the snapshot and review the Web contract.",
        file=sys.stderr,
    )
    diff = difflib.unified_diff(
        current.splitlines(),
        rendered.splitlines(),
        fromfile=str(SNAPSHOT.relative_to(ROOT)),
        tofile="generated Agent OpenAPI",
        lineterm="",
    )
    for line in list(diff)[:200]:
        print(line, file=sys.stderr)
    return 1


def _openapi_document() -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        app_name="Barrier-Free Mobility Agent",
        app_version="0.1.0",
        mcp_mobility_auth_mode=McpAuthMode.NONE,
        mcp_mobility_url="http://127.0.0.1:8000/mcp",
        mcp_mobility_health_url="http://127.0.0.1:8000/health",
        mcp_startup_required=False,
    )
    return create_app(settings=settings).openapi()


def _validate_bff_surface(document: dict[str, Any]) -> None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SystemExit("Generated OpenAPI does not contain a paths object.")
    for path, methods in REQUIRED_OPERATIONS.items():
        operation = paths.get(path)
        if not isinstance(operation, dict):
            raise SystemExit(f"Required BFF upstream path is missing: {path}")
        missing = methods.difference(operation)
        if missing:
            raise SystemExit(f"Required operation is missing for {path}: {sorted(missing)}")

    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    run_schema = schemas.get("ConversationRunResponse") if isinstance(schemas, dict) else None
    if not isinstance(run_schema, dict):
        raise SystemExit("ConversationRunResponse schema is missing from Agent OpenAPI.")
    properties = run_schema.get("properties")
    version = properties.get("schema_version") if isinstance(properties, dict) else None
    if not isinstance(version, dict) or version.get("const") != "3":
        raise SystemExit("The compatibility response must retain schema_version=3.")

    run_schema_v4 = schemas.get("ConversationRunResponseV4")
    if not isinstance(run_schema_v4, dict):
        raise SystemExit("ConversationRunResponseV4 schema is missing from Agent OpenAPI.")
    v4_properties = run_schema_v4.get("properties")
    v4_version = v4_properties.get("schema_version") if isinstance(v4_properties, dict) else None
    if not isinstance(v4_version, dict) or v4_version.get("const") != "4":
        raise SystemExit("The Web BFF requires ConversationRunResponseV4 schema_version=4.")
    if "StructuredClarificationView" not in schemas:
        raise SystemExit("StructuredClarificationView is missing from Agent OpenAPI.")

    _validate_public_projection_schemas(schemas)


def _validate_public_projection_schemas(schemas: dict[str, Any]) -> None:
    error_response = _closed_object_schema(schemas, "ErrorResponse")
    error_response_properties = _properties(error_response, "ErrorResponse")
    error_version = error_response_properties.get("schema_version")
    if not isinstance(error_version, dict) or error_version.get("const") != "2":
        raise SystemExit("The Web BFF requires ErrorResponse schema_version=2.")

    error_body = _closed_object_schema(schemas, "ErrorBody")
    error_properties = _properties(error_body, "ErrorBody")
    code_schema = error_properties.get("code")
    message_schema = error_properties.get("message")
    if not isinstance(code_schema, dict) or (
        code_schema.get("pattern") != "^[A-Z][A-Z0-9_]*$"
        or code_schema.get("minLength") != 1
        or code_schema.get("maxLength") != 100
    ):
        raise SystemExit("ErrorBody.code must remain a bounded public error code.")
    if not isinstance(message_schema, dict) or (
        message_schema.get("minLength") != 1 or message_schema.get("maxLength") != 500
    ):
        raise SystemExit("ErrorBody.message must remain bounded.")

    detail_schema = error_properties.get("detail")
    detail_variants = detail_schema.get("anyOf") if isinstance(detail_schema, dict) else None
    if (
        not isinstance(detail_variants, list)
        or {variant.get("$ref") for variant in detail_variants if isinstance(variant, dict)}
        != {
            "#/components/schemas/EmptyErrorDetail",
            "#/components/schemas/RequestValidationErrorDetail",
        }
        or len(detail_variants) != 2
    ):
        raise SystemExit(
            "ErrorBody.detail may contain only empty or bounded request-validation detail."
        )

    _closed_object_schema(schemas, "EmptyErrorDetail")
    validation_detail = _closed_object_schema(schemas, "RequestValidationErrorDetail")
    validation_detail_properties = _properties(validation_detail, "RequestValidationErrorDetail")
    issues_schema = validation_detail_properties.get("issues")
    if not isinstance(issues_schema, dict) or (
        issues_schema.get("minItems") != 1
        or issues_schema.get("maxItems") != 32
        or issues_schema.get("items") != {"$ref": "#/components/schemas/RequestValidationIssue"}
    ):
        raise SystemExit("Request validation issues must remain typed and bounded to 32 items.")

    validation_issue = _closed_object_schema(schemas, "RequestValidationIssue")
    validation_issue_properties = _properties(validation_issue, "RequestValidationIssue")
    issue_type = validation_issue_properties.get("type")
    issue_location = validation_issue_properties.get("location")
    issue_message = validation_issue_properties.get("message")
    if not isinstance(issue_type, dict) or (
        issue_type.get("minLength") != 1 or issue_type.get("maxLength") != 100
    ):
        raise SystemExit("RequestValidationIssue.type must remain bounded.")
    if not isinstance(issue_location, dict) or (
        issue_location.get("minItems") != 1 or issue_location.get("maxItems") != 16
    ):
        raise SystemExit("RequestValidationIssue.location must remain bounded.")
    location_item = issue_location.get("items") if isinstance(issue_location, dict) else None
    if not isinstance(location_item, dict) or (
        location_item.get("minLength") != 1 or location_item.get("maxLength") != 100
    ):
        raise SystemExit("RequestValidationIssue.location items must remain bounded.")
    if not isinstance(issue_message, dict) or (
        issue_message.get("minLength") != 1 or issue_message.get("maxLength") != 300
    ):
        raise SystemExit("RequestValidationIssue.message must remain bounded.")

    failed_source = _closed_object_schema(schemas, "FailedSourceSummary")
    failed_source_properties = _properties(failed_source, "FailedSourceSummary")
    expected_failed_source_fields = {
        "source_name",
        "display_name",
        "recoverable",
    }
    if (
        set(failed_source.get("required", [])) != expected_failed_source_fields
        or set(failed_source_properties) != expected_failed_source_fields
    ):
        raise SystemExit("FailedSourceSummary must require only its public projection fields.")
    if failed_source_properties.get("source_name") != {
        "$ref": "#/components/schemas/FailedSourceCode"
    }:
        raise SystemExit("FailedSourceSummary.source_name must use the stable public code enum.")
    display_name = failed_source_properties.get("display_name")
    if not isinstance(display_name, dict) or (
        display_name.get("minLength") != 1 or display_name.get("maxLength") != 80
    ):
        raise SystemExit("FailedSourceSummary.display_name must remain bounded.")

    failed_source_code = schemas.get("FailedSourceCode")
    expected_codes = {
        "route_information",
        "station_information",
        "facility_information",
        "mobility_information",
    }
    if (
        not isinstance(failed_source_code, dict)
        or set(failed_source_code.get("enum", [])) != expected_codes
    ):
        raise SystemExit("FailedSourceCode must contain only the stable public source codes.")


def _closed_object_schema(schemas: dict[str, Any], name: str) -> dict[str, Any]:
    schema = schemas.get(name)
    if not isinstance(schema, dict):
        raise SystemExit(f"{name} schema is missing from Agent OpenAPI.")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise SystemExit(f"{name} must remain a closed object schema.")
    return schema


def _properties(schema: dict[str, Any], name: str) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise SystemExit(f"{name} does not contain a properties object.")
    return properties


if __name__ == "__main__":
    raise SystemExit(main())
