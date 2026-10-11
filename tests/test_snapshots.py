from controller.snapshots import _glob_match, diff_trees, scope_violations


def test_diff_trees_classifies_changes():
    base = {"a.py": b"1", "b.py": b"2"}
    cand = {"a.py": b"1x", "c.py": b"3"}  # modify a, delete b, add c
    d = diff_trees(base, cand)
    assert d["added"] == ["c.py"]
    assert d["modified"] == ["a.py"]
    assert d["deleted"] == ["b.py"]
    assert d["changed_files"] == ["a.py", "b.py", "c.py"]


def test_scope_violations():
    changed = ["src/auth/service.py", "src/auth/evil.py"]
    assert scope_violations(changed, ["src/auth/service.py"]) == ["src/auth/evil.py"]
    assert scope_violations(changed, ["src/auth/**"]) == []


def test_glob_match_double_star():
    assert _glob_match("db/migrations/003_sessions.sql", "db/migrations/**")
    assert _glob_match("src/auth/service.py", "src/auth/**")
    assert not _glob_match("src/other/x.py", "src/auth/**")
    assert _glob_match("src/auth/service.py", "src/auth/*.py")
    assert not _glob_match("src/auth/sub/x.py", "src/auth/*.py")
