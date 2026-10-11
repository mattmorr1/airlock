import hmac
import os
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated

from fastapi import Header

from api.errors import ApiError


class Role(StrEnum):
    OPERATOR = "operator"
    BRIDGE = "bridge"
    HOOK = "hook"
    READER = "reader"


class Cap(StrEnum):
    PROJECT_CREATE = "project:create"
    TASK_CREATE = "task:create"
    TASK_APPROVE = "task:approve"
    ROLLBACK = "project:rollback"
    REVOKE = "project:revoke"
    READ = "read"


CAPS: dict[Role, frozenset[Cap]] = {
    Role.OPERATOR: frozenset({Cap.PROJECT_CREATE, Cap.TASK_APPROVE, Cap.ROLLBACK, Cap.REVOKE, Cap.READ}),
    Role.BRIDGE: frozenset({Cap.TASK_CREATE, Cap.READ}),
    Role.HOOK: frozenset(),
    Role.READER: frozenset({Cap.READ}),
}


def authenticate(token: str) -> Role | None:
    # Every configured token is compared so timing does not reveal which role matched.
    hits = [r for r in Role if (t := os.environ.get(f"AIRLOCK_TOKEN_{r.upper()}")) and hmac.compare_digest(t.encode(), token.encode())]
    return hits[0] if hits else None


def require(cap: Cap) -> Callable[[str | None], Awaitable[Role]]:
    """FastAPI dependency: the authenticated Role, which is also the actor id; 401 on a bad token, 403 without `cap`."""
    async def dep(authorization: Annotated[str | None, Header()] = None) -> Role:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or (role := authenticate(token)) is None:
            raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")
        if cap not in CAPS[role]:
            raise ApiError(403, "FORBIDDEN", f"role {role} lacks {cap}")
        return role
    return dep
