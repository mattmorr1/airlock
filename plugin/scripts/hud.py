#!/usr/bin/env python3
"""Airlock statusline: runs the upstream statusline (argv), then draws Hatch the astronaut beside live Airlock stats (stdlib only)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT = 0.3
HOME = Path.home() / ".airlock"
PAL = {"W": "#F2F5FA", "G": "#AEBBCD", "N": "#1C2B5A", "V": "#2E5BD8", "E": "#EAF3FF", "L": "#9CC2FF", "B": "#3D7BF7", "A": "#34D399", "R": "#F43F5E",
       "Y": "#FBBF24"}
HEAD, VISOR, COLLAR, LEGS = [".....WW.....", "..WWWWWWWG..", ".WWLVVVVVWG."], ".WWNVVVVNWG.", "..GBBBBBBG..", ["..GWWWWWWG..", "...BB..BB..."]
EYES = {"open": [".WVVEVVEVNG."] * 2, "shut": [".WVVVVVVVNG.", ".WVVEEVEENG."], "right": [".WVVVEVVENG."] * 2, "left": [".WVEVVEVVNG."] * 2,
        "wide": [".WVEEVVEENG."] * 2, "droop": [".WVVVVVVVNG.", ".WVEEVVEENG."]}
TONE = {"content": "#34D399", "watching": "#60A5FA", "waiting": "#FBBF24", "alarm": "#F43F5E", "unwell": "#A3E635"}
FACE = {"content": "(^_^)", "watching": "(o_o)", "waiting": "(-_-)", "alarm": "(O_O)!", "unwell": "(x_x)"}
DRIFT = [0, 0, 1, 2, 2, 1, 0, 0, 0, 1]  # columns, one step per 6s: a slow wander that never jumps
DRIFT_MAX, UP, DN = max(DRIFT), "\u2580", "\u2584"
WORD = {("content", "idle"): "airlock idle", ("content", "done"): "task done", ("watching", "merging"): "merging", ("watching", "planning"): "planning",
        ("waiting", "approve"): "waiting on you", ("waiting", "delegate"): "ready to delegate", ("alarm", "repair"): "needs repair",
        ("alarm", "failed"): "failed", ("alarm", "lease"): "lease expired", ("unwell", "stale"): "api unreachable", ("unwell", "offline"): "api unreachable",
        ("unwell", "token"): "no hook token"}


def word(d: dict[str, Any]) -> str:
    return WORD.get((d["mood"], d["reason"])) or str(d["mood"])


def frame(eyes: str, chest: str, **rows: str) -> list[str]:
    grid = [*HEAD, *EYES[eyes], VISOR, COLLAR, rows.pop("body", f".WGWW{chest}{chest}WWGW."), *LEGS]
    for i, row in rows.items():
        grid[int(i[1:])] = row
    return grid


def pose(mood: str, t: int) -> list[str]:
    """Mostly still: idle moods blink every 7s and wave once a minute; busy moods animate gently, alarm is meant to grab attention."""
    if mood == "content":
        if t % 60 in (30, 31):
            return frame("open", "A", r5=".WWNVVVVNWGW", body=".WGWWAAWWG..")
        return frame("shut" if t % 7 == 0 else "open", "A")
    if mood == "watching":
        return frame(("right", "open", "left", "open")[(t // 2) % 4], "L" if t % 4 < 2 else "B")
    if mood == "waiting":
        return frame("shut" if t % 9 == 0 else "open", "Y" if (t // 2) % 2 else "W")
    if mood == "alarm":
        return frame("wide", "R", r1="W.WWWWWWWG.W", r2="WWWLVVVVVWGW", body="..GWWRRWWG..") if t % 2 else frame("wide", "W", body=".WGWWRRWWGW.")
    return frame("droop", "G" if (t // 3) % 2 else "N")


def c256(hex_: str) -> int:
    rgb = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
    lv = [0, 95, 135, 175, 215, 255]
    cube = [min(range(6), key=lambda k: abs(lv[k] - c)) for c in rgb]
    gray = min(range(24), key=lambda k: abs(8 + 10 * k - sum(rgb) / 3))
    dc, dg = sum((lv[q] - c) ** 2 for q, c in zip(cube, rgb, strict=True)), sum((8 + 10 * gray - c) ** 2 for c in rgb)
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2] if dc <= dg else 232 + gray


def halfblocks(grid: list[str], shift: int) -> list[str]:
    def cell(top: str, bot: str) -> str:
        t, b = PAL.get(top), PAL.get(bot)
        if t and b:
            return f"\x1b[38;5;{c256(t)}m\x1b[48;5;{c256(b)}m{UP}\x1b[0m"
        return f"\x1b[38;5;{c256(t or b or '#000000')}m{UP if t else DN}\x1b[0m" if t or b else " "
    return [" " * shift + "".join(cell(a, b) for a, b in zip(grid[r], grid[r + 1], strict=True)) + " " * (DRIFT_MAX - shift) for r in range(0, len(grid), 2)]


def age(s: int) -> str:
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h"


def bar(done: int, total: int, width: int = 8) -> str:
    fill = round(width * done / total) if total else 0
    return "█" * fill + "░" * (width - fill)


def stats(d: dict[str, Any]) -> list[str]:
    """Plain-text stat lines; the first carries the mood word and is colored by the caller."""
    head = word(d)
    if d["reason"] == "stale":
        head += f" · stale {age(int(time.time() - d['fetched_at']))}"
    if (task := d.get("task")) is None:
        hint = {"token": "set AIRLOCK_TOKEN_HOOK or ~/.airlock/hook.token", "offline": f"is the API up at {url()}?"}.get(d["reason"], "/airlock:run <goal>")
        return [head, hint]
    p, j, h, today = d["progress"], d["jobs"], d["harness"], d["today"]
    lines = [f"{head} · {task['id']} · {task['request']}"]
    if d["reason"] == "approve":
        lines.append(f"approve: ! airlock approve {task['id']}")
    else:
        busy = [f"{p[k]} {k.replace('_required', '')}" for k in ("integrating", "validating", "running", "repair_required") if p.get(k)]
        extra = [f"ready: {', '.join(p['ready'])}"] * bool(p["ready"]) + [f"{k} try {n}/2" for k, n in p["retries"].items()]
        lines.append(" · ".join([f"merged {p['accepted']}/{p['total']} {bar(p['accepted'], p['total'])}", *busy, *extra]))
    lines.append(f"jobs {j['running']} running · {j['queued']} queued · " + (f"{j['expired']} lease expired" if j["expired"] else "leases ok"))
    last = f" · last {d['last']['type']} {age(d['last']['age_s'])} ago" if d.get("last") else ""
    lines.append(f"harness {h['version']} gen {h['generation']} · epoch {h['epoch']}{last}")
    lines.append(f"today {today['done']} done · {today['merged']} merged · {today['failed']} failed · {today['promoted']} promoted")
    return lines


def url() -> str:
    return os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000")


def token() -> str:
    f = HOME / "hook.token"
    return os.environ.get("AIRLOCK_TOKEN_HOOK") or (f.read_text().strip() if f.exists() else "")


def fetch(session_id: str) -> dict[str, Any]:
    cache = HOME / "hud" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', session_id)}.json"
    if not (tok := token()):
        return {"mood": "unwell", "reason": "token", "task": None}
    req = urllib.request.Request(f"{url()}/v1/hud?{urllib.parse.urlencode({'session_id': session_id})}", headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d: dict[str, Any] = json.loads(r.read())
    except (OSError, ValueError) as e:
        print(f"airlock hud: API unavailable ({type(e).__name__}: {e})", file=sys.stderr)
        offline: dict[str, Any] = {"mood": "unwell", "reason": "offline", "task": None}
        return {**json.loads(cache.read_text()), "mood": "unwell", "reason": "stale"} if cache.exists() else offline
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({**d, "fetched_at": time.time()}))
    return d


def columns() -> int:
    if (c := os.environ.get("COLUMNS", "")).isdigit():
        return int(c)
    try:
        with open("/dev/tty") as tty:
            return os.get_terminal_size(tty.fileno()).columns
    except OSError:
        return 120


def render(d: dict[str, Any], t: int, cols: int) -> list[str]:
    tone, dim, text = c256(TONE[d["mood"]]), 245, stats(d)
    if cols < 70:
        return [f"\x1b[38;5;{tone}m{FACE[d['mood']]} {text[0][:cols - 8]}\x1b[0m"]
    room = cols - 12 - DRIFT_MAX - 2
    w = word(d)
    body = [(f"\x1b[38;5;{tone}m{w}\x1b[0m\x1b[38;5;{dim}m{s[len(w):room]}\x1b[0m" if i == 0 else f"\x1b[38;5;{dim}m{s[:room]}\x1b[0m")
            for i, s in enumerate(text)]
    sprite = halfblocks(pose(d["mood"], t), DRIFT[(t // 6) % len(DRIFT)])
    return [f"{s}  {body[i] if i < len(body) else ''}".rstrip() for i, s in enumerate(sprite)]


def main() -> int:
    raw = sys.stdin.buffer.read()
    if len(sys.argv) > 1:
        up = subprocess.run(sys.argv[1:], input=raw, capture_output=True, timeout=3)
        if up.returncode:
            print(f"airlock hud: upstream exited {up.returncode}: {up.stderr.decode(errors='replace')[:200]}", file=sys.stderr)
        sys.stdout.write(up.stdout.decode(errors="replace").rstrip("\n") + "\n" if up.stdout.strip() else "")
    session = json.loads(raw or b"{}").get("session_id", "")
    print("\n".join(render(fetch(session), int(time.time()), columns())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
