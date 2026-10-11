"""Baseline/candidate execution and observation (spec section 9).

Runs a frozen application tree under a harness configuration, in a disposable
environment, and returns a verdict built from trusted inspectors. The same
entry point is used by application-loop validation and by paired harness
evaluation, so it is pure of MongoDB (callers persist the result).

Two backends:
  - LocalAppRunner: dev-only subprocess on the host. NOT a security boundary.
    Use only for trusted code / local development.
  - ContainerAppRunner: the real isolated path. Candidates run as non-root with
    resource/time limits, restricted mounts, no host control socket, and a
    network restricted to internal test services (spec section 9). Requires the
    prebuilt trusted runner image; never builds a candidate Dockerfile.

Observer health is explicit: if the app fails to start or capture fails, the
verdict is inconclusive, never pass.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

import requests

from common.config import get_settings
from common.enums import CheckStatus, ValidationDecision
from common.hashing import FileTree
from runner.inspectors import run_inspectors

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER_DIR = REPO_ROOT / "trusted-evals"


def materialize_tree(tree: FileTree, dest: Path) -> Path:
    for rel, content in tree.items():
        # Reject path traversal / absolute paths (spec section 4).
        p = (dest / rel).resolve()
        if not str(p).startswith(str(dest.resolve())):
            raise ValueError(f"path escapes checkout: {rel!r}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return dest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def LocalAppRunner(app_dir: Path, db_path: Path, log_path: Path):
    """DEV ONLY. Launch the demo app with uvicorn as a host subprocess."""
    port = _free_port()
    env = {
        "APP_DB": str(db_path),
        "APP_LOG": str(log_path),
        "PATH": __import__("os").environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(app_dir),
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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


def _wait_healthy(base_url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app exited early with code {proc.returncode}")
        try:
            if requests.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            time.sleep(0.25)
    raise TimeoutError("app did not become healthy")


def _decide(checks: list[dict], observer_ok: bool) -> ValidationDecision:
    if not observer_ok:
        return ValidationDecision.INCONCLUSIVE
    statuses = {c["status"] for c in checks}
    if not checks or CheckStatus.ERROR.value in statuses:
        return ValidationDecision.INCONCLUSIVE
    if CheckStatus.FAIL.value in statuses:
        return ValidationDecision.REPAIR_REQUIRED
    return ValidationDecision.PASS


def run_suite(
    app_dir: Path,
    harness_config: dict,
    *,
    seed: int = 0,
    canary_salt: str | None = None,
    run_dir: Path | None = None,
) -> dict:
    """Run the trusted suite against ``app_dir`` under ``harness_config``.

    Returns a dict shaped for ValidationRunDoc: checks, decision, behavior_delta,
    coverage, observer_ok, elapsed_ms.
    """
    settings = get_settings()
    validation = harness_config.get("validation", harness_config)
    tags = ",".join(validation.get("required_tags", ["happy_path"]))
    inspections = validation.get("inspections", ["http_responses", "database_state"])

    run_dir = run_dir or Path(mkdtemp(prefix="airlock-run-", dir=str(settings.ensure_work_root())))
    db_path = run_dir / "app.db"
    log_path = run_dir / "app.log"
    obs_path = run_dir / "observations.json"
    log_path.write_text("")  # ensure present for capture

    started = time.monotonic()
    observer_ok = True
    obs: dict = {}

    if settings.execution_mode == "container":
        raise NotImplementedError(
            "container backend requires the prebuilt trusted runner image; "
            "set AIRLOCK_EXECUTION_MODE=local for dev"
        )

    try:
        with LocalAppRunner(app_dir, db_path, log_path) as base_url:
            cmd = [
                sys.executable, str(DRIVER_DIR / "driver.py"),
                "--base-url", base_url,
                "--db", str(db_path),
                "--log", str(log_path),
                "--tags", tags,
                "--seed", str(seed),
                "--out", str(obs_path),
            ]
            if canary_salt:
                cmd += ["--canary-salt", canary_salt]
            res = subprocess.run(cmd, cwd=str(DRIVER_DIR), capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"driver failed: {res.stderr[:500]}")
        import json
        obs = json.loads(obs_path.read_text())
        observer_ok = obs.get("observer_ok", False)
    except Exception as exc:
        observer_ok = False
        obs = {"scenarios": [], "log_text": "", "observer_ok": False, "error": f"{type(exc).__name__}: {exc}"}

    checks = run_inspectors(inspections, obs) if observer_ok else []
    decision = _decide(checks, observer_ok)

    leaked = [c for c in checks if c["id"] == "no_secret_logs" and c["status"] == CheckStatus.FAIL.value]
    behavior_delta = {"leaked_credentials": bool(leaked)}

    return {
        "checks": checks,
        "decision": decision.value,
        "behavior_delta": behavior_delta,
        "coverage": {"scenario_ids": [s["id"] for s in obs.get("scenarios", [])]},
        "observer_ok": observer_ok,
        "observer_error": obs.get("error"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "inspections": inspections,
        "required_tags": validation.get("required_tags", ["happy_path"]),
        "seed": seed,
    }
