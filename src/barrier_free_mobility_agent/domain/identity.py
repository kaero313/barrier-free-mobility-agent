from __future__ import annotations

from typing import NewType

OwnerId = NewType("OwnerId", str)

# Disabled authentication is intentionally a single trust boundary for local and test runs.
# The non-hex value can never collide with an owner accepted from the trusted BFF headers.
LOCAL_OWNER_ID = OwnerId("local-test-owner")
