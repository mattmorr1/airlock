"""Scenario driver — executed inside the bounded runner, never in the controller.

Drives the running demo application over HTTP, records the final database state,
and captures the application log. It emits *raw* observations as JSON and makes
no judgments: "Raw application output is untrusted evidence, even when a trusted
runner collected it" (spec section 7). The trusted inspectors decide pass/fail.

Usage (inside the runner / local dev subprocess):
    python trusted-evals/driver.py \
        --base-url http://127.0.0.1:8080 --db /run/app.db --log /run/app.log \
        --tags happy_path,error_path --seed 103 --out /run/observations.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import requests

from scenarios import effective_password, select


def _session_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()


def run(base_url: str, db_path: str, log_path: str, tags: set[str], seed: int, canary_salt: str | None) -> dict:
    observations: dict = {
        "seed": seed,
        "selected_tags": sorted(tags),
        "scenarios": [],
        "log_text": "",
        "observer_ok": True,
        "error": None,
    }
    try:
        for sc in select(tags):
            pw = effective_password(sc, canary_salt)
            before = _session_count(db_path)
            resp = requests.post(
                f"{base_url}/login",
                json={"email": sc.email, "password": pw},
                timeout=10,
            )
            after = _session_count(db_path)
            body = None
            try:
                body = resp.json()
            except ValueError:
                body = {"_raw": resp.text[:200]}
            observations["scenarios"].append(
                {
                    "id": sc.id,
                    "tags": list(sc.tags),
                    "status_code": resp.status_code,
                    "expect_status": sc.expect_status,
                    "body": body,
                    "session_delta": after - before,
                    "expect_session_delta": sc.expect_session_delta,
                    "canary": pw,
                }
            )
        with open(log_path, "r", errors="replace") as fh:
            observations["log_text"] = fh.read()
    except Exception as exc:  # observer health: capture failure => inconclusive upstream
        observations["observer_ok"] = False
        observations["error"] = f"{type(exc).__name__}: {exc}"
    return observations


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--tags", default="happy_path")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--canary-salt", default=None)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    tags = {t.strip() for t in args.tags.split(",") if t.strip()}
    obs = run(args.base_url, args.db, args.log, tags, args.seed, args.canary_salt)
    with open(args.out, "w") as fh:
        json.dump(obs, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
