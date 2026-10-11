"""Runner integration test — no MongoDB required (run_suite is DB-free)."""
from pathlib import Path

from runner.execute import run_suite

APP = Path(__file__).resolve().parents[1] / "demo-app"

V1 = {"validation": {"required_tags": ["happy_path"],
                     "inspections": ["http_responses", "database_state"]}}
V2 = {"validation": {"required_tags": ["happy_path", "error_path"],
                     "inspections": ["http_responses", "database_state", "sensitive_logs"]}}


def test_v1_passes_leak_escapes():
    r = run_suite(APP, V1, seed=1, canary_salt="t1")
    assert r["observer_ok"]
    assert r["decision"] == "pass"
    assert r["behavior_delta"]["leaked_credentials"] is False


def test_v2_catches_leak():
    r = run_suite(APP, V2, seed=1, canary_salt="t2")
    assert r["decision"] == "repair_required"
    assert r["behavior_delta"]["leaked_credentials"] is True
    assert "login_unknown_user_exception" in r["coverage"]["scenario_ids"]


def test_broken_app_is_inconclusive(tmp_path):
    # Copy the app and corrupt the entrypoint so it cannot start.
    import shutil
    dst = tmp_path / "brokenapp"
    shutil.copytree(APP, dst)
    (dst / "app.py").write_text("this is not valid python :(")
    r = run_suite(dst, V2, seed=1)
    assert r["decision"] == "inconclusive"
    assert r["observer_ok"] is False
