"""Canonical content hashing for immutable artifacts (spec section 4, 7.5).

Artifact identity is a canonical tree hash over file *contents*, plus a patch
hash. Controller-side Git commit SHAs additionally identify revisions but are
not the primary identity. Hashing is pure over an in-memory file map so it is
fully unit-testable without a checkout.
"""
from __future__ import annotations

import hashlib

# A file tree is a mapping of POSIX relative path -> raw bytes.
FileTree = dict[str, bytes]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_tree_hash(tree: FileTree) -> str:
    """Order-independent hash of a file tree.

    The digest covers each path and its exact bytes, framed by length prefixes
    so that ``{"a": b"", "b": b"x"}`` cannot collide with ``{"ab": b"x"}`` or
    with any reordering.
    """
    h = hashlib.sha256()
    for path in sorted(tree):
        content = tree[path]
        path_bytes = path.encode("utf-8")
        h.update(len(path_bytes).to_bytes(8, "big"))
        h.update(path_bytes)
        h.update(len(content).to_bytes(8, "big"))
        h.update(content)
    return "sha256:" + h.hexdigest()


def patch_hash(patch_text: str) -> str:
    return "sha256:" + sha256_hex(patch_text.encode("utf-8"))


def blob_digest(data: bytes) -> str:
    """Digest for a GridFS blob; verified after upload, before referencing."""
    return "sha256:" + sha256_hex(data)
