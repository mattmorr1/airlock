"""Job worker loop — Person B's half of the async boundary (spec section 7.10).

Person A's scheduler enqueues jobs; this process claims them, dispatches to the
execution-plane handlers, and records a small result reference. Delivery is
at-least-once, so every handler is idempotent (content-addressed artifacts,
cache-keyed validations, fenced integration). Change streams are only a wake-up
hint; the durable truth is the polled `jobs` collection.
"""
from __future__ import annotations

import time

from common.config import get_settings
from common.enums import JobKind
from common.ids import new_id
from common.jobqueue import claim, complete, fail, reclaim_expired
from controller import integration, validation, workers
from evolution import evaluate, propose


def handle_launch(payload: dict) -> dict:
    """Launch one managed worker. Container mode uses the real SDK backend; dev
    mode may name a scripted fixture so the loop runs offline."""
    settings = get_settings()
    fixture = payload.get("dev_fixture")
    if settings.execution_mode == "local" and fixture:
        from controller.dev_fixtures import FIXTURES
        backend = workers.ScriptedWorker(FIXTURES[fixture])
    else:
        backend = workers.ClaudeSDKWorker()
    result = workers.launch_worker(payload, backend)
    return {"worker_run_id": result["worker_run_id"],
            "output_artifact_id": result["output_artifact_id"],
            "scope_violations": result["scope_violations"]}


def handle_validate(payload: dict) -> dict:
    res = validation.execute_validation(payload["validation_run_id"])
    return {"validation_run_id": res["validation_run_id"], "decision": res["decision"]}


def handle_integrate(payload: dict) -> dict:
    res = integration.integrate(payload["integration_run_id"])
    return {"integration_run_id": res["integration_run_id"], "status": res["status"],
            "result_artifact_id": res.get("result_artifact_id"), "decision": res.get("decision")}


def handle_evolve(payload: dict) -> dict:
    run_id = payload["evolution_run_id"]
    propose.generate(run_id)          # idempotent: skips if candidate exists
    res = evaluate.evaluate_run(run_id)
    return {"evolution_run_id": run_id, "status": res["status"]}


HANDLERS = {
    JobKind.LAUNCH.value: handle_launch,
    JobKind.VALIDATE.value: handle_validate,
    JobKind.INTEGRATE.value: handle_integrate,
    JobKind.EVOLVE.value: handle_evolve,
}


def run_once(owner: str) -> bool:
    """Claim and run at most one job. Returns True if a job was processed."""
    reclaim_expired()
    job = claim(owner)
    if job is None:
        return False
    fence = job["lease"]["fence"]
    handler = HANDLERS.get(job["kind"])
    if handler is None:
        fail(job["_id"], owner, fence, error=f"no handler for kind {job['kind']!r}")
        return True
    try:
        result = handler(job["payload"])
        complete(job["_id"], owner, fence, result_ref=result)
    except Exception as exc:
        fail(job["_id"], owner, fence, error=f"{type(exc).__name__}: {exc}")
    return True


def serve(owner: str | None = None, max_jobs: int | None = None) -> int:
    """Poll and process jobs until the queue drains (or max_jobs reached)."""
    owner = owner or new_id("jobworker")
    interval = get_settings().job_poll_interval_seconds
    processed = 0
    idle = 0
    while True:
        if run_once(owner):
            processed += 1
            idle = 0
            if max_jobs and processed >= max_jobs:
                break
        else:
            idle += 1
            if max_jobs is None and idle >= 3:
                break  # queue drained
            time.sleep(interval)
    return processed


if __name__ == "__main__":
    import sys
    n = serve(max_jobs=int(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"processed {n} jobs")
