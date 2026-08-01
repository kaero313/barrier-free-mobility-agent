from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import parse_qs, urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_MIN_API_OWNER_SHARED_SECRET_BYTES = 32
_MAX_API_OWNER_SHARED_SECRET_BYTES = 4096
_MIN_IDEMPOTENCY_HMAC_KEY_BYTES = 32
_MAX_IDEMPOTENCY_HMAC_KEY_BYTES = 4096


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ApiOwnerMode(StrEnum):
    DISABLED = "disabled"
    TRUSTED_HEADER = "trusted_header"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class McpAuthMode(StrEnum):
    NONE = "none"
    STATIC = "static"
    OIDC = "oidc"


class PersistenceBackend(StrEnum):
    MEMORY = "memory"
    POSTGRES = "postgres"


class Settings(BaseSettings):
    app_env: AppEnvironment = AppEnvironment.LOCAL
    app_name: str = "Barrier-Free Mobility Agent"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_request_max_chars: int = Field(default=2000, ge=1, le=20_000)
    api_owner_mode: ApiOwnerMode = ApiOwnerMode.DISABLED
    api_owner_shared_secret: SecretStr | None = None

    agent_flow_version: str = Field(
        default="mobility-v2",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    agent_state_schema_version: str = Field(
        default="3",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    agent_state_max_bytes: int = Field(default=65_536, ge=1024, le=65_536)
    agent_max_graph_steps: int = Field(default=12, ge=4, le=100)
    agent_max_tool_calls: int = Field(default=6, ge=1, le=50)
    agent_max_model_calls: int = Field(default=2, ge=0, le=20)
    agent_max_parallel_tools: int = Field(default=4, ge=1, le=20)
    agent_max_clarification_cycles: int = Field(default=2, ge=1, le=10)
    agent_max_threads_per_owner: int = Field(default=1000, ge=1, le=100_000)
    agent_max_total_threads: int = Field(default=10_000, ge=1, le=1_000_000)
    agent_max_runs_per_thread: int = Field(default=100, ge=1, le=10_000)

    persistence_backend: PersistenceBackend = PersistenceBackend.MEMORY
    database_url: SecretStr | None = None
    database_pool_min_size: int = Field(default=1, ge=1, le=20)
    database_pool_max_size: int = Field(default=4, ge=1, le=100)
    database_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    database_ready_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    database_auto_setup: bool = True
    database_at_rest_encryption_confirmed: bool = False
    checkpoint_encryption_key: SecretStr | None = None
    execution_payload_encryption_key: SecretStr | None = None
    execution_payload_previous_encryption_keys: tuple[SecretStr, ...] = ()
    idempotency_hmac_key: SecretStr | None = None
    execution_bounded_wait_seconds: float = Field(default=60.0, ge=0, le=120)
    execution_poll_interval_seconds: float = Field(default=0.05, gt=0, le=5)
    execution_worker_id: str = Field(
        default="worker-1",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    execution_worker_lease_seconds: float = Field(default=30.0, gt=0, le=300)
    execution_worker_heartbeat_seconds: float = Field(default=5.0, gt=0, le=60)
    execution_worker_readiness_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    execution_worker_poll_seconds: float = Field(default=0.25, gt=0, le=10)
    execution_worker_max_error_backoff_seconds: float = Field(default=5.0, gt=0, le=60)
    execution_worker_shutdown_grace_seconds: float = Field(default=30.0, gt=0, le=300)

    mcp_mobility_url: str = "http://127.0.0.1:8000/mcp"
    mcp_mobility_health_url: str = "http://127.0.0.1:8000/health"
    mcp_mobility_auth_mode: McpAuthMode = McpAuthMode.NONE
    mcp_mobility_bearer_token: SecretStr | None = None
    mcp_mobility_oidc_token_url: str | None = None
    mcp_mobility_oidc_client_id: str | None = None
    mcp_mobility_oidc_client_secret: SecretStr | None = None
    mcp_mobility_oidc_scope: str | None = None
    mcp_mobility_oidc_refresh_leeway_seconds: float = Field(default=30.0, gt=0, le=300)
    mcp_tool_input_max_chars: int = Field(default=120, ge=1, le=10_000)
    mcp_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    mcp_tool_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    mcp_startup_required: bool = False
    mcp_contract_refresh_seconds: float = Field(default=30.0, gt=0, le=3600)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        validate_default=True,
    )

    @model_validator(mode="after")
    def validate_mcp_auth(self) -> Self:
        _validate_http_url("MCP_MOBILITY_URL", self.mcp_mobility_url)
        _validate_http_url("MCP_MOBILITY_HEALTH_URL", self.mcp_mobility_health_url)
        if self.mcp_mobility_auth_mode == McpAuthMode.STATIC:
            token = self.mcp_mobility_bearer_token
            if token is None or not token.get_secret_value().strip():
                raise ValueError(
                    "MCP_MOBILITY_AUTH_MODE=static requires MCP_MOBILITY_BEARER_TOKEN."
                )
        if self.mcp_mobility_auth_mode == McpAuthMode.OIDC:
            token_url = _require_text(
                "MCP_MOBILITY_OIDC_TOKEN_URL", self.mcp_mobility_oidc_token_url
            )
            _validate_http_url("MCP_MOBILITY_OIDC_TOKEN_URL", token_url)
            client_id = _require_text(
                "MCP_MOBILITY_OIDC_CLIENT_ID", self.mcp_mobility_oidc_client_id
            )
            secret = self.mcp_mobility_oidc_client_secret
            if secret is None or not secret.get_secret_value().strip():
                raise ValueError(
                    "MCP_MOBILITY_AUTH_MODE=oidc requires MCP_MOBILITY_OIDC_CLIENT_SECRET."
                )
            self.mcp_mobility_oidc_token_url = token_url
            self.mcp_mobility_oidc_client_id = client_id
        return self

    @model_validator(mode="after")
    def validate_api_owner_auth(self) -> Self:
        hosted = self.app_env in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}
        if hosted and self.api_owner_mode != ApiOwnerMode.TRUSTED_HEADER:
            raise ValueError(
                "APP_ENV=staging or production requires API_OWNER_MODE=trusted_header."
            )

        if self.api_owner_mode == ApiOwnerMode.TRUSTED_HEADER:
            configured_secret = self.api_owner_shared_secret
            if configured_secret is None:
                raise ValueError("API_OWNER_MODE=trusted_header requires API_OWNER_SHARED_SECRET.")
            secret = configured_secret.get_secret_value()
            secret_bytes = secret.encode("utf-8")
            if not (
                _MIN_API_OWNER_SHARED_SECRET_BYTES
                <= len(secret_bytes)
                <= _MAX_API_OWNER_SHARED_SECRET_BYTES
            ):
                raise ValueError(
                    "API_OWNER_SHARED_SECRET must contain between 32 and 4096 UTF-8 bytes."
                )
            if any(not 0x21 <= ord(char) <= 0x7E for char in secret):
                raise ValueError(
                    "API_OWNER_SHARED_SECRET must contain only visible ASCII characters."
                )
        return self

    @model_validator(mode="after")
    def validate_persistence(self) -> Self:
        hosted = self.app_env in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}
        if hosted and self.persistence_backend != PersistenceBackend.POSTGRES:
            raise ValueError("APP_ENV=staging or production requires PERSISTENCE_BACKEND=postgres.")
        if self.database_pool_min_size > self.database_pool_max_size:
            raise ValueError(
                "DATABASE_POOL_MIN_SIZE must be less than or equal to DATABASE_POOL_MAX_SIZE."
            )
        if self.agent_max_threads_per_owner > self.agent_max_total_threads:
            raise ValueError(
                "AGENT_MAX_THREADS_PER_OWNER must be less than or equal to AGENT_MAX_TOTAL_THREADS."
            )
        if self.persistence_backend == PersistenceBackend.POSTGRES:
            database_url = self.database_url
            if database_url is None:
                raise ValueError("PERSISTENCE_BACKEND=postgres requires DATABASE_URL.")
            _validate_database_url(database_url)
            if self.database_pool_max_size < 2:
                raise ValueError(
                    "PERSISTENCE_BACKEND=postgres requires DATABASE_POOL_MAX_SIZE>=2 "
                    "for lease-fenced checkpoint writes."
                )
        encryption_key = self.checkpoint_encryption_key
        if hosted and encryption_key is None:
            raise ValueError("APP_ENV=staging or production requires CHECKPOINT_ENCRYPTION_KEY.")
        if encryption_key is not None and len(
            encryption_key.get_secret_value().encode("utf-8")
        ) not in {16, 24, 32}:
            raise ValueError(
                "CHECKPOINT_ENCRYPTION_KEY must contain exactly 16, 24, or 32 UTF-8 bytes."
            )
        payload_key = self.execution_payload_encryption_key
        previous_payload_keys = self.execution_payload_previous_encryption_keys
        if hosted and payload_key is None:
            raise ValueError(
                "APP_ENV=staging or production requires EXECUTION_PAYLOAD_ENCRYPTION_KEY."
            )
        if payload_key is None and previous_payload_keys:
            raise ValueError(
                "EXECUTION_PAYLOAD_PREVIOUS_ENCRYPTION_KEYS requires "
                "EXECUTION_PAYLOAD_ENCRYPTION_KEY."
            )
        payload_key_values = (
            () if payload_key is None else (payload_key.get_secret_value(),)
        ) + tuple(key.get_secret_value() for key in previous_payload_keys)
        if any(len(key.encode("utf-8")) != 32 for key in payload_key_values):
            raise ValueError(
                "Execution payload encryption keys must contain exactly 32 UTF-8 bytes."
            )
        if len(set(payload_key_values)) != len(payload_key_values):
            raise ValueError("Execution payload encryption keys must be distinct.")
        idempotency_key = self.idempotency_hmac_key
        if (payload_key is None) != (idempotency_key is None):
            raise ValueError(
                "EXECUTION_PAYLOAD_ENCRYPTION_KEY and IDEMPOTENCY_HMAC_KEY "
                "must be configured together."
            )
        if hosted and idempotency_key is None:
            raise ValueError("APP_ENV=staging or production requires IDEMPOTENCY_HMAC_KEY.")
        if idempotency_key is not None and not (
            _MIN_IDEMPOTENCY_HMAC_KEY_BYTES
            <= len(idempotency_key.get_secret_value().encode("utf-8"))
            <= _MAX_IDEMPOTENCY_HMAC_KEY_BYTES
        ):
            raise ValueError("IDEMPOTENCY_HMAC_KEY must contain between 32 and 4096 UTF-8 bytes.")
        if idempotency_key is not None and idempotency_key.get_secret_value() in payload_key_values:
            raise ValueError(
                "IDEMPOTENCY_HMAC_KEY must be distinct from all execution payload encryption keys."
            )
        if self.execution_worker_heartbeat_seconds * 2 >= self.execution_worker_lease_seconds:
            raise ValueError(
                "EXECUTION_WORKER_HEARTBEAT_SECONDS must be less than half "
                "EXECUTION_WORKER_LEASE_SECONDS."
            )
        if (
            self.execution_worker_heartbeat_seconds * 2
            >= self.execution_worker_readiness_timeout_seconds
        ):
            raise ValueError(
                "EXECUTION_WORKER_HEARTBEAT_SECONDS must be less than half "
                "EXECUTION_WORKER_READINESS_TIMEOUT_SECONDS."
            )
        if hosted:
            if not self.database_at_rest_encryption_confirmed:
                raise ValueError(
                    "APP_ENV=staging or production requires "
                    "DATABASE_AT_REST_ENCRYPTION_CONFIRMED=true."
                )
            database_url = self.database_url
            if database_url is None or not _database_uses_verified_tls(database_url):
                raise ValueError("Hosted DATABASE_URL requires sslmode=verify-full.")
        return self

    @property
    def mcp_headers(self) -> dict[str, str]:
        if self.mcp_mobility_auth_mode != McpAuthMode.STATIC:
            return {}
        token = self.mcp_mobility_bearer_token
        if token is None:
            return {}
        return {"Authorization": f"Bearer {token.get_secret_value()}"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _validate_http_url(name: str, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HTTP_HOSTS:
        raise ValueError(f"{name} must use HTTPS outside local development hosts.")


def _validate_database_url(value: SecretStr) -> None:
    parsed = urlparse(value.get_secret_value())
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise ValueError("DATABASE_URL must be an absolute PostgreSQL URL.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("DATABASE_URL must select a database.")


def _database_uses_verified_tls(value: SecretStr) -> bool:
    parsed = urlparse(value.get_secret_value())
    return parse_qs(parsed.query).get("sslmode") == ["verify-full"]


def _require_text(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError(f"MCP_MOBILITY_AUTH_MODE=oidc requires {name}.")
    return value.strip()
