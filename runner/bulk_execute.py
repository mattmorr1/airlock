"""Bulk-demo runner: validate a candidate app against the trusted suite.

Boots the target app on a FRESH, DISPOSABLE MongoDB database (separate from
Airlock's control plane), drives the bulk scenarios via the in-runner driver,
snapshots documents before/after, applies the selected trusted inspectors, and
returns a verdict. Pure of Airlock's Mongo — callers persist the result.

Fail-closed: if the app won't start or capture fails, the verdict is
`inconclusive`, never `pass`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from pymongo import MongoClient

from runner.execute import _free_port, _wait_healthy  # reuse health/port helpers

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "trusted-evals" / "bulk_driver.py"
DEFAULT_URI = "mongodb://127.0.0.1:27018/?replicaSet=rs0"


@contextmanager
def _app(app_dir: Path, target_uri: str, target_db: str, seed_prefix: str):
    port = _free_port()
    env = {
        **{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ},
        "TARGET_MONGO_URI": target_uri,
        "TARGET_DB": target_db,
        "TARGET_SEED_PREFIX": seed_prefix,
        "TARGET_SECRET": "dev-secret",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(app_dir),
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _decide(checks, observer_ok):
    if not observer_ok:
        return "inconclusive"
    statuses = {c["status"] for c in checks}
    if not checks or "error" in statuses:
        return "inconclusive"
    if "fail" in statuses:
        return "repair_required"
    return "pass"


def run_bulk_suite(app_dir: Path, harness_config: dict, *, seed: int = 0,
                   run_tag: str | None = None, target_uri: str | None = None) -> dict:
    from runner.bulk_inspectors import run_inspectors

    target_uri = target_uri or os.environ.get("TARGET_MONGO_URI", DEFAULT_URI)
    run_tag = run_tag or f"{os.getpid()}_{seed}"
    target_db = f"target_val_{run_tag}"
    seed_prefix = f"s{seed}_"

    validation = harness_config.get("validation", harness_config)
    tags = ",".join(validation.get("required_tags", ["happy_path"]))
    inspections = validation.get("inspections", ["http_responses"])

    run_dir = Path(mkdtemp(prefix="bulkval-"))
    obs_path = run_dir / "obs.json"
    started = time.monotonic()
    observer_ok, obs = True, {}

    client = MongoClient(target_uri)
    client.drop_database(target_db)  # start clean
    try:
        with _app(app_dir, target_uri, target_db, seed_prefix) as base_url:
            cmd = [sys.executable, str(DRIVER), "--base-url", base_url,
                   "--target-uri", target_uri, "--target-db", target_db,
                   "--tags", tags, "--seed", str(seed), "--out", str(obs_path)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"driver failed: {res.stderr[:400]}")
        obs = json.loads(obs_path.read_text())
        observer_ok = obs.get("observer_ok", False)
    except Exception as exc:
        observer_ok, obs = False, {"scenarios": [], "observer_ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        client.drop_database(target_db)  # disposable teardown

    checks = run_inspectors(inspections, obs) if observer_ok else []
    decision = _decide(checks, observer_ok)
    partial_write = any(c["id"] == "rows_unchanged_on_rejection" and c["status"] == "fail" for c in checks)

    return {
        "checks": checks,
        "decision": decision,
        "behavior_delta": {"partial_write": partial_write},
        "coverage": {"scenario_ids": [s["id"] for s in obs.get("scenarios", [])]},
        "observer_ok": observer_ok,
        "observer_error": obs.get("error"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "inspections": inspections,
        "required_tags": validation.get("required_tags", ["happy_path"]),
        "seed": seed,
    }
