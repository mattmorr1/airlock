import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import httpx


def operator_token() -> str:
    return os.environ.get("AIRLOCK_TOKEN_OPERATOR") or (Path.home() / ".airlock" / "operator.token").read_text().strip()


def approve(client: httpx.Client, task_id: str) -> int:
    r = client.get(f"/v1/tasks/{task_id}")
    if r.is_error:
        print(r.text, file=sys.stderr)
        return 1
    task = r.json()["task"]
    print(f"task:    {task_id} ({task['status']})\nrequest: {task['request']}\ncontract:\n{json.dumps(task['contract'], indent=2)}\n"
          f"digest:  {task['contract_digest']}")
    if input("approve this contract? [y/N] ").strip().lower() != "y":
        return 1
    r = client.post(f"/v1/tasks/{task_id}/approve", json={"contract_digest": task["contract_digest"]}, headers={"Idempotency-Key": str(uuid.uuid4())})
    print(r.text, file=sys.stderr if r.is_error else sys.stdout)
    return 1 if r.is_error else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="airlock")
    p.add_argument("--url", default=os.environ.get("AIRLOCK_URL", "http://127.0.0.1:8000"))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("approve", help="review a planned task's contract and approve its digest")
    a.add_argument("task_id")
    args = p.parse_args()
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {operator_token()}"}, timeout=30) as client:
        return approve(client, args.task_id)


if __name__ == "__main__":
    sys.exit(main())
