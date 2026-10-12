import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pymongo.asynchronous.database import AsyncDatabase
from test_api import TOKENS, auth, project
from test_scheduler import CONTRACT, approved_task

from api.main import app
from bridge import server
from controller.db import Doc
from controller.events import Doorbell
from controller.states import DelegationState, Prefix, TaskState, new_id

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(db: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"AIRLOCK_TOKEN_{role.upper()}", token)
    monkeypatch.setenv("AIRLOCK_ROOTS", f'{{"repo_hooks": "{tmp_path}"}}')
    app.state.db = db
    async with Doorbell(db) as app.state.doorbell, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
        yield c


async def session_task(client: httpx.AsyncClient, db: AsyncDatabase[Doc], status: TaskState, root: str = "repo_hooks") -> tuple[str, str]:
    """A task in `status` owned by a fresh parent session id, in a project registered at `root`."""
    proj = await project(client, root.removeprefix("repo_"))  # same key and body replay to the same project
    sid, tid = f"sess_{uuid.uuid4().hex}", new_id(Prefix.TASK)
    await db["tasks"].insert_one({"_id": tid, "schema_version": 1, "project_id": proj, "parent_session_id": sid, "status": status, "revision": 1,
                                  "contract": CONTRACT, "approval": None, "event_seq": 0, "created_at": datetime.now(UTC)})
    return sid, tid


async def pre(client: httpx.AsyncClient, sid: str, tool: str, cwd: str = "/", **tool_input: object) -> Doc:
    r = await client.post("/v1/hooks/pre-tool-use", headers=auth("hook"), json={
        "session_id": sid, "hook_event_name": "PreToolUse", "cwd": cwd, "tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_1"})
    assert r.status_code == 200, r.text
    return r.json()


async def stop(client: httpx.AsyncClient, sid: str) -> Doc:
    r = await client.post("/v1/hooks/lifecycle", headers=auth("hook"), json={"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": True})
    assert r.status_code == 200, r.text
    return r.json()


def denied(out: Doc) -> str:
    assert out["active"] is True and out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    return out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_unmanaged_session_gets_no_decision(client: httpx.AsyncClient) -> None:
    assert await pre(client, "sess_unknown", "Agent", subagent_type="general-purpose") == {"active": False}
    assert await stop(client, "sess_unknown") == {"active": False}


async def test_hook_routes_need_hook_token(client: httpx.AsyncClient) -> None:
    rs = [await client.post(p, headers=auth("reader"), json={"session_id": "s"}) for p in ("/v1/hooks/pre-tool-use", "/v1/hooks/lifecycle")]
    assert [r.status_code for r in rs] == [403, 403]


async def test_active_task_denies_coding_agents_and_allows_read_only(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, tid = await session_task(client, db, TaskState.RUNNING)
    reason = denied(await pre(client, sid, "Agent", subagent_type="general-purpose", prompt="implement login"))
    assert f"airlock_delegate('{tid}'" in reason and "general-purpose" in reason
    assert "airlock_delegate" in denied(await pre(client, sid, "Agent", prompt="no type given"))
    assert [await pre(client, sid, "Agent", subagent_type=t) for t in ("Explore", "Plan")] == [{"active": True}] * 2
    assert await pre(client, sid, "Bash", command="rm -rf src") == {"active": True}


async def test_active_task_denies_writes_inside_registered_root_only(client: httpx.AsyncClient, db: AsyncDatabase[Doc], tmp_path: Path) -> None:
    sid, _ = await session_task(client, db, TaskState.APPROVED)
    assert "inside the registered repository" in denied(await pre(client, sid, "Edit", file_path=str(tmp_path / "src" / "a.py")))
    assert "inside the registered repository" in denied(await pre(client, sid, "Write", cwd=str(tmp_path), file_path="new.py"))
    assert "inside the registered repository" in denied(await pre(client, sid, "NotebookEdit", notebook_path=str(tmp_path / "n.ipynb")))
    assert await pre(client, sid, "Edit", file_path=str(tmp_path) + "-sibling/a.py") == {"active": True}
    assert await pre(client, sid, "Write", cwd=str(tmp_path), file_path="../outside.py") == {"active": True}


async def test_unconfigured_root_denies_writes(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, _ = await session_task(client, db, TaskState.RUNNING, root="repo_hooks_unmapped")
    assert "AIRLOCK_ROOTS" in denied(await pre(client, sid, "Edit", file_path="/tmp/x.py"))


async def test_planned_task_is_not_gated_and_stop_waits_for_operator(client: httpx.AsyncClient, db: AsyncDatabase[Doc], tmp_path: Path) -> None:
    sid, tid = await session_task(client, db, TaskState.PLANNED)
    assert await pre(client, sid, "Edit", file_path=str(tmp_path / "a.py")) == {"active": False}
    assert await stop(client, sid) == {"active": False, "systemMessage": f"Airlock task {tid} awaiting operator: review with ! airlock show {tid}, "
                                                          f"then ! airlock -y approve {tid}"}


async def test_stop_allows_terminal_tasks(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    for status in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
        sid, _ = await session_task(client, db, status)
        assert await stop(client, sid) == {"active": False}


async def test_stop_releases_running_task_with_nothing_launchable(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, tid = await session_task(client, db, TaskState.RUNNING)
    await db["delegations"].insert_one({"_id": new_id(Prefix.DELEGATION), "schema_version": 1, "project_id": "proj_x", "task_id": tid,
                                        "assignment_key": "schema", "status": DelegationState.FAILED, "attempt": 2})
    out = await stop(client, sid)
    assert out["active"] is False and "decision" not in out and f"airlock -y cancel {tid}" in out["systemMessage"]


async def test_stop_blocks_approved_task_with_ready_assignment(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, tid = await session_task(client, db, TaskState.APPROVED)
    assert await stop(client, sid) == {"active": True, "decision": "block",
                                       "reason": f"Airlock task {tid} is approved. Next: `schema` is ready: call airlock_delegate('{tid}', 'schema')"}


async def test_stop_blocks_running_task_with_repair_ready_and_live_steps(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, tid = await session_task(client, db, TaskState.RUNNING)
    out = await stop(client, sid)
    assert (out["decision"], out["active"]) == ("block", True)
    for key, status in (("schema", DelegationState.ACCEPTED), ("service", DelegationState.REPAIR_REQUIRED)):
        await db["delegations"].insert_one({"_id": new_id(Prefix.DELEGATION), "schema_version": 1, "project_id": "proj_x", "task_id": tid,
                                            "assignment_key": key, "status": status, "attempt": 1})
    assert (await stop(client, sid))["reason"] == (f"Airlock task {tid} is running. Next: `service` is repair_required: call airlock_delegate('{tid}', "
                                                    f"'service'); `routes` is ready: call airlock_delegate('{tid}', 'routes')")
    await db["delegations"].update_many({"task_id": tid, "assignment_key": "service"}, {"$set": {"status": DelegationState.VALIDATING}})
    assert (await stop(client, sid))["reason"].endswith(f"`routes` is ready: call airlock_delegate('{tid}', 'routes'); "
                                                         f"1 delegation(s) in progress: call airlock_status('{tid}', 50)")


@pytest.fixture
async def bridge(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock", headers=auth("bridge")) as c:
        monkeypatch.setattr(server, "client", c)
        yield


async def tool(name: str, **args: object) -> Doc:
    res = await server.mcp.call_tool(name, args)
    assert isinstance(res, CallToolResult) and not res.is_error and res.structured_content is not None, res
    return res.structured_content


async def test_bridge_exposes_exactly_five_tools() -> None:
    assert sorted(t.name for t in await server.mcp.list_tools()) == \
        ["airlock_delegate", "airlock_plan", "airlock_register", "airlock_run", "airlock_status"]


@pytest.mark.usefixtures("bridge")
async def test_bridge_plan_status_and_delegate(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "bridge-plan")
    planned = await tool("airlock_plan", project_id=proj, parent_session_id="sess_bridge", request="Add login", base_artifact_id="art_base_01")
    task = await db["tasks"].find_one({"_id": planned["task_id"]})
    assert task is not None and (task["parent_session_id"], task["status"], planned["status"]) == ("sess_bridge", "draft", "draft")
    assert (await tool("airlock_status", task_id=planned["task_id"]))["status"] == "draft"
    assert (await tool("airlock_status", task_id=planned["task_id"], full=True))["task"]["status"] == "draft"
    tid, _, _ = await approved_task(client, db, "bridge-deleg")
    launched = await tool("airlock_delegate", task_id=tid, assignment_key="schema")
    assert (launched["status"], launched["attempt"]) == ("queued", 1)
    with pytest.raises(ToolError, match=r"409: .*STALE_REVISION"):
        await server.mcp.call_tool("airlock_delegate", {"task_id": tid, "assignment_key": "schema"})


@pytest.mark.usefixtures("bridge")
async def test_bridge_run_stops_for_approval_then_launches_ready_work(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    _, tid = await session_task(client, db, TaskState.PLANNED)
    out = await tool("airlock_run", task_id=tid, budget_seconds=1)
    assert (out["status"], out["approve"], out["contract"]) == ("planned", f"! airlock -y approve {tid}", CONTRACT)
    tid, _, _ = await approved_task(client, db, "bridge-run")
    out = await tool("airlock_run", task_id=tid, budget_seconds=0)
    assert out["log"] == ["launched schema attempt 1"] and "budget spent" in out["note"]
    assert next((a["key"], a["status"]) for a in out["assignments"]) == ("schema", "running")
