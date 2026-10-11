import importlib
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase
from test_api import TOKENS, auth
from test_hooks import session_task

from api.main import app
from controller.db import Doc
from controller.states import DelegationState, JobKind, JobStatus, Prefix, TaskState, new_id

HUD = Path(__file__).parent.parent / "plugin" / "scripts" / "hud.py"


@pytest.fixture
async def client(db: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for role, tok in TOKENS.items():
        monkeypatch.setenv(f"AIRLOCK_TOKEN_{role.upper()}", tok)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
        yield c


async def hud(client: httpx.AsyncClient, sid: str) -> Doc:
    r = await client.get("/v1/hud", params={"session_id": sid}, headers=auth("hook"))
    assert r.status_code == 200, r.text
    return r.json()


async def delegation(db: AsyncDatabase[Doc], tid: str, key: str, status: DelegationState, attempt: int = 1) -> None:
    task = await db["tasks"].find_one({"_id": tid})
    assert task
    await db["delegations"].insert_one({"_id": new_id(Prefix.DELEGATION), "schema_version": 1, "project_id": task["project_id"], "task_id": tid,
                                        "assignment_key": key, "status": status, "attempt": attempt})


async def job(db: AsyncDatabase[Doc], tid: str, status: JobStatus, expires_in: float = 30) -> None:
    now = datetime.now(UTC)
    await db["jobs"].insert_one({"_id": new_id(Prefix.JOB), "schema_version": 1, "kind": JobKind.LAUNCH, "status": status, "task_id": tid,
                                 "dedupe_key": new_id(Prefix.JOB), "available_at": now, "created_at": now,
                                 "lease": {"owner": "w1", "fence": 1, "expires_at": now + timedelta(seconds=expires_in)}})


@pytest.mark.anyio
async def test_hud_needs_hook_token_and_is_idle_without_a_task(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/hud", params={"session_id": "s"}, headers=auth("reader"))).status_code == 403
    assert await hud(client, "sess_none") == {"mood": "content", "reason": "idle", "task": None}


@pytest.mark.anyio
async def test_hud_moods_follow_task_and_delegation_state(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, _ = await session_task(client, db, TaskState.PLANNED)
    assert (await hud(client, sid))["mood"] == "waiting" and (await hud(client, sid))["reason"] == "approve"
    sid, tid = await session_task(client, db, TaskState.RUNNING)
    out = await hud(client, sid)
    assert (out["mood"], out["reason"], out["progress"]["ready"]) == ("waiting", "delegate", ["schema"])
    await delegation(db, tid, "schema", DelegationState.ACCEPTED)
    await delegation(db, tid, "service", DelegationState.INTEGRATING)
    await job(db, tid, JobStatus.RUNNING)
    await job(db, tid, JobStatus.QUEUED)
    out = await hud(client, sid)
    assert (out["mood"], out["reason"]) == ("watching", "merging")
    assert (out["progress"]["accepted"], out["progress"]["integrating"], out["progress"]["total"], out["progress"]["ready"]) == (1, 1, 3, ["routes"])
    assert out["jobs"] == {"running": 1, "queued": 1, "expired": 0} and out["harness"]["version"] == "hv_001" and out["task"]["id"] == tid
    await job(db, tid, JobStatus.RUNNING, expires_in=-5)
    assert ((await hud(client, sid))["mood"], (await hud(client, sid))["jobs"]["expired"]) == ("alarm", 1)
    await delegation(db, tid, "routes", DelegationState.REPAIR_REQUIRED, attempt=2)
    out = await hud(client, sid)
    assert (out["mood"], out["reason"], out["progress"]["retries"]) == ("alarm", "repair", {"routes": 2})


@pytest.mark.anyio
async def test_hud_counts_todays_project_events_only(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    sid, tid = await session_task(client, db, TaskState.RUNNING)
    task = await db["tasks"].find_one({"_id": tid})
    assert task
    now = datetime.now(UTC)
    events = [("task.completed", now), ("delegation.accepted", now), ("delegation.accepted", now), ("job.failed", now), ("harness.promoted", now),
              ("delegation.accepted", now - timedelta(days=2)), ("task.approved", now)]
    await db["events"].insert_many([{"_id": new_id(Prefix.EVENT), "schema_version": 1, "project_id": task["project_id"], "stream_id": tid, "sequence": i,
                                     "type": t, "created_at": at} for i, (t, at) in enumerate(events, 1)])
    out = await hud(client, sid)
    assert out["today"] == {"done": 1, "merged": 2, "failed": 1, "promoted": 1}
    assert out["last"]["type"] == "task.approved"


class Stub(BaseHTTPRequestHandler):
    reply: ClassVar[dict[str, Any]] = {}
    seen: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        Stub.seen.append((self.path, self.headers["Authorization"]))
        body = json.dumps(Stub.reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def stub() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Stub.seen.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def closed_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


WATCHING: dict[str, Any] = {
    "mood": "watching", "reason": "merging", "task": {"id": "task_abc", "status": "running", "request": "Implement login", "age_s": 30},
    "progress": {"total": 3, "accepted": 1, "integrating": 1, "ready": [], "retries": {}}, "jobs": {"running": 1, "queued": 0, "expired": 0},
    "harness": {"version": "hv_002", "generation": 2, "epoch": 1}, "last": {"type": "delegation.accepted", "age_s": 4},
    "today": {"done": 0, "merged": 1, "failed": 0, "promoted": 0}}
UPSTREAM = [sys.executable, "-c", "import sys, json; print('upstream:' + json.load(sys.stdin)['session_id'])"]


def run(url: str, home: Path, cols: int = 140, token: str | None = "hk-secret", upstream: list[str] = UPSTREAM) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "AIRLOCK_TOKEN_HOOK"} | {"HOME": str(home), "AIRLOCK_URL": url, "COLUMNS": str(cols)}
    env |= {"AIRLOCK_TOKEN_HOOK": token} if token else {}
    p = subprocess.run([sys.executable, str(HUD), *upstream], input=json.dumps({"session_id": "sess_abc"}), capture_output=True, text=True, env=env,
                       timeout=10, check=True)
    assert "hk-secret" not in p.stdout + p.stderr
    return p.stdout.splitlines()


def plain(lines: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", s) for s in lines)


def test_script_prints_upstream_then_five_line_sprite_and_caches(stub: str, tmp_path: Path) -> None:
    Stub.reply = WATCHING
    lines = run(stub, tmp_path)
    assert lines[0] == "upstream:sess_abc" and len(lines) == 6
    assert "\x1b[38;2;" in lines[1] and "▄" in "".join(lines[1:])
    text = plain(lines)
    assert "merging · task_abc" in text and "merged 1/3" in text and "harness hv_002 gen 2" in text and "leases ok" in text
    assert Stub.seen == [("/v1/hud?session_id=sess_abc", "Bearer hk-secret")]
    assert json.loads((tmp_path / ".airlock" / "hud" / "sess_abc.json").read_text())["mood"] == "watching"


def test_script_shows_stale_cache_when_api_is_down(stub: str, closed_port: str, tmp_path: Path) -> None:
    Stub.reply = WATCHING
    run(stub, tmp_path)
    text = plain(run(closed_port, tmp_path))
    assert "api unreachable · stale" in text and "merged 1/3" in text


def test_script_offline_tokenless_and_narrow(closed_port: str, stub: str, tmp_path: Path) -> None:
    assert "api unreachable" in plain(run(closed_port, tmp_path, upstream=[]))
    assert "no hook token" in plain(run(stub, tmp_path, token=None, upstream=[]))
    Stub.reply = WATCHING
    narrow = run(stub, tmp_path, cols=60, upstream=[])
    assert len(narrow) == 1 and plain(narrow).startswith("(o_o) merging")


def test_idle_hatch_moves_a_little_but_rarely() -> None:
    sys.path.insert(0, str(HUD.parent))
    h: Any = importlib.import_module("hud")
    poses: list[list[str]] = [h.halfblocks(h.frame_at("idle", t * 1000), h.DRIFT[(t // 6) % len(h.DRIFT)]) for t in range(60)]
    changes = sum(a != b for a, b in itertools.pairwise(poses))
    assert 0 < changes <= 30 and {len(plain(p).splitlines()) for p in poses} == {5}
    assert not any(row[:1].isspace() for p in poses for row in p), "Claude Code strips leading spaces, shearing the sprite"
    assert all(len(f) == 10 and len(r) == 12 and set(r) <= {*h.PAL, "."} for _, frames in h.STATES.values() for f in frames for r in f)
    assert all(len(holds) == len(frames) for holds, frames in h.STATES.values())
