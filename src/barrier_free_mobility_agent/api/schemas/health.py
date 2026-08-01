from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from barrier_free_mobility_agent.domain.models import ContractStatus, McpCapability


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str


class McpReadiness(BaseModel):
    status: ContractStatus
    checked_at: datetime | None
    tool_count: int
    capabilities: tuple[McpCapability, ...]
    schema_fingerprint: str | None
    error_code: str | None


class PersistenceReadiness(BaseModel):
    status: Literal["ready", "unavailable"]
    backend: Literal["memory", "postgres"]


class ReadinessDependencies(BaseModel):
    mobility_mcp: McpReadiness
    persistence: PersistenceReadiness


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str
    version: str
    dependencies: ReadinessDependencies
