import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import httpx


def operator_token() -> str:
    return os.environ.get("AIRLOCK_TOKEN_OPERATOR") or (Path.home() / ".airlock" / "operator.token").read_text().strip()


def approve(client: httpx.Client, task_id: str, yes: bool) -> int:
    r = client.get(f"/v1/tasks/{task_id}")
    if r.is_error:
        print(r.text, file=sys.stderr)
        return 1
    task = r.json()["task"]
    print(f"task:    {task_id} ({task['status']})\nrequest: {task['request']}\ncontract:\n{json.dumps(task['contract'], indent=2)}\n"
          f"digest:  {task['contract_digest']}")
    return confirm_post(client, "approve this contract?", f"/v1/tasks/{task_id}/approve", {"contract_digest": task["contract_digest"]}, yes)


def confirm_post(client: httpx.Client, prompt: str, path: str, body: dict[str, str] | None = None, yes: bool = False) -> int:
    if not yes and input(f"{prompt} [y/N] ").strip().lower() != "y":
        return 1
    r = client.post(path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    print(r.text, file=sys.stderr if r.is_error else sys.stdout)
    return 1 if r.is_error else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="airlock")
    p.add_argument("--url", default=os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000"))
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt (for `! airlock -y ...` in Claude Code, which has no stdin)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("approve", help="review a planned task's contract and approve its digest")
    a.add_argument("task_id")
    rb = sub.add_parser("rollback", help="point a project back at a previously validated harness version")
    rb.add_argument("project_id")
    rb.add_argument("harness_version_id")
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
            case _:
                return approve(client, args.task_id, args.yes)


if __name__ == "__main__":
    sys.exit(main())
