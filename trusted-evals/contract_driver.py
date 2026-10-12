"""Contract-check driver: runs the approved contract's own acceptance checks inside the bounded runner.

`checks` are declarative HTTP steps interpreted here (no contract code runs); `tests` are pytest modules from the approved contract,
written outside the candidate tree and run with a timeout. Emits one observation per check; the trusted inspector judges.

Usage:
  python trusted-evals/contract_driver.py --base-url URL --target-uri URI --target-db DB --contract contract.json --out obs.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bulk_driver import ALICE, BOB, _login, _safe

# ponytail: principals are the target app's seeded users; move them into the harness config once a second target app exists.
PRINCIPALS = {"alice": ALICE, "bob": BOB}
TEST_TIMEOUT_S = 60


def _fill(value, binds):
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(binds.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_fill(v, binds) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, binds) for k, v in value.items()}
    return value


def run_check(check, base_url, db, tokens):
    ids = {}
    for name, b in check["bind"].items():
        doc = next(db[b["collection"]].find(b["filter"], {"_id": 1}).sort("_id").skip(b["index"]).limit(1), None)
        if doc is None:
            return {"id": check["id"], "status": "error", "detail": f"bind {name!r} matched no document"}
        ids[name] = doc["_id"]
    snap = lambda: {n: db[check["bind"][n]["collection"]].find_one({"_id": ids[n]}) for n in check["unchanged"]}  # noqa: E731
    before, seen = snap(), {}
    for st in check["steps"]:
        headers = {**_fill(st["headers"], ids), **({"Authorization": f"Bearer {tokens[st['as']]}"} if st["as"] else {})}
        r = requests.request(st["method"], base_url + _fill(st["path"], ids), headers=headers, json=_fill(st["json"], ids), timeout=10)
        seen[st["id"]] = got = (r.status_code, _safe(r))
        if st["expect_status"] is not None and r.status_code != st["expect_status"]:
            return {"id": check["id"], "status": "fail", "detail": f"step {st['id']}: status {r.status_code} (expected {st['expect_status']})"}
        if st["same_response_as"] and got != seen[st["same_response_as"]]:
            return {"id": check["id"], "status": "fail", "detail": f"step {st['id']}: response differs from step {st['same_response_as']}"}
    changed = [n for n, doc in snap().items() if doc != before[n]]
    if changed:
        return {"id": check["id"], "status": "fail", "detail": f"documents changed but must not: {', '.join(changed)}"}
    return {"id": check["id"], "status": "pass", "detail": check["description"]}


def run_test(test, env, workdir):
    path = workdir / f"test_{re.sub(r'\W', '_', test['id'])}.py"
    path.write_text(test["code"])
    try:
        res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)], cwd=workdir, env=env,
                             capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"id": test["id"], "status": "fail", "detail": f"timed out after {TEST_TIMEOUT_S}s"}
    status = {0: "pass", 1: "fail"}.get(res.returncode, "error")
    return {"id": test["id"], "status": status, "detail": test["description"] if status == "pass" else res.stdout[-400:]}


def run(base_url, target_uri, target_db, contract):
    obs = {"contract": [], "observer_ok": True, "error": None}
    try:
        db = MongoClient(target_uri)[target_db]
        tokens = {n: _login(base_url, *cred) for n, cred in PRINCIPALS.items()}
        for check in contract.get("checks", []):
            try:
                obs["contract"].append(run_check(check, base_url, db, tokens))
            except Exception as exc:  # one broken check is an error verdict for it, not for the whole run
                obs["contract"].append({"id": check["id"], "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
        env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, "AIRLOCK_BASE_URL": base_url,
               "AIRLOCK_TOKENS": json.dumps(tokens), "TARGET_MONGO_URI": target_uri, "TARGET_DB": target_db}
        with tempfile.TemporaryDirectory(prefix="contract-tests-") as d:
            obs["contract"] += [run_test(t, env, Path(d)) for t in contract.get("tests", [])]
    except Exception as exc:
        obs["observer_ok"], obs["error"] = False, f"{type(exc).__name__}: {exc}"
    return obs


def main(argv):
    p = argparse.ArgumentParser()
    for flag in ("--base-url", "--target-uri", "--target-db", "--contract", "--out"):
        p.add_argument(flag, required=True)
    a = p.parse_args(argv)
    obs = run(a.base_url, a.target_uri, a.target_db, json.loads(Path(a.contract).read_text()))
    Path(a.out).write_text(json.dumps(obs, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
