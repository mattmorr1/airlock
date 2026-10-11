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
# Hatch: 12x10 astronaut (dome, blue visor with tall eyes, blue collar, chest light, boots); "." is the dark VOID tile.
PAL: dict[str, tuple[int, int, int]] = {"W": (242, 245, 250), "G": (174, 187, 205), "N": (28, 43, 90), "V": (46, 91, 216), "E": (234, 243, 255),
                                        "L": (156, 194, 255), "B": (61, 123, 247), "A": (52, 211, 153), "R": (244, 63, 94), "Y": (251, 191, 36)}
_HEAD, _VISOR, _COLLAR, _LEGS = [".....WW.....", "..WWWWWWWG..", ".WWLVVVVVWG."], ".WWNVVVVNWG.", "..GBBBBBBG..", ["..GWWWWWWG..", "...BB..BB..."]
_EYES = {"open": [".WVVEVVEVNG."] * 2, "shut": [".WVVVVVVVNG.", ".WVVEEVEENG."], "happy": [".WVVEEVEENG.", ".WVVVVVVVNG."],
         "right": [".WVVVEVVENG."] * 2, "left": [".WVEVVEVVNG."] * 2, "wide": [".WVEEVVEENG."] * 2, "droop": [".WVVVVVVVNG.", ".WVEEVVEENG."]}


def _f(eyes: str, chest: str, **rows: str) -> list[str]:
    grid = [*_HEAD, *_EYES[eyes], _VISOR, _COLLAR, rows.pop("body", f".WGWW{chest}{chest}WWGW."), *_LEGS]
    for k, row in rows.items():
        grid[int(k[1:])] = row
    return grid


def _wave(eyes: str, chest: str, high: bool) -> list[str]:
    """Right hand raised beside the visor, at visor height or one row higher."""
    up = {"r4": _EYES[eyes][1][:11] + "W"} if high else {"r5": ".WWNVVVVNWGW"}
    return _f(eyes, chest, body=f".WGWW{chest}{chest}WWG..", **up)


STATES: dict[str, tuple[list[int], list[list[str]]]] = {
    "idle": ([3200, 450, 3200, 450, 900, 900, 900], [_f("open", "A"), _f("shut", "A"), _f("open", "A"), _f("shut", "A"), _wave("open", "A", False),
                                                     _wave("open", "A", True), _wave("open", "A", False)]),
    "success": ([900, 600, 600, 900], [_f("happy", "A"), _wave("happy", "A", False), _wave("happy", "A", True), _wave("happy", "A", False)]),
    "thinking": ([1100, 700, 1100, 700], [_f("right", "L"), _f("open", "L"), _f("left", "L"), _f("open", "L")]),
    "working": ([600, 600, 600, 600], [_f("open", "B"), _f("open", "L"), _f("open", "B"), _f("open", "W")]),
    "alert": ([800, 800, 2400, 400], [_f("wide", "Y"), _f("wide", "W"), _f("wide", "Y"), _f("shut", "Y")]),
    "alarm": ([500, 500], [_f("wide", "R", r1="W.WWWWWWWG.W", r2="WWWLVVVVVWGW", body="..GWWRRWWG.."), _f("wide", "W", body=".WGWWRRWWGW.")]),
    "sleep": ([1600, 1600], [_f("droop", "G"), _f("droop", "N")]),
}
HATCH = {"content": "idle", "watching": "working", "waiting": "alert", "alarm": "alarm", "unwell": "sleep"}
HATCH_BY_REASON = {"done": "success", "planning": "thinking"}
TONE = {"content": (52, 211, 153), "watching": (116, 229, 255), "waiting": (251, 191, 36), "alarm": (244, 63, 94), "unwell": (160, 171, 184)}
MUTED = (160, 171, 184)
VOID = (17, 19, 30)  # backdrop tile behind Hatch
FACE = {"content": "(^_^)", "watching": "(o_o)", "waiting": "(-_-)", "alarm": "(O_O)!", "unwell": "(x_x)"}
DRIFT = [0, 0, 1, 2, 2, 1, 0, 0, 0, 1]  # columns, one step per 6s: a slow wander that never jumps
DRIFT_MAX, DN = max(DRIFT), "\u2584"
BLANK = "\u2800"  # braille blank: Claude Code strips leading spaces per statusline row, which would shear the sprite
WORD = {("content", "idle"): "airlock idle", ("content", "done"): "task done", ("watching", "merging"): "merging", ("watching", "planning"): "planning",
        ("waiting", "approve"): "waiting on you", ("waiting", "delegate"): "ready to delegate", ("alarm", "repair"): "needs repair",
        ("alarm", "failed"): "failed", ("alarm", "lease"): "lease expired", ("unwell", "stale"): "api unreachable", ("unwell", "offline"): "api unreachable",
        ("unwell", "token"): "no hook token"}


def word(d: dict[str, Any]) -> str:
    return WORD.get((d["mood"], d["reason"])) or str(d["mood"])


def frame_at(state: str, ms: int) -> list[str]:
    """The frame showing `ms` into the state's loop; the statusline samples one per refresh, so short blinks may be skipped."""
    holds, frames = STATES[state]
    t = ms % sum(holds)
    for hold, fr in zip(holds, frames, strict=True):
        if t < hold:
            return fr
        t -= hold
    raise AssertionError("unreachable: t < sum(holds)")


def fg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def bg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def halfblocks(grid: list[str], shift: int) -> list[str]:
    """Hatch on a dark tile he drifts inside. Background fills the whole cell but a font's block glyph may not (line spacing),
    so the top pixel rides on background and only differing bottoms draw a glyph: misfit glyphs skew heights, never leak colour."""
    sky = bg(VOID)
    def cell(top: str, bot: str) -> str:
        t, b = PAL.get(top, VOID), PAL.get(bot, VOID)
        return bg(t) + (BLANK if t == b else fg(b) + DN)
    return [sky + BLANK * shift + "".join(cell(a, b) for a, b in zip(grid[r], grid[r + 1], strict=True)) + sky + BLANK * (DRIFT_MAX - shift) + "\x1b[0m"
            for r in range(0, len(grid), 2)]


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
        lines.append(f"approve: ! airlock -y approve {task['id']}")
    else:
        busy = [f"{p[k]} {k.replace('_required', '')}" for k in ("integrating", "validating", "running", "repair_required") if p.get(k)]
        extra = [f"ready: {', '.join(p['ready'])}"] * bool(p["ready"]) + [f"{k} try {n}/2" for k, n in p["retries"].items()]
        lines.append(" · ".join([f"merged {p['accepted']}/{p['total']} {bar(p['accepted'], p['total'])}", *busy, *extra]))
    lines.append(f"jobs {j['running']} running \u00b7 {j['queued']} queued \u00b7 " + (f"{j['expired']} lease expired" if j["expired"] else "leases ok"))
    last = f" \u00b7 last {d['last']['type']} {age(d['last']['age_s'])} ago" if d.get("last") else ""
    lines.append(f"harness {h['version']} gen {h['generation']} \u00b7 epoch {h['epoch']}{last}")
    lines.append(f"today {today['done']} done \u00b7 {today['merged']} merged \u00b7 {today['failed']} failed \u00b7 {today['promoted']} promoted")
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


def render(d: dict[str, Any], now: float, cols: int) -> list[str]:
    tone, dim, text = fg(TONE[d["mood"]]), fg(MUTED), stats(d)
    if cols < 70:
        return [f"{tone}{FACE[d['mood']]} {text[0][:cols - 8]}\x1b[0m"]
    room, w = cols - 12 - DRIFT_MAX - 2, word(d)
    body = [f"{tone}{w}\x1b[0m{dim}{s[len(w):room]}\x1b[0m" if i == 0 else f"{dim}{s[:room]}\x1b[0m" for i, s in enumerate(text)]
    state = HATCH_BY_REASON.get(d["reason"]) or HATCH[d["mood"]]
    sprite = halfblocks(frame_at(state, int(now * 1000)), DRIFT[(int(now) // 6) % len(DRIFT)])
    return [f"{s}  {body[i] if i < len(body) else ''}".rstrip() for i, s in enumerate(sprite)]


DEMO = [("content", "idle"), ("watching", "planning"), ("watching", "merging"), ("waiting", "approve"), ("alarm", "repair"), ("content", "done"),
        ("unwell", "stale")]


def demo(now: float) -> dict[str, Any]:
    """AIRLOCK_HUD_DEMO=1: cycle every mood with sample stats, 8s each, so the sprite can be previewed without a running API."""
    mood, reason = DEMO[int(now // 8) % len(DEMO)]
    accepted = {"done": 3, "approve": 0, "planning": 0}.get(reason, 1)
    return {"mood": mood, "reason": reason, "fetched_at": now - 95, "today": {"done": 4, "merged": 9, "failed": 1, "promoted": 1},
            "task": {"id": "task_demo42", "status": "running", "request": "Implement login and sessions", "age_s": 300},
            "progress": {"total": 3, "accepted": accepted, "integrating": int(reason == "merging"), "repair_required": int(reason == "repair"),
                         "ready": ["service", "routes"] * (reason == "merging"), "retries": {"routes": 2} if reason == "repair" else {}},
            "jobs": {"running": int(reason == "merging"), "queued": 1, "expired": 0}, "harness": {"version": "hv_002", "generation": 2, "epoch": 1},
            "last": {"type": "delegation.accepted", "age_s": 12}}


def main() -> int:
    raw = sys.stdin.buffer.read()
    if len(sys.argv) > 1:
        up = subprocess.run(sys.argv[1:], input=raw, capture_output=True, timeout=3)
        if up.returncode:
            print(f"airlock hud: upstream exited {up.returncode}: {up.stderr.decode(errors='replace')[:200]}", file=sys.stderr)
        sys.stdout.write(up.stdout.decode(errors="replace").rstrip("\n") + "\n" if up.stdout.strip() else "")
    session = json.loads(raw or b"{}").get("session_id", "")
    now = time.time()
    print("\n".join(render(demo(now) if os.environ.get("AIRLOCK_HUD_DEMO") else fetch(session), now, columns())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
