import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.policies import NotValidated, accept_validation_guarded, promote, revoke, rollback
from controller.states import Conflict, EvolutionRunState, ValidationDecision, WorkerRunState, new_id
from controller.states import Prefix as P

pytestmark = pytest.mark.anyio
ES = EvolutionRunState


async def txn[R](db: AsyncDatabase[Doc], fn: Callable[[AsyncClientSession], Awaitable[R]]) -> R:
    async with db.client.start_session() as s:
        return await s.with_transaction(fn)


async def project(db: AsyncDatabase[Doc]) -> str:
    p = new_id(P.PROJECT)
    await db["projects"].insert_one({"_id": p, "schema_version": 1, "active_harness_version_id": "hv_1", "harness_generation": 1, "constraint_epoch": 1,
                                     "validated_harness_ids": ["hv_1"], "event_seq": 0, "revision": 1, "created_at": datetime.now(UTC)})
    return p


async def eligible(db: AsyncDatabase[Doc], proj: str, candidate: str) -> str:
    e = new_id(P.EVOLUTION_RUN)
    await db["evolution_runs"].insert_one({"_id": e, "schema_version": 1, "project_id": proj, "status": ES.ELIGIBLE, "revision": 3, "constraint_epoch": 1,
                                           "baseline_harness_version_id": "hv_1", "candidate_harness_version_id": candidate, "created_at": datetime.now(UTC)})
    return e


async def worker(db: AsyncDatabase[Doc], proj: str) -> Doc:
    w = {"_id": new_id(P.WORKER_RUN), "schema_version": 1, "project_id": proj, "task_id": new_id(P.TASK), "delegation_id": new_id(P.DELEGATION),
         "attempt": 1, "status": WorkerRunState.RUNNING, "revision": 1, "harness_version_id": "hv_1", "constraint_epoch": 1}
    await db["worker_runs"].insert_one(w)
    return w


async def doc(db: AsyncDatabase[Doc], coll: str, id_: str) -> Doc:
    d = await db[coll].find_one({"_id": id_})
    assert d is not None
    return d


async def test_concurrent_candidates_on_one_baseline_promote_exactly_one(db: AsyncDatabase[Doc]) -> None:
    proj = await project(db)
    pinned, evos = await worker(db, proj), {await eligible(db, proj, "hv_2"): "hv_2", await eligible(db, proj, "hv_3"): "hv_3"}
    async with asyncio.TaskGroup() as tg:
        tasks = {e: tg.create_task(txn(db, lambda s, e=e: promote(s, db, e, "evaluator"))) for e in evos}
    results = {e: t.result() for e, t in tasks.items()}
    assert sorted(results.values()) == [ES.PROMOTED, ES.REJECTED]
    winner, loser = sorted(results, key=lambda e: results[e] != ES.PROMOTED)
    w, lo, p = await doc(db, "evolution_runs", winner), await doc(db, "evolution_runs", loser), await doc(db, "projects", proj)
    assert (w["status"], w["revision"], lo["status"], lo["reason"]) == (ES.PROMOTED, 4, ES.REJECTED, "stale_baseline")
    assert (p["active_harness_version_id"], p["validated_harness_ids"], p["harness_generation"], p["revision"], p["event_seq"]) == (
        evos[winner], ["hv_1", evos[winner]], 2, 2, 1)
    events = await db["events"].find({"stream_id": proj}).to_list()
    assert [(e["sequence"], e["type"], e["payload"]["harness_version_id"]) for e in events] == [(1, "harness.promoted", evos[winner])]
    assert (await doc(db, "worker_runs", pinned["_id"]))["harness_version_id"] == "hv_1"


async def test_revoke_between_eligibility_and_promotion_rejects_stale_epoch(db: AsyncDatabase[Doc]) -> None:
    proj = await project(db)
    evo = await eligible(db, proj, "hv_2")
    assert await txn(db, lambda s: revoke(s, db, proj, "operator")) == 2
    assert await txn(db, lambda s: promote(s, db, evo, "evaluator")) == ES.REJECTED
    e, p = await doc(db, "evolution_runs", evo), await doc(db, "projects", proj)
    assert (e["status"], e["reason"]) == (ES.REJECTED, "stale_epoch")
    assert (p["active_harness_version_id"], p["validated_harness_ids"], p["harness_generation"], p["constraint_epoch"]) == ("hv_1", ["hv_1"], 1, 2)


async def test_promote_refuses_a_run_that_is_not_eligible(db: AsyncDatabase[Doc]) -> None:
    proj = await project(db)
    evo = await eligible(db, proj, "hv_2")
    assert await txn(db, lambda s: promote(s, db, evo, "evaluator")) == ES.PROMOTED
    with pytest.raises(Conflict):
        await txn(db, lambda s: promote(s, db, evo, "evaluator"))
    assert (await doc(db, "projects", proj))["harness_generation"] == 2


async def test_rollback_only_targets_validated_versions_and_keeps_epoch(db: AsyncDatabase[Doc]) -> None:
    proj = await project(db)
    evo = await eligible(db, proj, "hv_2")
    await txn(db, lambda s: promote(s, db, evo, "evaluator"))
    before = await doc(db, "projects", proj)
    with pytest.raises(NotValidated) as exc:
        await txn(db, lambda s: rollback(s, db, proj, "hv_9", "operator"))
    assert isinstance(exc.value, Conflict)
    assert await doc(db, "projects", proj) == before
    await txn(db, lambda s: rollback(s, db, proj, "hv_1", "operator"))
    p = await doc(db, "projects", proj)
    assert (p["active_harness_version_id"], p["harness_generation"], p["constraint_epoch"], p["validated_harness_ids"]) == ("hv_1", 3, 1, ["hv_1", "hv_2"])
    assert [e["type"] for e in await db["events"].find({"stream_id": proj}).sort("sequence").to_list()] == ["harness.promoted", "harness.rolled_back"]


@pytest.mark.parametrize(("revoked", "expected"), [(True, ValidationDecision.INCONCLUSIVE), (False, ValidationDecision.PASS)])
async def test_revoke_makes_guarded_acceptance_inconclusive(db: AsyncDatabase[Doc], revoked: bool, expected: ValidationDecision) -> None:
    proj = await project(db)
    w = await worker(db, proj)
    if revoked:
        await txn(db, lambda s: revoke(s, db, proj, "operator"))
    assert await txn(db, lambda s: accept_validation_guarded(s, db, ValidationDecision.PASS, w)) == expected


@pytest.mark.parametrize("revoke_before_guard", [True, False])
async def test_revoke_racing_acceptance_never_accepts_under_stale_epoch(db: AsyncDatabase[Doc], revoke_before_guard: bool) -> None:
    proj, order, attempts = await project(db), list[str](), {"accept": 0, "revoke": 0}
    w, paused, revoked, retried = await worker(db, proj), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def accept(s: AsyncClientSession) -> ValidationDecision:
        attempts["accept"] += 1
        first = attempts["accept"] == 1
        run = await db["worker_runs"].find_one({"_id": w["_id"]}, session=s)
        assert run is not None
        if first and revoke_before_guard:
            paused.set()
            await revoked.wait()
        decision = await accept_validation_guarded(s, db, ValidationDecision.PASS, run)
        if first and not revoke_before_guard:
            paused.set()
            await retried.wait()
        return decision

    async def revoker(s: AsyncClientSession) -> int:
        attempts["revoke"] += 1
        if attempts["revoke"] == 2:
            retried.set()
        return await revoke(s, db, proj, "operator")

    async def run_accept() -> ValidationDecision:
        decision = await txn(db, accept)
        order.append("accept")
        return decision

    async def run_revoke() -> None:
        await paused.wait()
        await txn(db, revoker)
        order.append("revoke")
        revoked.set()

    async with asyncio.timeout(60), asyncio.TaskGroup() as tg:
        accepted, _ = tg.create_task(run_accept()), tg.create_task(run_revoke())
    assert (await doc(db, "projects", proj))["constraint_epoch"] == 2
    if revoke_before_guard:
        assert (accepted.result(), order, attempts["accept"]) == (ValidationDecision.INCONCLUSIVE, ["revoke", "accept"], 2)
    else:
        assert (accepted.result(), order) == (ValidationDecision.PASS, ["accept", "revoke"])
        assert attempts["revoke"] >= 2
