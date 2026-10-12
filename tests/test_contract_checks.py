"""Contract checks end to end: the approved contract's declarative steps and pytest modules run in the bulk runner against a live app."""
import shutil
import socket
from pathlib import Path

import pytest

from controller.planner import validate
from runner.bulk_execute import DEFAULT_URI, run_bulk_suite

ROOT = Path(__file__).resolve().parents[1]
HARNESS = {"runner": "bulk", "validation": {"inspections": ["http_responses"], "required_tags": ["happy_path"]}}
PLAN = {"constraints": [], "acceptance": ["a mixed-owner batch changes nothing", "a retry replays", "bulk needs auth"], "interfaces": {},
        "assignments": [{"key": "bulk", "objective": "bulk", "depends_on": [], "file_scope": ["app/**"], "profile_key": "backend"}],
        "checks": [
            {"id": "mixed_owner_atomic", "description": "a mixed-owner batch is rejected and leaves Alice's item unchanged",
             "bind": {"mine": {"collection": "items", "filter": {"owner_id": "user_alice"}},
                      "theirs": {"collection": "items", "filter": {"owner_id": "user_bob"}}},
             "steps": [{"id": "mixed", "as": "alice", "method": "PATCH", "path": "/api/v1/items/bulk", "headers": {"Idempotency-Key": "cc-mixed"},
                        "json": {"updates": [{"id": "{mine}", "title": "PWNED"}, {"id": "{theirs}", "title": "PWNED"}]}, "expect_status": 403}],
             "unchanged": ["mine"]},
            {"id": "retry_replays", "description": "an identical retry returns the original result",
             "bind": {"mine": {"collection": "items", "filter": {"owner_id": "user_alice"}, "index": 1}},
             "steps": [{"id": "first", "as": "alice", "method": "PATCH", "path": "/api/v1/items/bulk", "headers": {"Idempotency-Key": "cc-retry"},
                        "json": {"updates": [{"id": "{mine}", "title": "again"}]}, "expect_status": 200},
                       {"id": "retry", "as": "alice", "method": "PATCH", "path": "/api/v1/items/bulk", "headers": {"Idempotency-Key": "cc-retry"},
                        "json": {"updates": [{"id": "{mine}", "title": "again"}]}, "same_response_as": "first"}]}],
        "tests": [{"id": "bulk_requires_auth", "description": "anonymous bulk updates are refused",
                   "code": "import os, requests\n\ndef test_anonymous():\n"
                           "    r = requests.patch(os.environ['AIRLOCK_BASE_URL'] + '/api/v1/items/bulk', json={'updates': []}, timeout=10)\n"
                           "    assert r.status_code == 401\n"}]}


def _mongo_up() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", 27018)) == 0


pytestmark = pytest.mark.skipif(not _mongo_up(), reason="target mongod on :27018 is not running")


def app(tmp_path: Path, variant: str) -> Path:
    shutil.copytree(ROOT / "target-app", dst := tmp_path / variant)
    shutil.copytree(ROOT / "demo" / "bulk_feature" / variant / "app", dst / "app", dirs_exist_ok=True)
    return dst


@pytest.mark.parametrize(("variant", "expected"), [("fixed", "pass"), ("faulty", "fail")])
def test_contract_checks_judge_the_live_app(tmp_path: Path, variant: str, expected: str) -> None:
    contract = validate("bulk", PLAN)
    assert isinstance(contract, dict), contract
    res = run_bulk_suite(app(tmp_path, variant), HARNESS, seed=7, run_tag=f"cc_{variant}", target_uri=DEFAULT_URI, contract=contract)
    by_id = {c["id"]: c for c in res["checks"]}
    assert res["observer_ok"], res
    assert by_id["contract_mixed_owner_atomic"]["status"] == expected, by_id
    assert by_id["contract_bulk_requires_auth"]["status"] == "pass", by_id
    assert res["coverage"]["contract_check_ids"] == ["mixed_owner_atomic", "retry_replays", "bulk_requires_auth"]


def test_contract_rejects_dangling_references() -> None:
    bad = PLAN | {"checks": [PLAN["checks"][0] | {"unchanged": ["nope"]}], "tests": [PLAN["tests"][0], PLAN["tests"][0]]}
    errors = validate("bulk", bad)
    assert errors == ["duplicate check id 'bulk_requires_auth'", "check mixed_owner_atomic: unchanged 'nope' is not a bind"]
