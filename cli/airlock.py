import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx


def operator_token() -> str:
    if tok := os.environ.get("AIRLOCK_TOKEN_OPERATOR"):
        return tok
    try:
        return (Path.home() / ".airlock" / "operator.token").read_text().strip()
    except FileNotFoundError:
        sys.exit("no operator token: set AIRLOCK_TOKEN_OPERATOR or write ~/.airlock/operator.token (see scripts/up.sh).")


def request(client: httpx.Client, method: str, path: str, **kw: Any) -> httpx.Response:
    try:
        return client.request(method, path, **kw)
    except httpx.RequestError as e:  # API not running / unreachable: a readable line beats a traceback.
        sys.exit(f"Airlock API not reachable at {client.base_url} ({e.__class__.__name__}). Start the stack with scripts/up.sh, then retry.")


def get(client: httpx.Client, path: str) -> Any:
    r = request(client, "GET", path)
    if r.is_error:
        sys.exit(f"{path} -> {r.status_code}: {r.text}")
    return r.json()


def step_line(st: dict[str, Any]) -> str:
    body = f" {json.dumps(st['json'])}" if st["json"] is not None else ""
    want = f" -> {st['expect_status']}" if st["expect_status"] is not None else ""
    same = f" == {st['same_response_as']}" if st["same_response_as"] else ""
    return f"  {st['id']}: {st['as'] or 'anon'} {st['method']} {st['path']}{body}{want}{same}"


def render(task: dict[str, Any], delegations: list[dict[str, Any]]) -> str:
    """A contract and its progress as plain text an operator can review before approving."""
    c, by_key = task.get("contract") or {}, {d["assignment_key"]: d for d in delegations}
    lines = [f"task     {task['_id']}  [{task['status']}]", f"request  {task['request']}", f"base     {task['base_artifact_id']}"]
    lines += [f"error    {task['last_error']}"] if task.get("last_error") else []
    lines += [f"final    {task['final_artifact_id']}"] if task.get("final_artifact_id") else []
    if not c:
        return "\n".join([*lines, "", "(no contract yet: still planning)"])
    for title, items in (("constraints", c.get("constraints", [])), ("acceptance", c.get("acceptance", []))):
        lines += ["", f"{title}:", *(f"  - {i}" for i in items)]
    lines += ["", "interfaces:", *(f"  {k}: {v}" for k, v in c.get("interfaces", {}).items())]
    for ch in c.get("checks", []):
        binds = (f"  {b} = {v['collection']} {json.dumps(v['filter'])} [{v['index']}]" for b, v in ch["bind"].items())
        lines += ["", f"check {ch['id']}: {ch['description']}", *binds]
        lines += [step_line(st) for st in ch["steps"]]
        lines += [f"  unchanged: {', '.join(ch['unchanged'])}"] if ch["unchanged"] else []
    for t in c.get("tests", []):
        lines += ["", f"test {t['id']}: {t['description']}", *(f"  | {ln}" for ln in t["code"].splitlines())]
    lines += ["", "assignments:"]
    for a in c["assignments"]:
        d = by_key.get(a["key"])
        state = f"{d['status']} (attempt {d['attempt']})" if d else "not delegated"
        lines += [f"  {a['key']}  [{state}]  after: {', '.join(a['depends_on']) or '-'}  scope: {', '.join(a['file_scope'])}  profile: {a['profile_key']}",
                  f"      {a['objective']}"]
    return "\n".join([*lines, "", f"digest   {task.get('contract_digest')}"])


def show(client: httpx.Client, task_id: str) -> int:
    view = get(client, f"/v1/tasks/{task_id}")
    print(render(view["task"], view["delegations"]))
    if view["task"]["status"] == "planned":
        print(f"\napprove with: ! airlock -y approve {task_id}")
    return 0


def ls(client: httpx.Client, project_id: str | None) -> int:
    for p in [{"_id": project_id}] if project_id else get(client, "/v1/projects"):
        print(f"{p['_id']}  {p.get('name', '')}  baseline {p.get('baseline_artifact_id', '')}")
        for t in get(client, f"/v1/projects/{p['_id']}/tasks")[:5 if not project_id else 20]:
            print(f"  {t['_id']}  [{t['status']}]  {t['request'][:70]}" + (f"  -> {t['final_artifact_id']}" if t.get("final_artifact_id") else ""))
    return 0


def approve(client: httpx.Client, task_id: str, yes: bool) -> int:
    view = get(client, f"/v1/tasks/{task_id}")
    task = view["task"]
    print(render(task, view["delegations"]))
    if task["status"] != "planned":
        print(f"\nnothing to approve: task is {task['status']}", file=sys.stderr)
        return 0 if task["status"] in ("approved", "running", "completed") else 1
    return confirm_post(client, "approve this contract?", f"/v1/tasks/{task_id}/approve", {"contract_digest": task["contract_digest"]}, yes)


def confirm_post(client: httpx.Client, prompt: str, path: str, body: dict[str, str] | None = None, yes: bool = False) -> int:
    if not yes and input(f"{prompt} [y/N] ").strip().lower() != "y":
        return 1
    r = request(client, "POST", path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    print(r.text, file=sys.stderr if r.is_error else sys.stdout)
    return 1 if r.is_error else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="airlock")
    p.add_argument("--url", default=os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000"))
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt (for `! airlock -y ...` in Claude Code, which has no stdin)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("approve", help="print a planned task's contract and approve its digest")
    a.add_argument("task_id")
    sh = sub.add_parser("show", help="review a task's contract and progress without changing anything")
    sh.add_argument("task_id")
    li = sub.add_parser("ls", help="list projects and their recent tasks")
    li.add_argument("project_id", nargs="?")
    rb = sub.add_parser("rollback", help="point a project back at a previously validated harness version")
    rb.add_argument("project_id")
    rb.add_argument("harness_version_id")
    cn = sub.add_parser("cancel", help="cancel an open task and its unfinished delegations")
    cn.add_argument("task_id")
    rv = sub.add_parser("revoke", help="bump a project's constraint epoch, invalidating approvals and in-flight validations")
    rv.add_argument("project_id")
    args = p.parse_args()
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {operator_token()}"}, timeout=30) as client:
        match args.cmd:
            case "rollback":
                return confirm_post(client, f"roll {args.project_id} back to {args.harness_version_id}?", f"/v1/projects/{args.project_id}/rollback",
                                    {"harness_version_id": args.harness_version_id}, args.yes)
            case "revoke":
                return confirm_post(client, f"revoke {args.project_id}? approvals and in-flight runs go stale.", f"/v1/projects/{args.project_id}/revoke",
                                    None, args.yes)
            case "show":
                return show(client, args.task_id)
            case "ls":
                return ls(client, args.project_id)
            case "cancel":
                return confirm_post(client, f"cancel {args.task_id}?", f"/v1/tasks/{args.task_id}/cancel", None, args.yes)
            case _:
                return approve(client, args.task_id, args.yes)


if __name__ == "__main__":
    sys.exit(main())
