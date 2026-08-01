from __future__ import annotations

import re
import secrets
from typing import Annotated, cast

from fastapi import Depends, Request

from barrier_free_mobility_agent.api.container import ApplicationContainer
from barrier_free_mobility_agent.config import ApiOwnerMode
from barrier_free_mobility_agent.domain.errors import OwnerAuthenticationError
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId

OWNER_ID_HEADER = "X-Agent-Owner-ID"
INTERNAL_TOKEN_HEADER = "X-Agent-Internal-Token"
_OWNER_ID_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def get_container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def get_request_id(request: Request) -> str:
    return cast(str, request.state.request_id)


ContainerDependency = Annotated[ApplicationContainer, Depends(get_container)]
RequestIdDependency = Annotated[str, Depends(get_request_id)]


def get_owner_id(
    request: Request,
    container: ContainerDependency,
) -> OwnerId:
    settings = container.settings
    if settings.api_owner_mode == ApiOwnerMode.DISABLED:
        return LOCAL_OWNER_ID

    owner_values = request.headers.getlist(OWNER_ID_HEADER)
    token_values = request.headers.getlist(INTERNAL_TOKEN_HEADER)
    if len(owner_values) != 1 or len(token_values) != 1:
        raise OwnerAuthenticationError()

    owner_value = owner_values[0]
    configured_secret = settings.api_owner_shared_secret
    if configured_secret is None:
        raise OwnerAuthenticationError()

    try:
        provided_token = token_values[0].encode("utf-8")
        expected_token = configured_secret.get_secret_value().encode("utf-8")
    except UnicodeError as exc:
        raise OwnerAuthenticationError() from exc

    token_is_valid = secrets.compare_digest(provided_token, expected_token)
    if _OWNER_ID_PATTERN.fullmatch(owner_value) is None or not token_is_valid:
        raise OwnerAuthenticationError()
    return OwnerId(owner_value)


OwnerDependency = Annotated[OwnerId, Depends(get_owner_id)]
