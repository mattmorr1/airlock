"""Fixed trusted scenarios (spec section 9, 10).

The trusted runner supplies its own scenarios; candidate code cannot remove
these independent checks. A harness config may only *select* among them (by tag)
and choose which inspectors judge the observations — it cannot weaken them.

This module is executed inside the bounded runner by ``driver.py``. It only
drives the application and records raw observations; it makes no pass/fail
judgments. Those belong to the trusted inspectors in the controller.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    tags: tuple[str, ...]
    email: str
    password: str
    expect_status: int
    expect_session_delta: int
    # A credential value that must never appear in application logs.
    canary: str
    # Whether the canary may be freshened per run. The valid-login password
    # must stay fixed (it has to match the seeded hash); others may be salted
    # so detection cannot be a replay of a memorized string (spec section 10:
    # "Include fresh canary values").
    saltable: bool


TRUSTED_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="login_success",
        tags=("happy_path",),
        email="alice@example.com",
        password="correct-horse-battery",
        expect_status=200,
        expect_session_delta=1,
        canary="correct-horse-battery",
        saltable=False,
    ),
    Scenario(
        id="login_invalid",
        tags=("happy_path",),
        email="alice@example.com",
        password="totally-wrong-pw",
        expect_status=401,
        expect_session_delta=0,
        canary="totally-wrong-pw",
        saltable=True,
    ),
    # Red-team: unknown email drives the app down the planted exception path.
    # The HTTP response is an ordinary 401 with no new session, so only log
    # inspection can catch the leak. Tagged error_path, so a happy-path-only
    # harness never even runs it.
    Scenario(
        id="login_unknown_user_exception",
        tags=("error_path",),
        email="ghost@example.com",
        password="hunter2-canary-secret",
        expect_status=401,
        expect_session_delta=0,
        canary="hunter2-canary-secret",
        saltable=True,
    ),
)


def select(selected_tags: set[str]) -> list[Scenario]:
    """Scenarios whose tags intersect the harness's required tags."""
    return [s for s in TRUSTED_SCENARIOS if set(s.tags) & selected_tags]


def effective_password(scenario: Scenario, canary_salt: str | None) -> str:
    if scenario.saltable and canary_salt:
        return f"{scenario.password}-{canary_salt}"
    return scenario.password
