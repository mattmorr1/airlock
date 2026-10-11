from common.hashing import canonical_tree_hash, patch_hash


def test_tree_hash_order_independent():
    a = canonical_tree_hash({"a": b"1", "b": b"2"})
    b = canonical_tree_hash({"b": b"2", "a": b"1"})
    assert a == b


def test_tree_hash_no_boundary_collision():
    # {"a":"", "b":"x"} must not collide with {"ab":"x"} (length framing).
    assert canonical_tree_hash({"a": b"", "b": b"x"}) != canonical_tree_hash({"ab": b"x"})


def test_tree_hash_sensitive_to_content():
    assert canonical_tree_hash({"a": b"x"}) != canonical_tree_hash({"a": b"y"})


def test_patch_hash_stable():
    assert patch_hash("diff") == patch_hash("diff")
    assert patch_hash("a") != patch_hash("b")
