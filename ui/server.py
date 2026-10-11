"""Read-only web dashboard for Airlock state (Person B's review surface).

A thin FastAPI page that reads Airlock state from Atlas and renders it: the job
queue, worker runs, validation verdicts with before/after database evidence,
the harness-evolution metrics, and the active-harness pointer. It never writes.

Run:
  AIRLOCK_MONGO_URI=... AIRLOCK_DB_NAME=airlock_bulkdemo \
  uvicorn ui.server:app --port 8099
Then open http://127.0.0.1:8099/?project=proj_bulk
"""
from __future__ import annotations

import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from common.db import col

app = FastAPI(title="airlock-ui")

CSS = """
:root{color-scheme:dark}
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin:28px 0 10px;border-bottom:1px solid #21262d;padding-bottom:6px}
.sub{color:#8b949e;margin-bottom:8px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 16px;margin:10px 0}
.row{display:flex;gap:10px;flex-wrap:wrap}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pass{background:#17351f;color:#4ade80} .fail{background:#3b1113;color:#ff7b72} .warn{background:#3b2f12;color:#f1c40f}
.mut{color:#8b949e} code{color:#79c0ff}
table{border-collapse:collapse;width:100%} td,th{text-align:left;padding:4px 10px;border-bottom:1px solid #21262d}
.evi{background:#0b0f14;border:1px solid #30363d;border-radius:8px;padding:8px 12px;margin-top:8px;font-family:ui-monospace,monospace;font-size:13px}
.chg{color:#ff7b72;font-weight:700} .ok{color:#4ade80}
.flow span{padding:4px 8px;border-radius:6px;background:#161b22;border:1px solid #21262d;margin-right:6px}
"""


def _pill(status):
    cls = {"pass": "pass", "completed": "pass", "eligible": "pass", "done": "pass", "finished": "pass",
           "fail": "fail", "repair_required": "fail", "failed": "fail", "rejected": "fail",
           "inconclusive": "warn", "queued": "warn", "running": "warn"}.get(status, "mut")
    return f'<span class="pill {cls}">{html.escape(str(status))}</span>'


def _counts(name):
    out = {}
    for d in col(name).find({}, {"status": 1}):
        out[d.get("status")] = out.get(d.get("status"), 0) + 1
    return out


def render(project_id: str) -> str:
    q = {"project_id": project_id}
    proj = col("projects").find_one({"_id": project_id}) or {}
    hvs = {h["_id"]: h for h in col("harness_versions").find(q)}

    def hv_label(hid):
        h = hvs.get(hid)
        return f"v{h['version']}" if h else (hid or "—")

    parts = [f"<h1>Airlock · {html.escape(project_id)}</h1>"]
    parts.append(f'<div class="sub">active harness <code>{hv_label(proj.get("active_harness_version_id"))}</code> '
                 f'· generation {proj.get("harness_generation","—")} · epoch {proj.get("constraint_epoch","—")} '
                 f'· jobs {_counts("jobs")}</div>')

    # Worker runs
    parts.append("<h2>Worker runs (coding sub-agents)</h2><div class='card'><table>"
                 "<tr><th>worker</th><th>status</th><th>frozen artifact</th></tr>")
    for w in col("worker_runs").find(q).sort("_id", 1):
        parts.append(f"<tr><td>{html.escape(w['_id'])}</td><td>{_pill(w['status'])}</td>"
                     f"<td><code>{html.escape(str(w.get('output_artifact_id')))}</code></td></tr>")
    parts.append("</table></div>")

    # Validation runs
    parts.append("<h2>Validation verdicts</h2>")
    for v in col("validation_runs").find(q).sort("created_at", 1):
        hv = hv_label(v.get("inputs", {}).get("harness_version_id"))
        parts.append(f"<div class='card'><div class='row'><b>{html.escape(v['_id'])}</b> {_pill(v.get('decision'))} "
                     f"<span class='mut'>harness {hv} · {html.escape(v.get('kind',''))}</span></div>")
        parts.append("<table>")
        for c in v.get("checks", []):
            icon = {"pass": "✓", "fail": "✗", "error": "!", "inconclusive": "?"}.get(c["status"], "·")
            cls = "ok" if c["status"] == "pass" else "chg"
            parts.append(f"<tr><td class='{cls}'>{icon}</td><td>{html.escape(c['id'])}</td>"
                         f"<td class='mut'>{html.escape(c.get('detail',''))}</td></tr>")
        parts.append("</table>")
        for c in v.get("checks", []):
            for ev in (c.get("evidence") or []):
                parts.append(
                    "<div class='evi'>⚠ rejected batch changed a row (partial write):<br>"
                    f"item <code>{html.escape(str(ev.get('id')))}</code><br>"
                    f"before = <span class='ok'>{html.escape(str(ev.get('before')))}</span><br>"
                    f"after&nbsp; = <span class='chg'>{html.escape(str(ev.get('after')))}</span></div>")
        parts.append("</div>")

    # Evolution
    parts.append("<h2>Harness evolution</h2>")
    for e in col("evolution_runs").find(q):
        m = (e.get("evaluation") or {}).get("metrics") or {}
        parts.append(f"<div class='card'><div class='row'><b>{html.escape(e['_id'])}</b> {_pill(e.get('status'))} "
                     f"<span class='mut'>{hv_label(e.get('baseline_harness_version_id'))} → "
                     f"{hv_label(e.get('candidate_harness_version_id'))}</span></div>")
        if e.get("hypothesis"):
            parts.append(f"<div class='mut'>{html.escape(e['hypothesis'])}</div>")
        for ch in e.get("changes", []):
            parts.append(f"<div>• {html.escape(ch)}</div>")
        if m:
            b, c = m["baseline"], m["candidate"]
            parts.append(f"<div class='evi'>comparison set = {m['comparison_set_size']} runs &nbsp;·&nbsp; "
                         f"baseline escaped = <span class='chg'>{b['escaped_failures']}</span> &nbsp;→&nbsp; "
                         f"candidate escaped = <span class='ok'>{c['escaped_failures']}</span> &nbsp;·&nbsp; "
                         f"legit blocks = {c['legitimate_blocks']}</div>")
        parts.append("</div>")

    # Harness versions
    parts.append("<h2>Harness versions</h2><div class='card'><table>"
                 "<tr><th>version</th><th>runner</th><th>scenarios</th><th>inspectors</th></tr>")
    for h in col("harness_versions").find(q).sort("version", 1):
        val = h["config"].get("validation", {})
        parts.append(f"<tr><td>v{h['version']}</td><td>{h['config'].get('runner')}</td>"
                     f"<td class='mut'>{html.escape(str(val.get('required_tags')))}</td>"
                     f"<td><code>{html.escape(str(val.get('inspections')))}</code></td></tr>")
    parts.append("</table></div>")

    body = "\n".join(parts)
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta http-equiv=refresh content=2><title>Airlock</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


@app.get("/", response_class=HTMLResponse)
def index(project: str = "proj_bulk"):
    return render(project)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
