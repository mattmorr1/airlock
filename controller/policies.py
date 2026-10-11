from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.states import Conflict, Entity, EvolutionRunState, ValidationDecision, append_event, transition


class NotValidated(Conflict):
    def __init__(self, project_id: str, harness_version_id: str) -> None:
        Exception.__init__(self, f"projects {project_id}: missing, or {harness_version_id} is not in validated_harness_ids")
        self.project_id, self.harness_version_id = project_id, harness_version_id


async def promote(session: AsyncClientSession, db: AsyncDatabase[Doc], evo_id: str, actor_id: str) -> EvolutionRunState:
    """Flip the active pointer to an eligible candidate inside the caller's transaction; a moved baseline or epoch rejects it instead."""
    evo = await db["evolution_runs"].find_one({"_id": evo_id}, session=session)
    if evo is None:
        raise LookupError(f"evolution_runs {evo_id} not found")
    if evo["status"] != EvolutionRunState.ELIGIBLE:
        raise Conflict(Entity.EVOLUTION_RUN, evo_id, evo["revision"], EvolutionRunState.PROMOTED)
    proj, base, cand = evo["project_id"], evo["baseline_harness_version_id"], evo["candidate_harness_version_id"]
    res = await db["projects"].update_one({"_id": proj, "active_harness_version_id": base, "constraint_epoch": evo["constraint_epoch"]},
                                          {"$set": {"active_harness_version_id": cand}, "$addToSet": {"validated_harness_ids": cand},
                                           "$inc": {"harness_generation": 1, "revision": 1}}, session=session)
    if res.matched_count:
        await transition(session, db["evolution_runs"], evo_id, evo["revision"], EvolutionRunState.PROMOTED)
        await append_event(session, proj, db["projects"], "harness.promoted", actor_id,
                           {"evolution_run_id": evo_id, "harness_version_id": cand, "previous_harness_version_id": base})
        return EvolutionRunState.PROMOTED
    if (project := await db["projects"].find_one({"_id": proj}, {"active_harness_version_id": 1}, session=session)) is None:
        raise LookupError(f"projects {proj} not found")
    reason = "stale_baseline" if project["active_harness_version_id"] != base else "stale_epoch"
    await transition(session, db["evolution_runs"], evo_id, evo["revision"], EvolutionRunState.REJECTED, {"reason": reason})
    return EvolutionRunState.REJECTED


async def rollback(session: AsyncClientSession, db: AsyncDatabase[Doc], project_id: str, harness_version_id: str, actor_id: str) -> None:
    res = await db["projects"].update_one({"_id": project_id, "validated_harness_ids": harness_version_id},
                                          {"$set": {"active_harness_version_id": harness_version_id}, "$inc": {"harness_generation": 1, "revision": 1}},
                                          session=session)
    if res.matched_count == 0:
        raise NotValidated(project_id, harness_version_id)
    await append_event(session, project_id, db["projects"], "harness.rolled_back", actor_id, {"harness_version_id": harness_version_id})


async def revoke(session: AsyncClientSession, db: AsyncDatabase[Doc], project_id: str, actor_id: str) -> int:
    project = await db["projects"].find_one_and_update({"_id": project_id}, {"$inc": {"constraint_epoch": 1, "revision": 1}}, {"constraint_epoch": 1},
                                                       return_document=ReturnDocument.AFTER, session=session)
    if project is None:
        raise LookupError(f"projects {project_id} not found")
    await append_event(session, project_id, db["projects"], "project.revoked", actor_id, {"constraint_epoch": project["constraint_epoch"]})
    return project["constraint_epoch"]


async def guard_epoch(session: AsyncClientSession, db: AsyncDatabase[Doc], project_id: str, epoch: int) -> bool:
    """True iff `epoch` is current. A write, not a read, so a concurrent revoke write-conflicts the transaction and forces a retry.
    $inc rather than $set of a timestamp: a no-op update writes nothing and so would not conflict."""
    res = await db["projects"].update_one({"_id": project_id, "constraint_epoch": epoch}, {"$inc": {"epoch_guard": 1}}, session=session)
    if res.matched_count == 0 and await db["projects"].count_documents({"_id": project_id}, session=session) == 0:
        raise LookupError(f"projects {project_id} not found")
    return res.matched_count == 1


async def accept_validation_guarded(session: AsyncClientSession, db: AsyncDatabase[Doc], decision: ValidationDecision, worker_run: Doc) -> ValidationDecision:
    ok = await guard_epoch(session, db, worker_run["project_id"], worker_run["constraint_epoch"])
    return decision if ok else ValidationDecision.INCONCLUSIVE
