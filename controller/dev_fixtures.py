"""DEV/TEST ONLY scripted worker edits.

These stand in for a real Claude coding session so the full loop runs offline.
They are selected only when execution_mode == "local" and a job payload names a
fixture. Never used in the container backend.
"""
from __future__ import annotations

from pathlib import Path

LEAK_LINE = 'logger.error("login failed email=%s password=%s", email, password)'
FIXED_LINE = 'logger.error("login failed email=%s", email)  # no credential in logs'


def fix_credential_leak(checkout: Path, payload: dict) -> dict:
    svc = checkout / "src/auth/service.py"
    text = svc.read_text()
    if LEAK_LINE in text:
        svc.write_text(text.replace(LEAK_LINE, FIXED_LINE))
        return {"edits": 1, "fixture": "fix_credential_leak"}
    return {"edits": 0, "fixture": "fix_credential_leak"}


def noop(checkout: Path, payload: dict) -> dict:
    return {"edits": 0, "fixture": "noop"}


FIXTURES = {"fix_credential_leak": fix_credential_leak, "noop": noop}
