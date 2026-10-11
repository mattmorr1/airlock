from datetime import UTC, datetime

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from ui.server import render


@pytest.mark.anyio
async def test_dashboard_tells_the_caught_then_repaired_story(db: AsyncDatabase[Doc]) -> None:
    now, pid, tid = datetime.now(UTC), "proj_uitest", "task_uitest"

    async def ins(coll: str, *docs: Doc) -> None:  # partial fixture docs, so skip the collection validators
        await db[coll].insert_many([{"project_id": pid, "created_at": now, **d} for d in docs], bypass_document_validation=True)

    await ins("projects", {"_id": pid, "name": "ui-test", "active_harness_version_id": "hv_ui2", "constraint_epoch": 1})
    await ins("harness_versions", *({"_id": f"hv_ui{v}", "version": v, "config": {}} for v in (1, 2)))
    await ins("tasks", {"_id": tid, "status": "running", "request": "bulk updates",
                        "contract": {"assignments": [{"key": "bulk", "depends_on": [], "file_scope": ["app/**"]}]}})
    await ins("delegations", {"_id": "del_ui", "task_id": tid, "assignment_key": "bulk", "status": "running", "attempt": 2})
    await ins("worker_runs", *({"_id": f"worker_ui{n}", "task_id": tid, "delegation_id": "del_ui", "attempt": n, "status": "finished",
                                "output_artifact_id": f"art_ui{n}", "metrics": m} for n, m in ((1, {"mode": "scripted", "fixture": "bulk_faulty"}), (2, {}))))
    await ins("validation_runs", {"_id": "val_ui", "decision": "repair_required", "inputs": {"candidate_artifact_id": "art_ui1"},
                                  "checks": [{"id": "rows_unchanged_on_rejection", "status": "fail",
                                              "evidence": [{"id": "item_alice", "before": "valid", "after": "PWNED"}]}]})
    page = await render(db, pid)
    assert "active harness</div><div class=\"val\">v2" in page
    assert "scripted stand-in · bulk_faulty" in page and "mode unrecorded" in page
    assert "repair_required" in page and "PWNED" in page and "validation pending" in page
    assert "no project proj_missing" in await render(db, "proj_missing")
