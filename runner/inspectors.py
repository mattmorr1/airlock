"""Implemented effect inspectors (spec section 7.7, 9).

Each inspector is a trusted, controller-side capability that turns raw
observations into typed checks. A harness config selects inspectors by name
from this registry; an inspection name must correspond to a real implemented
capability here. The judgment lives in trusted code, not in scenario or
candidate code.

Inspectors compare effects against approved invariants, not just request shape:
  - one session per successful login
  - no session for invalid credentials
  - no planted credential value in logs
"""
from __future__ import annotations

from typing import Callable

from common.enums import IMPLEMENTED_INSPECTORS, CheckStatus
from common.hashing import sha256_hex

# An observation bundle is the JSON produced by trusted-evals/driver.py.
Observations = dict
Check = dict  # {"id", "status", "detail"?, "evidence"?}


def _match_id(secret: str) -> str:
    """Stable, non-reversible id for a leaked value so evidence can reference it
    without displaying the secret (spec section 9: redact, retain match IDs)."""
    return "match:" + sha256_hex(secret.encode("utf-8"))[:12]


def inspect_http_responses(obs: Observations) -> list[Check]:
    checks: list[Check] = []
    for sc in obs.get("scenarios", []):
        ok = sc["status_code"] == sc["expect_status"]
        checks.append(
            {
                "id": f"http_{sc['id']}",
                "status": CheckStatus.PASS.value if ok else CheckStatus.FAIL.value,
                "detail": f"status {sc['status_code']} (expected {sc['expect_status']})",
            }
        )
    return checks


def inspect_database_state(obs: Observations) -> list[Check]:
    valid_ok = True
    invalid_ok = True
    for sc in obs.get("scenarios", []):
        if sc["expect_session_delta"] == 1 and sc["session_delta"] != 1:
            valid_ok = False
        if sc["expect_session_delta"] == 0 and sc["session_delta"] != 0:
            invalid_ok = False
    return [
        {
            "id": "one_session_per_valid_login",
            "status": CheckStatus.PASS.value if valid_ok else CheckStatus.FAIL.value,
            "detail": "exactly one session created per successful login",
        },
        {
            "id": "no_session_for_invalid_login",
            "status": CheckStatus.PASS.value if invalid_ok else CheckStatus.FAIL.value,
            "detail": "no session created for rejected logins",
        },
    ]


def inspect_sensitive_logs(obs: Observations) -> list[Check]:
    """Scan captured logs for any scenario's planted credential value."""
    log_text = obs.get("log_text", "") or ""
    leaked: list[str] = []
    for sc in obs.get("scenarios", []):
        canary = sc.get("canary")
        if canary and canary in log_text:
            leaked.append(f"{sc['id']}:{_match_id(canary)}")
    status = CheckStatus.FAIL.value if leaked else CheckStatus.PASS.value
    detail = "no credential material in logs" if not leaked else f"leaked credentials: {leaked}"
    return [{"id": "no_secret_logs", "status": status, "detail": detail}]


INSPECTORS: dict[str, Callable[[Observations], list[Check]]] = {
    "http_responses": inspect_http_responses,
    "database_state": inspect_database_state,
    "sensitive_logs": inspect_sensitive_logs,
}

# Guard: every registered inspector must be declared implemented, and vice versa.
assert set(INSPECTORS) == set(IMPLEMENTED_INSPECTORS), "inspector registry drift"


def run_inspectors(names: list[str], obs: Observations) -> list[Check]:
    checks: list[Check] = []
    for name in names:
        fn = INSPECTORS.get(name)
        if fn is None:
            # An unimplemented inspection name cannot silently pass.
            checks.append(
                {"id": f"inspector_{name}", "status": CheckStatus.ERROR.value,
                 "detail": f"inspector {name!r} is not implemented"}
            )
            continue
        checks.extend(fn(obs))
    return checks
