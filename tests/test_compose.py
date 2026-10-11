from controller.integration import compose


def test_compose_independent_changes_merge_clean():
    base = {"svc.py": b"a\nb\nc\n", "other.py": b"x\n"}
    candB = {"svc.py": b"a\nB\nc\n", "other.py": b"x\n"}          # edits line 2
    candC = {"svc.py": b"a\nb\nc\n", "other.py": b"x\n", "new.py": b"n\n"}  # adds a file
    combined, conflicts = compose(base, [
        {"artifact_id": "B", "ancestor_tree": base, "cand_tree": candB},
        {"artifact_id": "C", "ancestor_tree": base, "cand_tree": candC},
    ])
    assert conflicts == []
    assert combined["svc.py"] == b"a\nB\nc\n"
    assert combined["new.py"] == b"n\n"


def test_compose_same_line_conflicts():
    base = {"svc.py": b"a\nb\nc\n"}
    candB = {"svc.py": b"a\nB1\nc\n"}
    candC = {"svc.py": b"a\nB2\nc\n"}
    _, conflicts = compose(base, [
        {"artifact_id": "B", "ancestor_tree": base, "cand_tree": candB},
        {"artifact_id": "C", "ancestor_tree": base, "cand_tree": candC},
    ])
    assert any(c["file"] == "svc.py" and c["type"] == "modify/modify" for c in conflicts)


def test_compose_delete_vs_modify_conflicts():
    base = {"svc.py": b"a\nb\n"}
    candB = {}  # deletes svc.py
    candC = {"svc.py": b"a\nB\n"}  # modifies it
    _, conflicts = compose(base, [
        {"artifact_id": "C", "ancestor_tree": base, "cand_tree": candC},
        {"artifact_id": "B", "ancestor_tree": base, "cand_tree": candB},
    ])
    assert any(c["type"] == "delete/modify" for c in conflicts)
