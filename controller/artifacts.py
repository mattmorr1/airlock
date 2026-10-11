"""Immutable artifact store: content-addressed trees in GridFS (spec 7.5).

An artifact is an immutable snapshot of a file tree, identified by its canonical
tree hash. The archive blob goes to GridFS; small metadata goes to `artifacts`.
GridFS upload is separate from any state transaction: we finalize the blob and
verify its digest *first*, then reference it (spec section 7.5). Writes are
idempotent on (project_id, tree_hash, kind) so at-least-once delivery cannot
create duplicate accepted outputs.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from common.db import col, get_gridfs
from common.enums import ArtifactKind
from common.hashing import FileTree, blob_digest, canonical_tree_hash, patch_hash
from common.ids import new_id, utcnow


def _pack(tree: FileTree) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(tree):
            data = tree[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0  # deterministic archive
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack(data: bytes) -> FileTree:
    tree: FileTree = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                fh = tar.extractfile(member)
                tree[member.name] = fh.read() if fh else b""
    return tree


def put_tree(
    tree: FileTree,
    *,
    kind: ArtifactKind | str,
    project_id: str,
    created_by: str,
    task_id: str | None = None,
    worker_run_id: str | None = None,
    base_artifact_id: str | None = None,
    changed_files: list[str] | None = None,
    patch_text: str | None = None,
    controller_commit_sha: str | None = None,
) -> dict:
    tree_hash = canonical_tree_hash(tree)
    kind = str(kind)

    existing = col("artifacts").find_one({"project_id": project_id, "tree_hash": tree_hash, "kind": kind})
    if existing is not None:
        return existing

    archive = _pack(tree)
    digest = blob_digest(archive)

    # Finalize + verify the blob BEFORE referencing it.
    fs = get_gridfs()
    file_id = fs.upload_from_stream(f"{tree_hash}.tgz", archive)
    readback = fs.open_download_stream(file_id).read()
    if blob_digest(readback) != digest:
        fs.delete(file_id)
        raise RuntimeError("artifact blob digest mismatch after upload")

    doc = {
        "_id": new_id("art"),
        "schema_version": 1,
        "project_id": project_id,
        "task_id": task_id,
        "worker_run_id": worker_run_id,
        "kind": kind,
        "base_artifact_id": base_artifact_id,
        "tree_hash": tree_hash,
        "patch_hash": patch_hash(patch_text) if patch_text is not None else None,
        "controller_commit_sha": controller_commit_sha,
        "changed_files": changed_files or [],
        "storage": {"backend": "gridfs", "file_id": file_id, "digest": digest},
        "size_bytes": len(archive),
        "created_by": created_by,
        "created_at": utcnow(),
    }
    col("artifacts").insert_one(doc)
    return doc


def get_doc(artifact_id: str) -> dict:
    doc = col("artifacts").find_one({"_id": artifact_id})
    if doc is None:
        raise KeyError(f"unknown artifact {artifact_id!r}")
    return doc


def get_tree(artifact_id: str) -> FileTree:
    doc = get_doc(artifact_id)
    file_id = doc["storage"]["file_id"]
    data = get_gridfs().open_download_stream(file_id).read()
    if blob_digest(data) != doc["storage"]["digest"]:
        raise RuntimeError(f"artifact {artifact_id} blob digest mismatch on read")
    return _unpack(data)


def materialize(artifact_id: str, dest: Path) -> Path:
    tree = get_tree(artifact_id)
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in tree.items():
        p = (dest / rel).resolve()
        if not str(p).startswith(str(dest.resolve())):
            raise ValueError(f"path escapes checkout: {rel!r}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return dest
