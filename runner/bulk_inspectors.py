"""Trusted inspectors for the bulk-update demo (spec section 9).

The judges. A harness config selects inspectors by name from this registry; an
unimplemented name errors rather than silently passing. The star is
`rows_unchanged_on_rejection`: it looks at the actual database before and after
a rejected batch, catching a partial write that a status-only check cannot.
"""
from __future__ import annotations

from typing import Callable

Observations = dict
Check = dict

IMPLEMENTED = ("http_responses", "rows_unchanged_on_rejection", "idempotency_semantics")


def _pass(cid, detail=""):
    return {"id": cid, "status": "pass", "detail": detail}


def _fail(cid, detail="", evidence=None):
    c = {"id": cid, "status": "fail", "detail": detail}
    if evidence is not None:
        c["evidence"] = evidence
    return c


def inspect_http_responses(obs: Observations) -> list[Check]:
    checks = []
    for sc in obs.get("scenarios", []):
        if "expect_status" not in sc:
            continue
        ok = sc["status_code"] == sc["expect_status"]
        checks.append((_pass if ok else _fail)(
            f"http_{sc['id']}", f"status {sc['status_code']} (expected {sc['expect_status']})"))
    return checks


def inspect_rows_unchanged_on_rejection(obs: Observations) -> list[Check]:
    """For every batch that must be rejected, every affected row must be byte-for-byte
    unchanged. This is the invariant that exposes the partial write."""
    checks = []
    for sc in obs.get("scenarios", []):
        if not sc.get("reject_unchanged"):
            continue
        changed = []
        for item_id, ba in sc.get("affected", {}).items():
            if ba.get("before") != ba.get("after"):
                changed.append({"id": item_id, "before": ba.get("before"), "after": ba.get("after")})
        if changed:
            checks.append(_fail("rows_unchanged_on_rejection",
                                f"rejected batch '{sc['id']}' left {len(changed)} row(s) changed",
                                evidence=changed))
        else:
            checks.append(_pass("rows_unchanged_on_rejection",
                                f"rejected batch '{sc['id']}' left all affected rows unchanged"))
    return checks


def inspect_idempotency_semantics(obs: Observations) -> list[Check]:
    checks = []
    for sc in obs.get("scenarios", []):
        if sc.get("idempotent"):
            ok = sc.get("first_result") == sc.get("retry_result") and sc["status_code"] == 200
            checks.append((_pass if ok else _fail)(
                "idempotency_retry", "identical retry returns the original result"))
        if sc.get("id") == "reuse_diff_payload":
            ok = sc["status_code"] == sc.get("expect_status", 409)
            checks.append((_pass if ok else _fail)(
                "idempotency_conflict", f"reused key with different payload -> {sc['status_code']} (want 409)"))
    return checks


INSPECTORS: dict[str, Callable[[Observations], list[Check]]] = {
    "http_responses": inspect_http_responses,
    "rows_unchanged_on_rejection": inspect_rows_unchanged_on_rejection,
    "idempotency_semantics": inspect_idempotency_semantics,
}
assert set(INSPECTORS) == set(IMPLEMENTED), "inspector registry drift"


def run_inspectors(names: list[str], obs: Observations) -> list[Check]:
    checks: list[Check] = []
    for name in names:
        fn = INSPECTORS.get(name)
        if fn is None:
            checks.append({"id": f"inspector_{name}", "status": "error",
                           "detail": f"inspector {name!r} is not implemented"})
            continue
        checks.extend(fn(obs))
    return checks
