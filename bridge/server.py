"""Airlock MCP bridge: a stdio MCP server that forwards three parent-session tools to the HTTP API with the bridge token."""
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


async def call(method: str, path: str, **kw: Any) -> dict[str, Any]:
    """API JSON, or a ToolError carrying the API error envelope; MCP hides other exceptions' text from the model."""
    r = await client.request(method, path, headers={"Idempotency-Key": str(uuid.uuid4())} if method == "POST" else None, **kw)
    if r.is_error:
        raise ToolError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


@mcp.tool()
async def airlock_plan(project_id: str, parent_session_id: str, request: str, base_artifact_id: str) -> dict[str, Any]:
    """Create a draft Airlock task; the controller plans it and the operator approves with `! airlock -y approve <task_id>`."""
    return await call("POST", "/v1/tasks", json={"project_id": project_id, "parent_session_id": parent_session_id, "request": request,
                                                 "base_artifact_id": base_artifact_id})


@mcp.tool()
async def airlock_status(task_id: str, wait_seconds: float = 0) -> dict[str, Any]:
    """Task and delegation states; wait_seconds (at most 50) long-polls until the task's next event."""
    return await call("GET", f"/v1/tasks/{task_id}", params={"wait_seconds": wait_seconds})


@mcp.tool()
async def airlock_delegate(task_id: str, assignment_key: str) -> dict[str, Any]:
    """Launch (or repair) one approved assignment as an isolated worker; 409 CAPACITY is retryable after airlock_status."""
    return await call("POST", f"/v1/tasks/{task_id}/delegations", json={"assignment_key": assignment_key})


if __name__ == "__main__":
    mcp.run("stdio")
