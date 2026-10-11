from runner.inspectors import run_inspectors


def _obs(leak: bool):
    return {
        "scenarios": [
            {"id": "login_success", "status_code": 200, "expect_status": 200,
             "session_delta": 1, "expect_session_delta": 1, "canary": "pw-ok"},
            {"id": "login_exc", "status_code": 401, "expect_status": 401,
             "session_delta": 0, "expect_session_delta": 0, "canary": "SEKRET"},
        ],
        "log_text": "login failed email=x password=SEKRET" if leak else "login failed email=x",
        "observer_ok": True,
    }


def test_sensitive_logs_catches_leak():
    checks = run_inspectors(["sensitive_logs"], _obs(leak=True))
    c = next(c for c in checks if c["id"] == "no_secret_logs")
    assert c["status"] == "fail"
    # Redacted: the raw secret must not appear in the evidence string.
    assert "SEKRET" not in c["detail"]
    assert "match:" in c["detail"]


def test_sensitive_logs_passes_when_clean():
    checks = run_inspectors(["sensitive_logs"], _obs(leak=False))
    assert next(c for c in checks if c["id"] == "no_secret_logs")["status"] == "pass"


def test_database_state_invariants():
    checks = run_inspectors(["database_state"], _obs(leak=False))
    ids = {c["id"]: c["status"] for c in checks}
    assert ids["one_session_per_valid_login"] == "pass"
    assert ids["no_session_for_invalid_login"] == "pass"


def test_unimplemented_inspector_errors_not_passes():
    checks = run_inspectors(["nonexistent"], _obs(leak=False))
    assert checks[0]["status"] == "error"
