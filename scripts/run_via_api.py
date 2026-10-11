"""Run the whole flow through A's REAL HTTP API — the exact calls the plugin makes.

Bootstraps a project (operator seed), then drives the parent-session flow over
HTTP just like `plugin/skills/run/SKILL.md`: airlock_plan (POST /tasks) ->
poll airlock_status (GET /tasks) until planned -> operator approve ->
airlock_delegate (POST /delegations) loop until the task completes. The B
worker daemon (A's run_worker + B's handlers) does plan/launch/validate/
integrate. Active harness is v2 so the DB invariant is enforced; the first
attempt is faulty (caught) and the repair attempt is fixed (passes).

Env: AIRLOCK_URL, AIRLOCK_TOKEN_BRIDGE, AIRLOCK_TOKEN_OPERATOR, AIRLOCK_MONGO_URI,
     AIRLOCK_DB, TARGET_MONGO_URI.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.db import col, get_client
from controller import artifacts, harness, snapshots
from evolution.propose import propose_config

API = os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000")
BRIDGE = {"Authorization": f"Bearer {os.environ.get('AIRLOCK_TOKEN_BRIDGE', '')}"}
OP = {"Authorization": f"Bearer {os.environ.get('AIRLOCK_TOKEN_OPERATOR', '')}"}


def bootstrap() -> tuple[str, str]:
    """Operator seed: a project with harness v1 + v2 (active v2) and a baseline."""
    from controller.states import Prefix, new_id
    pid = new_id(Prefix.PROJECT)
    base = artifacts.put_tree(snapshots.read_dir_tree(Path("target-app")), kind="baseline",
                              project_id=pid, created_by="operator_01")
    v1 = harness.bootstrap_bulk_v1(pid)
    v2cfg, _, _ = propose_config(harness.BULK_V1_CONFIG, {})
    v2 = harness.create_harness_version(project_id=pid, config=v2cfg, parent_id=v1["_id"])
    col("projects").insert_one({
        "_id": pid, "schema_version": 1, "name": "plugin-demo", "created_at": datetime.now(UTC),
        "repository": {"registered_root_id": f"repo_{pid[-6:]}", "default_branch": "main"},
        "baseline_artifact_id": base["_id"], "active_harness_version_id": v2["_id"],
        "harness_generation": 1, "constraint_epoch": 1, "validated_harness_ids": [v1["_id"], v2["_id"]],
        "event_seq": 0, "revision": 1})
    return pid, base["_id"]


def main() -> None:
    pid, base = bootstrap()
    ui = f"http://127.0.0.1:8099/?project={pid}"
    print(f"bootstrapped project {pid} (active harness v2). watch: {ui}")
    subprocess.run(["open", ui], check=False)

    with httpx.Client(base_url=API, timeout=70) as hc:
        # 1. airlock_plan -> POST /v1/tasks
        r = hc.post("/v1/tasks", headers={**BRIDGE, "Idempotency-Key": f"plan-{pid}"},
                    json={"project_id": pid, "parent_session_id": "demo_session",
                          "request": "Implement atomic bulk item updates with idempotency", "base_artifact_id": base})
        r.raise_for_status()
        tid = r.json()["task_id"]
        print(f"[plan] task {tid} created; planning...")

        # 2. poll until planned
        for _ in range(60):
            t = hc.get(f"/v1/tasks/{tid}", headers={**BRIDGE}, params={"wait_seconds": 50}).json()["task"]
            if t["status"] in ("planned", "failed"):
                break
        assert t["status"] == "planned", f"planning ended {t['status']}: {t.get('plan_errors')}"
        asn = [a["key"] for a in t["contract"]["assignments"]]
        print(f"[plan] planned; assignments={asn}; digest={t['contract_digest'][:20]}")

        # 3. operator approve
        ap = hc.post(f"/v1/tasks/{tid}/approve", headers={**OP, "Idempotency-Key": f"approve-{tid}"},
                     json={"contract_digest": t["contract_digest"]})
        ap.raise_for_status()
        print(f"[approve] {ap.json()['status']} at epoch {ap.json()['constraint_epoch']}")

        # 4. delegate loop until the task completes
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            body = hc.get(f"/v1/tasks/{tid}", headers={**BRIDGE}, params={"wait_seconds": 30}).json()
            t, dels = body["task"], {d["assignment_key"]: d for d in body["delegations"]}
            if t["status"] in ("completed", "failed", "cancelled"):
                break
            for a in t["contract"]["assignments"]:
                d = dels.get(a["key"])
                ready = all(dep in {k for k, dd in dels.items() if dd["status"] == "accepted"} for dep in a["depends_on"])
                if (d is None and ready) or (d and d["status"] == "repair_required"):
                    rr = hc.post(f"/v1/tasks/{tid}/delegations", headers={**BRIDGE, "Idempotency-Key": f"del-{a['key']}-{int(time.monotonic())}"},
                                 json={"assignment_key": a["key"]})
                    if rr.status_code < 300:
                        print(f"[delegate] {a['key']} -> attempt {rr.json().get('attempt')}")

        # 5. report
        body = hc.get(f"/v1/tasks/{tid}", headers={**BRIDGE}).json()
        t = body["task"]
        print(f"\n[result] task {t['status']} final_artifact={t.get('final_artifact_id')}")
        for d in body["delegations"]:
            print(f"  delegation {d['assignment_key']}: {d['status']} (attempt {d['attempt']})")
        print("\n  validation verdicts:")
        for v in col("validation_runs").find({"task_id": tid}).sort("created_at", 1):
            rows = next((c for c in v.get("checks", []) if c["id"] == "rows_unchanged_on_rejection"), {})
            print(f"    {v.get('kind')}: decision={v.get('decision')}  rows_unchanged={rows.get('status','-')}")
        print(f"\n  explore: {ui}")


if __name__ == "__main__":
    main()
