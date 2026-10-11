#!/usr/bin/env python3
"""Airlock hook adapter: forwards a Claude Code hook payload to the API and prints its native decision (stdlib only)."""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROUTES = {"PreToolUse": "/v1/hooks/pre-tool-use", "Stop": "/v1/hooks/lifecycle"}
TIMEOUT = 2


def main() -> int:
    raw = sys.stdin.buffer.read()
    event = json.loads(raw)
    marker = Path.home() / ".airlock" / "managed" / re.sub(r"[^A-Za-z0-9_-]", "_", event["session_id"])
    req = urllib.request.Request(os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000") + ROUTES[event["hook_event_name"]], data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('AIRLOCK_TOKEN_HOOK', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(r.read())
    except (OSError, ValueError) as e:
        # Fail closed only for sessions the API last reported as managed; Stop is never blocked on an outage.
        print(f"airlock hook: API unavailable ({type(e).__name__}: {e}); managed={marker.exists()}", file=sys.stderr)
        if marker.exists() and event["hook_event_name"] == "PreToolUse":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": "Airlock API is unreachable and this session has an active task; "
                                                                                 "repository writes are blocked until it is back."}}))
        return 0
    if out.pop("active"):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
