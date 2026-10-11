"""Read-only monitor for Airlock state (what Person B is doing), from Atlas.

Usage:
  python scripts/monitor.py [project_id] [--watch]

Shows the job queue, worker runs, validation verdicts (with before/after
evidence), integration runs, evolution metrics, and the active harness pointer.
Read-only: it never writes.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from common.db import col


def _counts(name, field="status"):
    out = {}
    for doc in col(name).find({}, {field: 1}):
        out[doc.get(field)] = out.get(doc.get(field), 0) + 1
    return out


def snapshot(project_id: str | None):
    q = {"project_id": project_id} if project_id else {}

    proj = col("projects").find_one(q or {})
    print("\n" + "=" * 72)
    if proj:
        print(f"PROJECT {proj['_id']}  active_harness={proj.get('active_harness_version_id')}  "
              f"gen={proj.get('harness_generation')}  epoch={proj.get('constraint_epoch')}")

    print("\nJOBS        :", _counts("jobs"))
    print("WORKER RUNS :", _counts("worker_runs"))

    print("\nWORKER RUNS")
    for w in col("worker_runs").find(q).sort("_id", 1):
        print(f"  {w['_id']:<16} {w['status']:<12} out={w.get('output_artifact_id')}")

    print("\nVALIDATION RUNS")
    for v in col("validation_runs").find(q).sort("created_at", 1):
        print(f"  {v['_id']:<18} kind={v.get('kind'):<11} decision={v.get('decision')}")
        for c in v.get("checks", []):
            mark = {"pass": "✓", "fail": "✗", "error": "!", "inconclusive": "?"}.get(c["status"], "·")
            line = f"      {mark} {c['id']}"
            if c.get("evidence"):
                for ev in c["evidence"]:
                    line += f"\n          before={ev.get('before')}  after={ev.get('after')}"
            print(line)

    integ = list(col("integration_runs").find(q))
    if integ:
        print("\nINTEGRATION RUNS")
        for i in integ:
            print(f"  {i['_id']:<16} status={i.get('status')} result={i.get('result_artifact_id')} "
                  f"conflicts={len(i.get('conflicts', []))}")

    print("\nEVOLUTION RUNS")
    for e in col("evolution_runs").find(q):
        m = (e.get("evaluation") or {}).get("metrics")
        print(f"  {e['_id']:<16} status={e.get('status')}  candidate={e.get('candidate_harness_version_id')}")
        if m:
            print(f"      baseline escaped={m['baseline']['escaped_failures']}  "
                  f"candidate escaped={m['candidate']['escaped_failures']}  "
                  f"reduction={m['escaped_reduction']}  legit_blocks={m['candidate']['legitimate_blocks']}  "
                  f"set={m['comparison_set_size']}")

    print("\nHARNESS VERSIONS")
    for h in col("harness_versions").find(q).sort("version", 1):
        print(f"  v{h['version']} {h['_id']:<16} runner={h['config'].get('runner')} "
              f"inspections={h['config'].get('validation', {}).get('inspections')}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    watch = "--watch" in sys.argv
    project_id = args[0] if args else None
    if not watch:
        snapshot(project_id)
        return
    while True:
        print("\033[2J\033[H", end="")  # clear screen
        snapshot(project_id)
        time.sleep(2)


if __name__ == "__main__":
    main()
