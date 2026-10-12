"""Airlock MCP bridge: a stdio MCP server that forwards the parent-session tools to the HTTP API with the bridge token."""
import asyncio
import os
import uuid
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("airlock")
# Read timeout covers the 50 s long-poll ceiling of GET /v1/tasks/{id}.
client = httpx.AsyncClient(base_url=os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000"), timeout=60,
                           headers={"Authorization": f"Bearer {os.environ.get('AIRLOCK_TOKEN_BRIDGE', '')}"})
TERMINAL = ("completed", "failed", "cancelled")


async def call(method: str, path: str, **kw: Any) -> dict[str, Any]:
    """API JSON, or a ToolError carrying the API error envelope; MCP hides other exceptions' text from the model."""
    try:
        r = await client.request(method, path, headers={"Idempotency-Key": str(uuid.uuid4())} if method == "POST" else None, **kw)
    except httpx.RequestError as e:  # connection refused / timeout: the API likely isn't running. MCP would hide a raw exception's text.
        raise ToolError(f"Airlock API not reachable at {client.base_url} ({e.__class__.__name__}). Start the stack with scripts/up.sh, "
                        "then retry.") from e
    if r.is_error:
        raise ToolError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def compact(view: dict[str, Any]) -> dict[str, Any]:
    """The task as a parent session needs it: per-assignment progress, the next step, and the full contract only while awaiting approval."""
    t, by_key = view["task"], {d["assignment_key"]: d for d in view["delegations"]}
    tid, contract = t["_id"], t.get("contract") or {}
    out = {"task_id": tid, **{k: t.get(k) for k in ("status", "request", "base_artifact_id", "final_artifact_id", "next", "last_error")},
           "assignments": [{"key": a["key"], "depends_on": a["depends_on"], "status": d["status"] if (d := by_key.get(a["key"])) else "not_delegated",
                            "attempt": d["attempt"] if d else 0, "last_decision": d.get("last_decision") if d else None}
                           for a in contract.get("assignments", [])]}
    if t["status"] == "planned":
        out |= {"contract": contract, "review": f"! airlock show {tid}", "approve": f"! airlock -y approve {tid}"}
    out["summary"] = summarize(out, tid)
    return out


def summarize(out: dict[str, Any], tid: str) -> str:
    """One human-readable line for the user: status, assignment progress, and the next action."""
    asn = out["assignments"]
    n, done = len(asn), sum(a["status"] == "accepted" for a in asn)
    status, counts = out["status"], f"{done}/{n} assignments accepted" if n else "no assignments yet"
    tail = {"planned": f"awaiting your approval: ! airlock -y approve {tid}",
            "completed": f"done -> {out.get('final_artifact_id')}",
            "failed": out.get("last_error") or "failed",
            "cancelled": "cancelled"}.get(status, out.get("next") or "in progress")
    return f"{status} - {counts} - {tail}"


@mcp.tool()
async def airlock_register(cwd: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Idempotently register this session's project (default: current directory) with Airlock, returning its project_id and
    the live dashboard URL to show the user. Safe to call every run: an already-registered repo returns its existing project.
    Call before airlock_plan."""
    r = await call("POST", "/v1/projects/register", json={"cwd": cwd or os.getcwd(), **({"name": name} if name else {})})
    dash = os.environ.get("AIRLOCK_DASHBOARD_URL", "http://127.0.0.1:8099")
    return r | {"dashboard_url": f"{dash}/?project={r['project_id']}"}


@mcp.tool()
async def airlock_plan(parent_session_id: str, request: str, cwd: str | None = None, project_id: str | None = None,
                       base_artifact_id: str | None = None, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create an Airlock task for the project registered at `cwd` (default: this session's directory), based on its latest integrated tree.
    Pass `contract` (constraints, acceptance, interfaces, assignments, checks, tests) to plan it yourself; it then goes straight to planned."""
    body = {"parent_session_id": parent_session_id, "request": request, "cwd": None if project_id else cwd or os.getcwd(),
            "project_id": project_id, "base_artifact_id": base_artifact_id, "contract": contract}
    return await call("POST", "/v1/tasks", json={k: v for k, v in body.items() if v is not None})


@mcp.tool()
async def airlock_status(task_id: str, wait_seconds: float = 0, full: bool = False) -> dict[str, Any]:
    """Compact task state (full=True for raw documents); wait_seconds (at most 50) long-polls until the task's next event."""
    view = await call("GET", f"/v1/tasks/{task_id}", params={"wait_seconds": wait_seconds})
    return view if full else compact(view)


@mcp.tool()
async def airlock_delegate(task_id: str, assignment_key: str) -> dict[str, Any]:
    """Launch (or repair) one approved assignment as an isolated worker; 409 CAPACITY is retryable after airlock_status."""
    return await call("POST", f"/v1/tasks/{task_id}/delegations", json={"assignment_key": assignment_key})


@mcp.tool()
async def airlock_run(task_id: str, budget_seconds: float = 300) -> dict[str, Any]:
    """Drive a task: delegate every ready or repair_required assignment and wait, returning once it finishes, needs operator approval,
    or the budget runs out (then call again). Each launch and refusal is listed in `log`."""
    loop, log = asyncio.get_running_loop(), []
    deadline, wait = loop.time() + budget_seconds, 0.0
    while True:
        view = await call("GET", f"/v1/tasks/{task_id}", params={"wait_seconds": wait})
        status = view["task"]["status"]
        if status in TERMINAL or status == "planned":
            return compact(view) | {"log": log}
        for key in view["task"]["launchable"]:
            try:
                r = await airlock_delegate(task_id, key)
                log.append(f"launched {key} attempt {r['attempt']}")
            except ToolError as e:
                log.append(f"{key}: {e}")
        if (left := deadline - loop.time()) <= 0:
            view = await call("GET", f"/v1/tasks/{task_id}") if log else view
            return compact(view) | {"log": log, "note": "budget spent; call airlock_run again to keep driving"}
        wait = min(50.0, left)


if __name__ == "__main__":
    mcp.run("stdio")
