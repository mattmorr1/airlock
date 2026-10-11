import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

HOOK = Path(__file__).parent.parent / "plugin" / "scripts" / "hook.py"
PRE: dict[str, Any] = {"session_id": "sess_abc", "hook_event_name": "PreToolUse", "cwd": "/r", "tool_name": "Edit", "tool_input": {"file_path": "/r/a.py"}}
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "nope"}}


class Stub(BaseHTTPRequestHandler):
    reply: ClassVar[dict[str, object]] = {}
    seen: ClassVar[list[tuple[str, str, object]]] = []

    def do_POST(self) -> None:
        Stub.seen.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
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


def run(url: str, home: Path, event: dict[str, Any] = PRE) -> tuple[Any, str]:
    env = os.environ | {"HOME": str(home), "AIRLOCK_URL": url, "AIRLOCK_TOKEN_HOOK": "hk-secret"}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True, env=env, timeout=10, check=True)
    assert "hk-secret" not in p.stdout + p.stderr
    return (json.loads(p.stdout) if p.stdout.strip() else None), p.stderr


def marker(home: Path) -> Path:
    return home / ".airlock" / "managed" / "sess_abc"


def test_active_reply_writes_marker_and_prints_native_decision(stub: str, tmp_path: Path) -> None:
    Stub.reply = {"active": True, **DENY}
    assert run(stub, tmp_path)[0] == DENY
    assert marker(tmp_path).exists()
    assert Stub.seen == [("/v1/hooks/pre-tool-use", "Bearer hk-secret", PRE)]


def test_inactive_reply_removes_marker(stub: str, tmp_path: Path) -> None:
    marker(tmp_path).parent.mkdir(parents=True)
    marker(tmp_path).touch()
    Stub.reply = {"active": False, "systemMessage": "awaiting operator"}
    assert run(stub, tmp_path, PRE | {"hook_event_name": "Stop"})[0] == {"systemMessage": "awaiting operator"}
    assert not marker(tmp_path).exists()
    assert Stub.seen[0][0] == "/v1/hooks/lifecycle"


def test_api_down_denies_managed_session(closed_port: str, tmp_path: Path) -> None:
    marker(tmp_path).parent.mkdir(parents=True)
    marker(tmp_path).touch()
    out, err = run(closed_port, tmp_path)
    assert (out["hookSpecificOutput"]["permissionDecision"], "unreachable" in out["hookSpecificOutput"]["permissionDecisionReason"]) == ("deny", True)
    assert "API unavailable" in err and marker(tmp_path).exists()


def test_api_down_allows_unmanaged_session_and_stop(closed_port: str, tmp_path: Path) -> None:
    assert run(closed_port, tmp_path)[0] is None
    marker(tmp_path).parent.mkdir(parents=True)
    marker(tmp_path).touch()
    assert run(closed_port, tmp_path, PRE | {"hook_event_name": "Stop"})[0] is None
