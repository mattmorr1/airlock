"""Live, read-only Airlock dashboard: MongoDB change streams push a re-render over SSE on every write to the project.

Run:  AIRLOCK_MONGO_URI=... AIRLOCK_DB=airlock_plugin uvicorn ui.server:app --port 8099
Open: http://127.0.0.1:8099/  (newest project) or /?project=<project_id>
"""
from __future__ import annotations

import asyncio
import html
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pymongo.asynchronous.database import AsyncDatabase

from controller import db as dbmod
from controller.db import Doc

NEWEST: Any = [("created_at", -1)]
GOOD = {"pass", "completed", "accepted", "promoted", "eligible", "finished", "done"}
BAD = {"fail", "failed", "repair_required", "rejected", "cancelled", "error", "conflicted"}
STEPS = ["draft", "planned", "approved", "running", "completed"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with dbmod.client() as c:
        app.state.db = dbmod.database(c)
        yield


app = FastAPI(title="airlock-ui", lifespan=lifespan)


def esc(x: object) -> str:
    return html.escape(str(x))


def pill(status: object) -> str:
    s = str(status)
    cls, icon = ("good", "✓") if s in GOOD else ("bad", "✗") if s in BAD else ("warn", "•") if s != "None" else ("mut", "-")
    return f'<span class="pill {cls}">{icon} {esc(s)}</span>'


def hms(d: Any) -> str:
    return d.strftime("%H:%M:%S") if d else "-"


def mode(w: Doc) -> str:
    m = w.get("metrics") or {}
    if m.get("mode") == "claude-sdk":
        return f'<span class="tag sdk">claude-sdk · {m.get("num_turns", "?")} turns · ${m.get("total_cost_usd") or 0:.2f}</span>'
    if m.get("mode") == "scripted":
        return f'<span class="tag">scripted stand-in · {esc(m.get("fixture"))}</span>'
    return '<span class="tag">mode unrecorded</span>'


def checks(v: Doc | None) -> str:
    if not v:
        return '<div class="mut">validation pending…</div>'
    chips = "".join(f'<span class="chk {"ok" if c["status"] == "pass" else "no"}">{"✓" if c["status"] == "pass" else "✗"} {esc(c["id"])}</span>'
                    for c in v.get("checks", []))
    evi = "".join(f'<div class="evi">rejected batch still changed item <code>{esc(ev.get("id"))}</code>: '
                  f'<span class="ok">{esc(ev.get("before"))}</span> → <span class="no">{esc(ev.get("after"))}</span></div>'
                  for c in v.get("checks", []) for ev in c.get("evidence") or [])
    return f'<div class="chips">{chips}</div>{evi}'


def task_card(t: Doc, dels: list[Doc], wrs: list[Doc], vals: dict[str, Doc], integs: list[Doc]) -> str:
    at = STEPS.index(t["status"]) if t["status"] in STEPS else -1
    stepper = "".join(f'<span class="step {"done" if i < at else "now" if i == at else ""}">{s}</span>' for i, s in enumerate(STEPS))
    if at == -1:
        stepper += f" {pill(t['status'])}"
    by_key = {d["assignment_key"]: d for d in dels if d["task_id"] == t["_id"]}
    rows = []
    for a in (t.get("contract") or {}).get("assignments", []):
        d = by_key.get(a["key"])
        deps = f' <span class="mut">after {esc(", ".join(a["depends_on"]))}</span>' if a.get("depends_on") else ""
        rows.append(f'<div class="asn"><div class="row"><b>{esc(a["key"])}</b>{deps} <code class="mut">{esc(", ".join(a.get("file_scope", [])))}</code>'
                    f' {pill(d["status"] if d else "not delegated")}</div><div class="mut">{esc(a.get("objective", ""))}</div>')
        for w in sorted((w for w in wrs if d and w["delegation_id"] == d["_id"]), key=lambda w: w["attempt"]):
            v = vals.get(w.get("output_artifact_id") or "")
            rows.append(f'<div class="att"><div class="row">attempt {w["attempt"]} {pill(w["status"])} {mode(w)}'
                        f' <span class="mut">frozen <code>{esc(w.get("output_artifact_id") or "-")}</code></span>'
                        f' → verdict {pill(v.get("decision") if v else None)}</div>{checks(v) if w.get("output_artifact_id") else ""}</div>')
        for i in (i for i in integs if d and i.get("delegation_id") == d["_id"]):
            rows.append(f'<div class="att"><div class="row">integration {pill(i["status"])} <span class="mut">result '
                        f'<code>{esc(i.get("result_artifact_id"))}</code> · fence {esc(i.get("integration_fence"))}</span></div>'
                        f'{checks(vals.get(i.get("result_artifact_id") or ""))}</div>')
        rows.append("</div>")
    if c := t.get("contract"):
        lists = {"constraints": c.get("constraints", []), "acceptance": c.get("acceptance", []),
                 "generated checks": [f"{x['id']}: {x['description']}" for x in [*c.get("checks", []), *c.get("tests", [])]]}
        items = "".join(f'<div class="mut">{esc(h)}</div><ul>{"".join(f"<li>{esc(i)}</li>" for i in v)}</ul>' for h, v in lists.items() if v)
        rows.insert(0, f'<details{" open" if t["status"] == "planned" else ""}><summary>contract <code class="mut">{esc(t.get("contract_digest"))}</code>'
                       f'</summary>{items}</details>')
    return (f'<div class="card"><div class="row"><b>{esc(t["request"])}</b> <span class="mut">{esc(t["_id"])} · {hms(t.get("created_at"))}</span></div>'
            f'<div class="stepper">{stepper}</div>{"".join(rows) or "<div class=mut>planning…</div>"}'
            f'{"<div class=mut>final artifact <code>" + esc(t["final_artifact_id"]) + "</code></div>" if t.get("final_artifact_id") else ""}</div>')


def evolution(e: Doc, hv: dict[str, Doc]) -> str:
    m = (e.get("evaluation") or {}).get("metrics") or {}
    label = lambda h: f"v{hv[h]['version']}" if h in hv else esc(h)  # noqa: E731
    body = "".join(f"<li>{esc(c)}</li>" for c in e.get("changes", []))
    stats = (f'<div class="tiles"><div class="tile"><div class="lbl">escaped failures (baseline → candidate)</div>'
             f'<div class="hero"><span class="no">{m["baseline"]["escaped_failures"]}</span> → '
             f'<span class="ok">{m["candidate"]["escaped_failures"]}</span></div></div>'
             f'<div class="tile"><div class="lbl">legitimate work blocked</div><div class="val">{m["candidate"]["legitimate_blocks"]}</div></div>'
             f'<div class="tile"><div class="lbl">comparison set</div><div class="val">{m["comparison_set_size"]} runs</div></div></div>') if m else ""
    return (f'<div class="card"><div class="row"><b>{label(e.get("baseline_harness_version_id"))} → {label(e.get("candidate_harness_version_id"))}</b>'
            f' {pill(e.get("status"))} <span class="mut">{esc(e["_id"])}</span></div>'
            f'<div class="mut">{esc(e.get("hypothesis", ""))}</div><ul>{body}</ul>{stats}</div>')


async def render(db: AsyncDatabase[Doc], pid: str | None) -> str:
    if pid is None or (proj := await db["projects"].find_one({"_id": pid})) is None:
        return f'<div class="card mut">no project {esc(pid or "")} in this database yet; run a demo script.</div>'
    q = {"project_id": pid}
    tasks, dels, wrs, vals, integs, evos, hvs, evts, jobs = await asyncio.gather(
        db["tasks"].find(q).sort(NEWEST).limit(3).to_list(), db["delegations"].find(q).to_list(), db["worker_runs"].find(q).to_list(),
        db["validation_runs"].find(q).sort("created_at", 1).to_list(), db["integration_runs"].find(q).to_list(),
        db["evolution_runs"].find(q).sort(NEWEST).to_list(), db["harness_versions"].find(q).sort("version", 1).to_list(),
        db["events"].find(q).sort(NEWEST).limit(25).to_list(),
        (await db["jobs"].aggregate([{"$match": q}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])).to_list())
    hv = {h["_id"]: h for h in hvs}
    vby = {v["inputs"]["candidate_artifact_id"]: v for v in vals if (v.get("inputs") or {}).get("candidate_artifact_id")}
    active = proj.get("active_harness_version_id")
    jc = {j["_id"]: j["n"] for j in jobs}
    head = (f'<div class="tiles"><div class="tile"><div class="lbl">active harness</div>'
            f'<div class="val">{"v" + str(hv[active]["version"]) if active in hv else esc(active)}</div>'
            f'<div class="mut">generation {esc(proj.get("harness_generation"))} · epoch {esc(proj.get("constraint_epoch"))}</div></div>'
            f'<div class="tile"><div class="lbl">tasks shown</div><div class="val">{len(tasks)}</div>'
            f'<div class="mut">{sum(t["status"] == "completed" for t in tasks)} completed</div></div>'
            f'<div class="tile"><div class="lbl">job queue</div><div class="val">{jc.get("running", 0)} running</div>'
            f'<div class="mut">{jc.get("queued", 0)} queued · {jc.get("completed", 0)} completed · {jc.get("failed", 0)} failed</div></div></div>')
    timeline = "".join(f'<tr><td class="mut">{hms(e["created_at"])}</td><td>{esc(e["type"])}</td><td class="mut">{esc(e.get("actor_id"))}</td></tr>'
                       for e in evts)
    versions = "".join(f'<tr><td>v{h["version"]}{" <b>(active)</b>" if h["_id"] == active else ""}</td><td><code>{esc(h["_id"])}</code></td>'
                       f'<td class="mut">{esc(", ".join(h["config"].get("validation", {}).get("inspections") or []) or "status codes only")}</td></tr>'
                       for h in hvs)
    return (f'<h1>{esc(proj.get("name", pid))} <span class="mut">{esc(pid)}</span></h1>{head}'
            f'<h2>Tasks</h2>{"".join(task_card(t, dels, wrs, vby, integs) for t in tasks) or "<div class=mut>no tasks yet</div>"}'
            f'<h2>Harness evolution</h2>{"".join(evolution(e, hv) for e in evos) or "<div class=mut>no evolution runs yet</div>"}'
            f'<div class="cols"><div><h2>Event stream</h2><div class="card"><table>{timeline}</table></div></div>'
            f'<div><h2>Harness versions</h2><div class="card"><table>{versions}</table></div></div></div>')


async def newest_project(db: AsyncDatabase[Doc]) -> str | None:
    p = await db["projects"].find_one({}, {"_id": 1}, sort=NEWEST)
    return p["_id"] if p else None


@app.get("/stream")
async def stream(request: Request, project: str | None = None) -> StreamingResponse:
    db: AsyncDatabase[Doc] = request.app.state.db
    pid = project or await newest_project(db)
    match = {"$match": {"$or": [{"fullDocument.project_id": pid}, {"documentKey._id": pid}]}}

    async def frames() -> AsyncIterator[str]:
        async with await db.watch([match], full_document="updateLookup", max_await_time_ms=300) as cs:
            yield f"data: {json.dumps(await render(db, pid))}\n\n"
            while not await request.is_disconnected():
                if await cs.try_next() is None:
                    yield ": idle\n\n"
                    continue
                for _ in range(50):  # coalesce a burst of writes (one transaction touches several docs) into one render
                    if await cs.try_next() is None:
                        break
                yield f"data: {json.dumps(await render(db, pid))}\n\n"
    return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


CSS = """
:root{color-scheme:light dark;--bg:#fcfcfb;--card:#fff;--line:#e4e4e0;--ink:#1a1a19;--mut:#6b6b66;--code:#2456c9;--good:#0a7d0a;--warn:#9a6700;--bad:#c02f2f;
--goodbg:#e3f5e3;--warnbg:#fbf0d6;--badbg:#fbe4e4}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--card:#232321;--line:#33332f;--ink:#ecece8;--mut:#9a9a93;--code:#8ab4ff;--good:#4cc94c;--warn:#fab219;
--bad:#ff7b72;--goodbg:#173517;--warnbg:#3b2f12;--badbg:#3b1515}}
*{box-sizing:border-box}body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:20px 16px;
max-width:1100px;margin-inline:auto}
h1{font-size:20px;margin:0 0 12px}h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:24px 0 8px}
header{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:8px}
.live{font-size:12px;color:var(--mut)}.live b{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--warn);margin-right:6px}
.live.on b{background:var(--good)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0;overflow-x:auto}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.mut{color:var(--mut)}code{color:var(--code);font-size:12px;overflow-wrap:anywhere}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600}
.good{background:var(--goodbg);color:var(--good)}.warn{background:var(--warnbg);color:var(--warn)}.bad{background:var(--badbg);color:var(--bad)}
.pill.mut{border:1px solid var(--line)}
.tag{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--mut)}.tag.sdk{color:var(--code);border-color:var(--code)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin:8px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}.lbl{font-size:12px;color:var(--mut)}.val{font-size:22px;
font-weight:600}
.hero{font-size:48px;font-weight:600;line-height:1.1}.ok{color:var(--good)}.no{color:var(--bad)}
.stepper{display:flex;gap:4px;flex-wrap:wrap;margin:8px 0}.step{font-size:12px;padding:2px 10px;border-radius:6px;border:1px solid var(--line);
color:var(--mut)}
.step.done{color:var(--good);border-color:var(--good)}.step.now{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.asn{border-top:1px solid var(--line);padding-top:8px;margin-top:8px}.att{margin:6px 0 0 14px;padding-left:10px;border-left:2px solid var(--line)}
.chips{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0}.chk{font-size:12px;font-family:ui-monospace,monospace;padding:0 6px;border-radius:4px}
.chk.ok{background:var(--goodbg)}.chk.no{background:var(--badbg);color:var(--bad);font-weight:600}
.evi{font-family:ui-monospace,monospace;font-size:12px;background:var(--badbg);border-radius:6px;padding:6px 10px;margin-top:4px}
table{border-collapse:collapse;width:100%}td{padding:3px 8px;border-bottom:1px solid var(--line);font-size:13px;font-variant-numeric:tabular-nums}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}ul{margin:6px 0;padding-left:20px}
select{font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:2px 6px;max-width:100%}
"""

JS = """
const main=document.querySelector('main'),live=document.querySelector('.live'),es=new EventSource('/stream'+location.search);
es.onmessage=e=>{main.innerHTML=JSON.parse(e.data);live.className='live on';live.lastChild.textContent='live · change stream'};
es.onerror=()=>{live.className='live';live.lastChild.textContent='reconnecting'};
document.querySelector('select').onchange=e=>location.search='?project='+e.target.value;
"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, project: str | None = None) -> str:
    db: AsyncDatabase[Doc] = request.app.state.db
    pid = project or await newest_project(db)
    projects = await db["projects"].find({}, {"name": 1}).sort(NEWEST).limit(20).to_list()
    opts = "".join(f'<option value="{esc(p["_id"])}"{" selected" if p["_id"] == pid else ""}>{esc(p.get("name", ""))} · {esc(p["_id"])}</option>'
                   for p in projects)
    return (f'<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Airlock</title>'
            f'<style>{CSS}</style></head><body><header><b>Airlock</b><span class="live"><b></b><span>connecting</span></span><select>{opts}</select></header>'
            f'<main>{await render(db, pid)}</main><script>{JS}</script></body></html>')


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
