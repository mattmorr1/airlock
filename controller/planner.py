import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.scheduler import complete
from controller.states import TaskState, append_event, transition

ACTOR = "planner"
TOOLS = ["Read", "Glob", "Grep"]
PROMPT = """Plan the coding request below for the repository in the current directory. Read the code as needed, then return the plan.
Split the work into assignments that one worker each can complete. Each has a unique key, an objective, depends_on (keys that must be integrated \
first), file_scope (the only files it may change), and profile_key.
file_scope entries are exact relative file paths or directory prefixes written dir/**; no other wildcards.
Assignments with overlapping file scopes must be ordered through depends_on; otherwise the controller orders them by list position.
Also list constraints every worker must respect, acceptance criteria for the finished task, and interfaces shared between assignments.
Turn each testable acceptance criterion into executable checks the trusted runner will run against the finished app. Prefer `checks`: \
declarative HTTP steps (`as` a seeded user such as alice or bob, method, path, json, expect_status, or same_response_as an earlier step id), \
with `bind` naming documents to template into `{name}` and `unchanged` listing binds that must be identical afterwards. Use `tests` (pytest \
modules reading AIRLOCK_BASE_URL, AIRLOCK_TOKENS, TARGET_MONGO_URI, TARGET_DB) only for what steps cannot express.

Request:
{request}"""


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    objective: str
    depends_on: list[str]
    file_scope: list[str]
    profile_key: str


class Bind(BaseModel):
    """`{name}` in a step's path or json becomes the _id of the index-th document (by _id) matching filter."""
    model_config = ConfigDict(extra="forbid")
    collection: str
    filter: dict[str, Any]
    index: int = 0


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    as_: str | None = Field(None, alias="as")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    headers: dict[str, str] = {}
    json_: Any = Field(None, alias="json")
    expect_status: int | None = None
    same_response_as: str | None = None


class HttpCheck(BaseModel):
    """Declarative acceptance check run by the trusted contract driver; `unchanged` binds must be identical after the last step."""
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    bind: dict[str, Bind] = {}
    steps: list[Step]
    unchanged: list[str] = []


class TestModule(BaseModel):
    """A pytest module run against the live app: env AIRLOCK_BASE_URL, AIRLOCK_TOKENS (json name->bearer), TARGET_MONGO_URI, TARGET_DB."""
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    code: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    constraints: list[str]
    acceptance: list[str]
    interfaces: dict[str, str]
    assignments: list[Assignment]
    checks: list[HttpCheck] = []
    tests: list[TestModule] = []


class PlannerError(Exception):
    pass


type Propose = Callable[[str, Path, list[str] | None], Awaitable[object]]


def scope_ok(entry: str) -> bool:
    body = entry.removesuffix("/**")
    return not entry.startswith("/") and all(s not in ("", ".", "..") for s in body.split("/")) and not any(c in body for c in "*?[]{}\\")


def scope_matches(scope: list[str], path: str) -> bool:
    return any(path == p or (p.endswith("/**") and path.startswith(p[:-2])) for p in scope)


def _overlap(a: str, b: str) -> bool:
    a, b = a.removesuffix("**"), b.removesuffix("**")
    return a == b or (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def _depends(deps: dict[str, list[str]], key: str, on: str) -> bool:
    seen: set[str] = set()
    stack = list(deps[key])
    while stack:
        if (k := stack.pop()) == on:
            return True
        if k not in seen:
            seen.add(k)
            stack.extend(deps[k])
    return False


def validate(request: str, raw: object) -> Doc | list[str]:
    """Parse and check a proposed plan; returns the Contract, or the errors to feed back to the planner."""
    try:
        plan = Plan.model_validate(raw)
    except ValidationError as e:
        return [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
    keys, check_ids = [a.key for a in plan.assignments], [c.id for c in [*plan.checks, *plan.tests]]
    errors = ["plan has no assignments"] if not keys else []
    errors += [f"duplicate check id {i!r}" for i in sorted({i for i in check_ids if check_ids.count(i) > 1})]
    errors += [f"check {c.id}: unchanged {u!r} is not a bind" for c in plan.checks for u in c.unchanged if u not in c.bind]
    errors += [f"check {c.id}: step {st.id} same_response_as unknown step {st.same_response_as!r}" for c in plan.checks for i, st in enumerate(c.steps)
               if st.same_response_as and st.same_response_as not in [x.id for x in c.steps[:i]]]
    errors += [f"duplicate assignment key {k!r}" for k in sorted({k for k in keys if keys.count(k) > 1})]
    errors += [f"{a.key}: unknown depends_on key {d!r}" for a in plan.assignments for d in a.depends_on if d not in keys]
    errors += [f"{a.key}: file_scope entry {e!r} is not an exact path or dir/**" for a in plan.assignments for e in a.file_scope if not scope_ok(e)]
    if errors:
        return errors
    deps = {a.key: list(a.depends_on) for a in plan.assignments}
    try:
        TopologicalSorter(deps).prepare()
    except CycleError as e:
        return [f"dependency cycle: {' -> '.join(e.args[1])}"]
    # An edge is added only between unordered pairs, so it can never close a cycle.
    inferred: list[list[str]] = []
    for i, a in enumerate(plan.assignments):
        for b in plan.assignments[i + 1:]:
            if any(_overlap(x, y) for x in a.file_scope for y in b.file_scope) and not _depends(deps, b.key, a.key) and not _depends(deps, a.key, b.key):
                deps[b.key].append(a.key)
                inferred.append([a.key, b.key])
    return {"request": request, "constraints": plan.constraints, "acceptance": plan.acceptance, "interfaces": plan.interfaces,
            "assignments": [{**a.model_dump(), "depends_on": deps[a.key]} for a in plan.assignments], "inferred_edges": inferred,
            # Only when present, so contracts without generated checks keep their pre-existing digests.
            **({"checks": [c.model_dump(by_alias=True) for c in plan.checks]} if plan.checks else {}),
            **({"tests": [t.model_dump() for t in plan.tests]} if plan.tests else {})}


def digest(contract: Doc) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def planner_options(checkout: Path) -> ClaudeAgentOptions:
    # tools bounds what exists, allowed_tools pre-approves it; setting_sources=[] keeps repo .claude/ hooks and permissions out.
    return ClaudeAgentOptions(cwd=checkout, tools=TOOLS, allowed_tools=TOOLS, setting_sources=[], max_turns=15,
                              output_format={"type": "json_schema", "schema": Plan.model_json_schema()})


async def sdk_propose(request: str, checkout: Path, errors: list[str] | None = None) -> object:
    prompt = PROMPT.format(request=request) + ("\n\nYour previous plan was rejected:\n" + "\n".join(f"- {e}" for e in errors) if errors else "")
    result: ResultMessage | None = None
    async with asyncio.timeout(180):
        async for msg in query(prompt=prompt, options=planner_options(checkout)):
            if isinstance(msg, ResultMessage):
                result = msg
    if result is None or result.is_error or result.structured_output is None:
        raise PlannerError(f"planner session returned no plan: {result and (result.subtype, result.errors)}")
    return result.structured_output


async def run_plan_job(db: AsyncDatabase[Doc], job: Doc, owner: str, fence: int, checkout: Path, propose: Propose = sdk_propose) -> TaskState:
    """`plan` job handler: propose, validate, re-plan once with the errors, then move the task to planned or failed."""
    task = await db["tasks"].find_one({"_id": job["task_id"]})
    if task is None:
        raise LookupError(f"task {job['task_id']} not found")
    result = validate(task["request"], await propose(task["request"], checkout, None))
    if isinstance(result, list):
        result = validate(task["request"], await propose(task["request"], checkout, result))

    async def apply(s: AsyncClientSession) -> TaskState:
        if isinstance(result, list):
            await transition(s, db["tasks"], task["_id"], task["revision"], TaskState.FAILED, {"plan_errors": result})
            await append_event(s, task["_id"], db["tasks"], "task.failed", ACTOR, {"reason": "plan_invalid", "errors": result})
            return TaskState.FAILED
        d = digest(result)
        await transition(s, db["tasks"], task["_id"], task["revision"], TaskState.PLANNED, {"contract": result, "contract_digest": d, "contract_version": 1})
        await append_event(s, task["_id"], db["tasks"], "task.planned", ACTOR, {"contract_digest": d, "inferred_edges": result["inferred_edges"]})
        return TaskState.PLANNED

    return await complete(db, job["_id"], owner, fence, apply)
