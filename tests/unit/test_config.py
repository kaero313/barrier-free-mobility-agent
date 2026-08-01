from __future__ import annotations

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.config import (
    ApiOwnerMode,
    AppEnvironment,
    McpAuthMode,
    PersistenceBackend,
    Settings,
)

_VALID_OWNER_SHARED_SECRET = "owner-shared-secret-0123456789abcdef"


def test_default_settings_use_local_mcp() -> None:
    settings = Settings(_env_file=None)

    assert settings.api_port == 8080
    assert settings.mcp_mobility_url == "http://127.0.0.1:8000/mcp"
    assert settings.mcp_tool_input_max_chars == 120
    assert settings.mcp_headers == {}
    assert settings.agent_flow_version == "mobility-v2"
    assert settings.agent_state_schema_version == "3"
    assert settings.agent_state_max_bytes == 65_536
    assert settings.agent_max_graph_steps == 12
    assert settings.agent_max_tool_calls == 6
    assert settings.agent_max_clarification_cycles == 2
    assert settings.agent_max_model_calls == 2
    assert settings.agent_max_parallel_tools == 4
    assert settings.agent_max_threads_per_owner == 1000
    assert settings.agent_max_total_threads == 10_000
    assert settings.agent_max_runs_per_thread == 100
    assert settings.api_owner_mode == ApiOwnerMode.DISABLED
    assert settings.api_owner_shared_secret is None
    assert settings.execution_bounded_wait_seconds == 60.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_flow_version", ""),
        ("agent_flow_version", "x" * 101),
        ("agent_state_schema_version", "schema version"),
        ("agent_state_schema_version", "3\x00"),
    ],
)
def test_agent_versions_fail_fast_before_thread_creation(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1023, 65_537])
def test_agent_state_byte_budget_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_state_max_bytes=value)


@pytest.mark.parametrize("app_env", [AppEnvironment.LOCAL, AppEnvironment.TEST])
@pytest.mark.parametrize("owner_mode", list(ApiOwnerMode))
def test_local_and_test_allow_both_owner_modes(
    app_env: AppEnvironment,
    owner_mode: ApiOwnerMode,
) -> None:
    values: dict[str, object] = {
        "app_env": app_env,
        "api_owner_mode": owner_mode,
    }
    if owner_mode == ApiOwnerMode.TRUSTED_HEADER:
        values["api_owner_shared_secret"] = _VALID_OWNER_SHARED_SECRET

    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]

    assert settings.api_owner_mode == owner_mode


@pytest.mark.parametrize("app_env", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
def test_hosted_environments_require_trusted_owner_headers(
    app_env: AppEnvironment,
) -> None:
    with pytest.raises(ValidationError, match="API_OWNER_MODE=trusted_header"):
        Settings(
            _env_file=None,
            app_env=app_env,
            api_owner_mode=ApiOwnerMode.DISABLED,
        )


@pytest.mark.parametrize("app_env", list(AppEnvironment))
def test_trusted_owner_headers_require_shared_secret(app_env: AppEnvironment) -> None:
    with pytest.raises(ValidationError, match="API_OWNER_SHARED_SECRET"):
        Settings(
            _env_file=None,
            app_env=app_env,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
        )


def test_owner_shared_secret_accepts_visible_ascii_boundary_and_is_redacted() -> None:
    secret = "x" * 32
    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.LOCAL,
        api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
        api_owner_shared_secret=secret,
    )

    assert len(secret.encode("utf-8")) == 32
    assert settings.api_owner_shared_secret is not None
    assert settings.api_owner_shared_secret.get_secret_value() == secret
    assert secret not in repr(settings)


@pytest.mark.parametrize("secret", ["x" * 31, "x" * 4097])
def test_owner_shared_secret_rejects_out_of_range_utf8_length_without_leaking(
    secret: str,
) -> None:

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=secret,
        )

    assert "between 32 and 4096 UTF-8 bytes" in str(exc_info.value)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "secret",
    [
        "x" * 31 + " ",
        "x" * 31 + "\n",
        "가" * 32,
    ],
)
def test_owner_shared_secret_rejects_non_visible_ascii(secret: str) -> None:
    with pytest.raises(ValidationError, match="visible ASCII"):
        Settings(
            _env_file=None,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=secret,
        )


def test_static_auth_requires_token() -> None:
    with pytest.raises(ValidationError, match="MCP_MOBILITY_BEARER_TOKEN"):
        Settings(_env_file=None, mcp_mobility_auth_mode=McpAuthMode.STATIC)


def test_static_auth_builds_bearer_header() -> None:
    settings = Settings(
        _env_file=None,
        mcp_mobility_auth_mode=McpAuthMode.STATIC,
        mcp_mobility_bearer_token="secret-token",
    )

    assert settings.mcp_headers == {"Authorization": "Bearer secret-token"}
    assert "secret-token" not in repr(settings)


def test_postgres_persistence_requires_a_database_url() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            _env_file=None,
            persistence_backend=PersistenceBackend.POSTGRES,
        )


def test_postgres_database_url_is_validated_and_redacted() -> None:
    database_url = "postgresql://mobility:super-secret@database:5432/mobility"
    settings = Settings(
        _env_file=None,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url=database_url,
    )

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == database_url
    assert database_url not in repr(settings)


def test_invalid_database_url_does_not_leak_credentials() -> None:
    database_url = "https://mobility:super-secret@database.example/mobility"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            persistence_backend=PersistenceBackend.POSTGRES,
            database_url=database_url,
        )

    assert "absolute PostgreSQL URL" in str(exc_info.value)
    assert database_url not in str(exc_info.value)
    assert "super-secret" not in str(exc_info.value)


def test_database_pool_bounds_are_validated() -> None:
    with pytest.raises(ValidationError, match="DATABASE_POOL_MIN_SIZE"):
        Settings(
            _env_file=None,
            database_pool_min_size=5,
            database_pool_max_size=4,
        )


def test_postgres_requires_two_connections_for_lease_fenced_checkpoints() -> None:
    with pytest.raises(ValidationError, match="DATABASE_POOL_MAX_SIZE>=2"):
        Settings(
            _env_file=None,
            persistence_backend=PersistenceBackend.POSTGRES,
            database_url="postgresql://mobility:secret@database:5432/mobility",
            database_pool_min_size=1,
            database_pool_max_size=1,
        )


def test_owner_thread_capacity_cannot_exceed_the_global_capacity() -> None:
    with pytest.raises(ValidationError, match="AGENT_MAX_THREADS_PER_OWNER"):
        Settings(
            _env_file=None,
            agent_max_threads_per_owner=2,
            agent_max_total_threads=1,
        )


def test_hosted_environment_requires_durable_encrypted_persistence() -> None:
    owner_secret = "owner-shared-secret-0123456789abcdef"
    with pytest.raises(ValidationError, match="PERSISTENCE_BACKEND=postgres"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=owner_secret,
        )

    with pytest.raises(ValidationError, match="CHECKPOINT_ENCRYPTION_KEY"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=owner_secret,
            persistence_backend=PersistenceBackend.POSTGRES,
            database_url="postgresql://mobility:secret@database:5432/mobility",
        )

    with pytest.raises(ValidationError, match="AT_REST_ENCRYPTION"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=owner_secret,
            persistence_backend=PersistenceBackend.POSTGRES,
            database_url="postgresql://mobility:secret@database:5432/mobility",
            checkpoint_encryption_key="x" * 32,
            execution_payload_encryption_key="y" * 32,
            idempotency_hmac_key="z" * 32,
        )

    with pytest.raises(ValidationError, match="sslmode=verify-full"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
            api_owner_shared_secret=owner_secret,
            persistence_backend=PersistenceBackend.POSTGRES,
            database_url="postgresql://mobility:secret@database:5432/mobility",
            checkpoint_encryption_key="x" * 32,
            execution_payload_encryption_key="y" * 32,
            idempotency_hmac_key="z" * 32,
            database_at_rest_encryption_confirmed=True,
        )

    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.PRODUCTION,
        api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
        api_owner_shared_secret=owner_secret,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url=("postgresql://mobility:secret@database:5432/mobility?sslmode=verify-full"),
        checkpoint_encryption_key="x" * 32,
        execution_payload_encryption_key="y" * 32,
        idempotency_hmac_key="z" * 32,
        database_at_rest_encryption_confirmed=True,
    )
    assert settings.app_env == AppEnvironment.PRODUCTION


def test_execution_coordination_secrets_and_lease_cadence_are_validated() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(_env_file=None, execution_payload_encryption_key="p" * 32)
    with pytest.raises(ValidationError, match="IDEMPOTENCY_HMAC_KEY"):
        Settings(_env_file=None, idempotency_hmac_key="short")
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            _env_file=None,
            execution_payload_encryption_key="s" * 32,
            idempotency_hmac_key="s" * 32,
        )
    with pytest.raises(ValidationError, match="less than half"):
        Settings(
            _env_file=None,
            execution_worker_lease_seconds=10,
            execution_worker_heartbeat_seconds=5,
        )
    with pytest.raises(
        ValidationError,
        match="EXECUTION_WORKER_READINESS_TIMEOUT_SECONDS",
    ):
        Settings(
            _env_file=None,
            execution_worker_heartbeat_seconds=5,
            execution_worker_readiness_timeout_seconds=10,
        )

    settings = Settings(_env_file=None)
    assert settings.execution_worker_readiness_timeout_seconds == 15


def test_execution_payload_encryption_keyring_is_strict_and_redacted() -> None:
    active = "a" * 32
    previous = "b" * 32
    settings = Settings(
        _env_file=None,
        execution_payload_encryption_key=active,
        execution_payload_previous_encryption_keys=[previous],
        idempotency_hmac_key="h" * 32,
    )

    assert settings.execution_payload_encryption_key is not None
    assert len(settings.execution_payload_previous_encryption_keys) == 1
    assert active not in repr(settings)
    assert previous not in repr(settings)

    with pytest.raises(ValidationError, match="exactly 32"):
        Settings(_env_file=None, execution_payload_encryption_key="short")
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            _env_file=None,
            execution_payload_encryption_key=active,
            execution_payload_previous_encryption_keys=[active],
            idempotency_hmac_key="h" * 32,
        )


def test_execution_payload_previous_keys_parse_from_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXECUTION_PAYLOAD_ENCRYPTION_KEY", "a" * 32)
    monkeypatch.setenv("IDEMPOTENCY_HMAC_KEY", "h" * 32)
    monkeypatch.setenv(
        "EXECUTION_PAYLOAD_PREVIOUS_ENCRYPTION_KEYS",
        '["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]',
    )

    settings = Settings(_env_file=None)

    assert settings.execution_payload_encryption_key is not None
    assert settings.execution_payload_encryption_key.get_secret_value() == "a" * 32
    assert tuple(
        key.get_secret_value() for key in settings.execution_payload_previous_encryption_keys
    ) == ("b" * 32,)


@pytest.mark.parametrize("key", ["x" * 16, "한" * 8, "x" * 32])
def test_checkpoint_encryption_key_accepts_aes_byte_lengths(key: str) -> None:
    settings = Settings(_env_file=None, checkpoint_encryption_key=key)

    assert settings.checkpoint_encryption_key is not None
    assert settings.checkpoint_encryption_key.get_secret_value() == key
    assert key not in repr(settings)


@pytest.mark.parametrize(
    ("missing_field", "expected_name"),
    [
        ("mcp_mobility_oidc_token_url", "MCP_MOBILITY_OIDC_TOKEN_URL"),
        ("mcp_mobility_oidc_client_id", "MCP_MOBILITY_OIDC_CLIENT_ID"),
        ("mcp_mobility_oidc_client_secret", "MCP_MOBILITY_OIDC_CLIENT_SECRET"),
    ],
)
def test_oidc_auth_requires_all_credentials(
    missing_field: str,
    expected_name: str,
) -> None:
    values: dict[str, object] = {
        "mcp_mobility_auth_mode": McpAuthMode.OIDC,
        "mcp_mobility_oidc_token_url": "https://identity.example.com/oauth/token",
        "mcp_mobility_oidc_client_id": "mobility-agent",
        "mcp_mobility_oidc_client_secret": "client-secret",
    }
    values[missing_field] = None

    with pytest.raises(ValidationError, match=expected_name):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_oidc_auth_accepts_local_http_and_redacts_secret() -> None:
    settings = Settings(
        _env_file=None,
        mcp_mobility_auth_mode=McpAuthMode.OIDC,
        mcp_mobility_oidc_token_url="http://127.0.0.1:9000/oauth/token",
        mcp_mobility_oidc_client_id="mobility-agent",
        mcp_mobility_oidc_client_secret="client-secret",
    )

    assert settings.mcp_headers == {}
    assert "client-secret" not in repr(settings)


def test_oidc_remote_plain_http_token_url_is_rejected_without_secret_leak() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            mcp_mobility_auth_mode=McpAuthMode.OIDC,
            mcp_mobility_oidc_token_url="http://identity.example.com/oauth/token",
            mcp_mobility_oidc_client_id="mobility-agent",
            mcp_mobility_oidc_client_secret="client-secret",
        )

    assert "must use HTTPS" in str(exc_info.value)
    assert "client-secret" not in str(exc_info.value)


def test_oidc_token_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="must not contain embedded credentials"):
        Settings(
            _env_file=None,
            mcp_mobility_auth_mode=McpAuthMode.OIDC,
            mcp_mobility_oidc_token_url="https://user:password@identity.example.com/token",
            mcp_mobility_oidc_client_id="mobility-agent",
            mcp_mobility_oidc_client_secret="client-secret",
        )


def test_remote_plain_http_mcp_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            _env_file=None,
            mcp_mobility_url="http://example.com/mcp",
        )


def test_docker_desktop_host_is_allowed_for_local_compose() -> None:
    settings = Settings(
        _env_file=None,
        mcp_mobility_url="http://host.docker.internal:8000/mcp",
        mcp_mobility_health_url="http://host.docker.internal:8000/health",
    )

    assert settings.mcp_mobility_url.startswith("http://host.docker.internal")
