from datetime import UTC, datetime
from pathlib import Path

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.planner import Plan, Propose, digest, planner_options, scope_matches, scope_ok, validate
from controller.planner import run_plan_job as run
from controller.states import JobKind, JobStatus, TaskState, new_id
from controller.states import Prefix as P


def asg(key: str, scope: list[str], deps: list[str] | None = None) -> Doc:
    return {"key": key, "objective": f"do {key}", "depends_on": deps or [], "file_scope": scope, "profile_key": "impl"}


def plan(*assignments: Doc) -> Doc:
    return {"constraints": ["no secrets in logs"], "acceptance": ["login works"], "interfaces": {"login": "login(email, password) -> token"},
            "assignments": list(assignments)}


SPEC = plan(asg("schema", ["db/migrations/**"]), asg("service", ["src/auth/service.py"], ["schema"]), asg("routes", ["src/auth/routes.py"], ["schema"]))


@pytest.mark.parametrize("entry", ["src/auth/service.py", "db/migrations/**", "README.md", ".github/workflows/ci.yml"])
def test_grammar_accepts(entry: str) -> None:
    assert scope_ok(entry)


@pytest.mark.parametrize("entry", ["src/*.py", "src/**/x.py", "**", "src/**/", "/etc/passwd", "../x", "a/../b", "./a", "a//b", "a/", "", "a/?.py",
                                   "a/[ab].py", "a/{x,y}.py", "a\\b"])
def test_grammar_rejects(entry: str) -> None:
    assert not scope_ok(entry)
    assert isinstance(validate("r", plan(asg("a", [entry]))), list)


def test_scope_matches() -> None:
    scope = ["src/auth/**", "README.md"]
    assert scope_matches(scope, "src/auth/a/b.py") and scope_matches(scope, "README.md")
    assert not any(scope_matches(scope, p) for p in ["src/auth", "src/authx/a.py", "src/a.py", "README.md.bak", "docs/README.md"])


@pytest.mark.parametrize(("raw", "needle"), [
    (plan(asg("a", ["x"], ["b"]), asg("b", ["y"], ["a"])), "cycle"),
    (plan(asg("a", ["x"], ["a"])), "cycle"),
    (plan(asg("a", ["x"], ["c"]), asg("b", ["y"], ["a"]), asg("c", ["z"], ["b"])), "cycle"),
    (plan(asg("a", ["x"], ["nope"])), "unknown depends_on"),
    (plan(asg("a", ["x"]), asg("a", ["y"])), "duplicate"),
    (plan(), "no assignments"),
    ({**plan(asg("a", ["x"])), "admin": True}, "admin"),
    (plan({**asg("a", ["x"]), "privileged": True}), "privileged"),
])
def test_rejects(raw: Doc, needle: str) -> None:
    errors = validate("r", raw)
    assert isinstance(errors, list) and any(needle in e for e in errors), errors


def test_valid_plan_has_no_inferred_edges() -> None:
    contract = validate("r", SPEC)
    assert isinstance(contract, dict) and contract["inferred_edges"] == [] and contract["request"] == "r"
    assert [a["depends_on"] for a in contract["assignments"]] == [[], ["schema"], ["schema"]]


def test_overlap_infers_edge_in_list_order_and_changes_digest() -> None:
    contract = validate("r", plan(asg("svc", ["src/auth/service.py"]), asg("pkg", ["src/**"]), asg("docs", ["docs/**"])))
    assert isinstance(contract, dict) and contract["inferred_edges"] == [["svc", "pkg"]]
    assert contract["assignments"][1]["depends_on"] == ["svc"] and contract["assignments"][2]["depends_on"] == []
    assert digest(contract) != digest({**contract, "inferred_edges": []})


@pytest.mark.parametrize("raw", [
    plan(asg("a", ["src/**"]), asg("b", ["src/x.py"], ["a"])),
    plan(asg("a", ["src/**"], ["b"]), asg("b", ["src/x.py"])),
    plan(asg("a", ["src/**"]), asg("m", ["m.py"], ["a"]), asg("b", ["src/x.py"], ["m"])),
    plan(asg("a", ["src/auth/**"]), asg("b", ["src/authx/**"]), asg("c", ["src/auth"])),
])
def test_no_edge_when_ordered_or_disjoint(raw: Doc) -> None:
    contract = validate("r", raw)
    assert isinstance(contract, dict) and contract["inferred_edges"] == []


def test_digest_is_stable() -> None:
    contract = validate("Implement login and sessions", SPEC)
    assert isinstance(contract, dict)
    shuffled = {k: contract[k] for k in reversed(contract)} | {"assignments": [dict(reversed(a.items())) for a in contract["assignments"]]}
    assert digest(contract) == digest(shuffled) == "sha256:666f6943d018a1af9440a52b3a75ff74b94bf935352e5b45934a0e994f294485"


def test_sdk_options_are_read_only() -> None:
    opts = planner_options(Path("/checkout"))
    assert opts.tools == opts.allowed_tools == ["Read", "Glob", "Grep"] and "Bash" not in opts.allowed_tools
    assert opts.setting_sources == [] and opts.max_turns == 15 and opts.cwd == Path("/checkout")
    assert opts.output_format == {"type": "json_schema", "schema": Plan.model_json_schema()}


async def seed(db: AsyncDatabase[Doc]) -> Doc:
    now, proj, task = datetime.now(UTC), new_id(P.PROJECT), new_id(P.TASK)
    await db["tasks"].insert_one({"_id": task, "schema_version": 1, "project_id": proj, "request": "Implement login", "status": TaskState.DRAFT,
                                  "revision": 1, "event_seq": 0, "base_artifact_id": "art_base", "created_at": now})
    job: Doc = {"_id": new_id(P.JOB), "schema_version": 1, "project_id": proj, "task_id": task, "kind": JobKind.PLAN, "payload": {},
                "status": JobStatus.RUNNING, "dedupe_key": f"{task}:plan", "available_at": now, "attempts": 1,
                "lease": {"owner": "w1", "expires_at": now, "fence": 1}, "created_at": now}
    await db["jobs"].insert_one(job)
    return job


def stub(*raws: Doc) -> tuple[list[list[str] | None], Propose]:
    seen: list[list[str] | None] = []

    async def propose(request: str, checkout: Path, errors: list[str] | None) -> object:
        seen.append(errors)
        return raws[len(seen) - 1]
    return seen, propose


@pytest.mark.anyio
async def test_replan_after_invalid_plan_ends_planned(db: AsyncDatabase[Doc]) -> None:
    job = await seed(db)
    seen, propose = stub(plan(asg("a", ["src/*.py"])), SPEC)
    assert await run(db, job, "w1", 1, Path("/checkout"), propose) == TaskState.PLANNED
    assert seen[0] is None and seen[1] is not None and "src/*.py" in seen[1][0]
    task = await db["tasks"].find_one({"_id": job["task_id"]})
    assert task is not None and task["status"] == TaskState.PLANNED and task["revision"] == 2 and task["contract_version"] == 1
    assert task["contract_digest"] == digest(task["contract"]) and task["contract"] == validate("Implement login", SPEC)
    assert [e["type"] async for e in db["events"].find({"stream_id": job["task_id"]})] == ["task.planned"]
    assert (await db["jobs"].find_one({"_id": job["_id"]}) or {})["status"] == JobStatus.COMPLETED


@pytest.mark.anyio
async def test_two_invalid_plans_end_failed(db: AsyncDatabase[Doc]) -> None:
    job = await seed(db)
    seen, propose = stub(plan(asg("a", ["x"], ["b"]), asg("b", ["y"], ["a"])), plan(asg("a", ["x"], ["ghost"])))
    assert await run(db, job, "w1", 1, Path("/checkout"), propose) == TaskState.FAILED
    assert len(seen) == 2
    task = await db["tasks"].find_one({"_id": job["task_id"]})
    assert task is not None and task["status"] == TaskState.FAILED and "contract" not in task and "ghost" in task["plan_errors"][0]
    assert [e["type"] async for e in db["events"].find({"stream_id": job["task_id"]})] == ["task.failed"]
    assert (await db["jobs"].find_one({"_id": job["_id"]}) or {})["status"] == JobStatus.COMPLETED
