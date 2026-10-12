"""Freeze, manifest, digest (spec section 4).

Turns a quiesced worker checkout into an immutable artifact. The worker and its
children must already be stopped before reading output — a final text response
alone does not freeze the filesystem (that sequencing is workers.py's job). Here
we capture the file manifest, reject path traversal, diff against the input
revision, compare changed files to the assignment's allowed scope, and store a
canonical tree hash + patch hash.

The pure helpers (read_dir_tree, diff_trees, scope_violations) take no database
and are unit-testable without infrastructure.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from common.enums import ArtifactKind
from common.hashing import FileTree
from controller import artifacts

# Runtime/noise paths never captured into an artifact.
IGNORE_DIRS = {".git", ".claude", "__pycache__", ".venv", ".pytest_cache", "node_modules"}
IGNORE_SUFFIXES = {".pyc", ".pyo"}
IGNORE_NAMES = {"app.db", "app.log", "observations.json", "CLAUDE.md"}


def read_dir_tree(root: Path) -> FileTree:
    """Capture tracked + new files under ``root`` as a {relpath: bytes} map."""
    root = root.resolve()
    tree: FileTree = {}
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if any(part in IGNORE_DIRS for part in p.relative_to(root).parts):
            continue
        if p.suffix in IGNORE_SUFFIXES or p.name in IGNORE_NAMES:
            continue
        rel = p.relative_to(root).as_posix()
        # Reject anything resolving outside the checkout (symlink escapes, etc.).
        if not str(p.resolve()).startswith(str(root)):
            raise ValueError(f"path escapes checkout: {rel!r}")
        tree[rel] = p.read_bytes()
    return tree


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8000]


def diff_trees(base: FileTree, cand: FileTree) -> dict:
    """Changed-file manifest and a unified patch between two trees."""
    added = sorted(set(cand) - set(base))
    deleted = sorted(set(base) - set(cand))
    modified = sorted(p for p in (set(base) & set(cand)) if base[p] != cand[p])
    changed_files = sorted(added + modified + deleted)

    patch_parts: list[str] = []
    for p in added + modified:
        old = base.get(p, b"")
        new = cand[p]
        if _is_text(old) and _is_text(new):
            patch_parts.extend(
                difflib.unified_diff(
                    old.decode("utf-8", "replace").splitlines(keepends=True),
                    new.decode("utf-8", "replace").splitlines(keepends=True),
                    fromfile=f"a/{p}", tofile=f"b/{p}",
                )
            )
        else:
            patch_parts.append(f"Binary file {p} changed\n")
    for p in deleted:
        patch_parts.append(f"deleted file {p}\n")

    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "changed_files": changed_files,
        "patch_text": "".join(patch_parts),
    }


def _glob_match(path: str, pattern: str) -> bool:
    # Translate a glob (with ** across slashes) into a regex.
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(rx, path) is not None


def scope_violations(changed_files: list[str], allowed_scope: list[str]) -> list[str]:
    """Files changed outside the assignment's declared scope."""
    return [f for f in changed_files if not any(_glob_match(f, pat) for pat in allowed_scope)]


def freeze_worker_output(
    *,
    checkout_dir: Path,
    base_tree: FileTree,
    project_id: str,
    task_id: str,
    worker_run_id: str,
    base_artifact_id: str,
    allowed_scope: list[str],
    created_by: str,
) -> dict:
    """Freeze a worker checkout into an immutable artifact and report scope."""
    cand_tree = read_dir_tree(checkout_dir)
    diff = diff_trees(base_tree, cand_tree)
    violations = scope_violations(diff["changed_files"], allowed_scope)

    artifact = artifacts.put_tree(
        cand_tree,
        kind=ArtifactKind.CODE_SNAPSHOT,
        project_id=project_id,
        task_id=task_id,
        worker_run_id=worker_run_id,
        base_artifact_id=base_artifact_id,
        changed_files=diff["changed_files"],
        patch_text=diff["patch_text"],
        created_by=created_by,
    )
    return {
        "artifact": artifact,
        "changed_files": diff["changed_files"],
        "scope_violations": violations,
        "patch_text": diff["patch_text"],
    }
