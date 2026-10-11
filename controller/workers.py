"""Managed coding-worker launch (spec section 1, 4, 5).

A managed worker is a separate SDK-hosted Claude Code session in a bounded
container — not a native `Agent` instance in the parent session. The controller:

  1. resolves the input artifact and materializes an INDEPENDENT checkout
     (an archive, with no writable .git authority exposed to the worker),
  2. runs the worker under explicit model/tool/turn/budget limits with
     controller-owned PreToolUse policy,
  3. stops the worker AND its children before reading output — a final text
     response alone does not freeze the filesystem,
  4. freezes the checkout into an immutable artifact.

Only the supervisor asserts worker identity; model-supplied task IDs and worker
reports are never authorization credentials (spec section 4).

Backends:
  - ClaudeSDKWorker: the real managed path (requires claude-agent-sdk, the
    bounded worker image, and a short-lived inference-gateway credential).
  - ScriptedWorker: DEV/TEST ONLY. Applies an injected edit function to the
    checkout to stand in for a coding session. It is NOT Claude and NOT a
    security boundary; it exists so the full loop runs offline.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import mkdtemp
from typing import Protocol

from common.config import get_settings
from common.db import col
from common.enums import WorkerState
from common.hashing import FileTree
from common.ids import utcnow
from controller import artifacts, snapshots


class WorkerBackend(Protocol):
    def run(self, checkout_dir: Path, payload: dict) -> dict:
        """Execute the coding session, mutating files under checkout_dir.

        Returns a metrics dict (only values actually produced — never invented).
        Must return only after the session has ended.
        """

    def stop(self) -> None:
        """Terminate the worker and any child processes."""


class ScriptedWorker:
    """DEV/TEST ONLY. Stand-in that applies an injected edit to the checkout."""

    def __init__(self, edit_fn: Callable[[Path, dict], dict] | None = None):
        self._edit_fn = edit_fn or (lambda checkout, payload: {})

    def run(self, checkout_dir: Path, payload: dict) -> dict:
        metrics = self._edit_fn(checkout_dir, payload) or {}
        metrics.setdefault("mode", "scripted")
        return metrics

    def stop(self) -> None:  # synchronous backend: nothing to terminate
        return None


def _relative(path: str, root: Path) -> str:
    """Checkout-relative, normalized path; anything escaping the checkout keeps a leading '..' and so matches no scope."""
    return os.path.normpath(os.path.relpath(path, root) if os.path.isabs(path) else path)


class ClaudeSDKWorker:
    """Real managed worker: a bounded Claude Agent SDK session in a container."""

    def __init__(self):
        self._session = None

    def run(self, checkout_dir: Path, payload: dict) -> dict:
        """Runs off the event loop (asyncio.to_thread), so it owns a fresh loop for the SDK's async stream."""
        return asyncio.run(self._run(checkout_dir, payload))

    async def _run(self, checkout_dir: Path, payload: dict) -> dict:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

        from controller.planner import scope_matches

        scope = payload.get("file_scope", [])

        async def scope_gate(inp, _tool_use_id, _ctx) -> dict:
            # Feedback, not the boundary: Bash can still write anywhere, which freeze-time scope checks and the container bound.
            rel = _relative(inp["tool_input"].get("file_path") or inp["tool_input"].get("notebook_path") or "", checkout_dir)
            if scope_matches(scope, rel):
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"{rel} is outside this assignment's file scope {scope}"}}

        options = ClaudeAgentOptions(
            cwd=str(checkout_dir),
            model=payload.get("model_requested", get_settings().worker_default_model),
            max_turns=payload.get("max_turns", 20),
            allowed_tools=["Read", "Edit", "Write", "Bash"],
            hooks={"PreToolUse": [HookMatcher(matcher="Edit|Write|NotebookEdit", hooks=[scope_gate])]},
        )
        result = None
        async for message in query(prompt=self._build_prompt(payload), options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None or result.is_error:
            raise RuntimeError(f"SDK worker ended without success: {result.subtype if result else 'no result message'}")
        return {"mode": "claude-sdk", "claude_session_id": result.session_id, "num_turns": result.num_turns, "total_cost_usd": result.total_cost_usd}

    def _build_prompt(self, payload: dict) -> str:
        scope = ", ".join(payload.get("file_scope", []))
        return (
            f"{payload.get('objective', '')}\n\n"
            f"Work only within: {scope}\n"
            f"Honor the interface contract exactly."
        )

    def stop(self) -> None:  # pragma: no cover
        self._session = None


def _materialize_checkout(base_artifact_id: str, worker_run_id: str) -> tuple[Path, FileTree]:
    settings = get_settings()
    root = Path(mkdtemp(prefix=f"ckout-{worker_run_id}-", dir=str(settings.ensure_work_root())))
    base_tree = artifacts.get_tree(base_artifact_id)
    artifacts.materialize(base_artifact_id, root)
    return root, base_tree


def launch_worker(payload: dict, backend: WorkerBackend) -> dict:
    """Run one managed worker and freeze its output. Idempotent-safe to re-run:
    the frozen artifact is content-addressed, so a duplicate produces the same id.
    """
    wr = col("worker_runs")
    worker_run_id = payload["worker_run_id"]

    wr.update_one(
        {"_id": worker_run_id},
        {
            "$setOnInsert": {
                "_id": worker_run_id,
                "schema_version": 1,
                "project_id": payload["project_id"],
                "task_id": payload["task_id"],
                "delegation_id": payload["delegation_id"],
                "attempt": payload.get("attempt", 1),
                "revision": 1,
                "execution": {"mode": get_settings().execution_mode, "host_id": "host_01"},
                "input_artifact_id": payload["base_artifact_id"],
                "harness_version_id": payload["harness_version_id"],
                "constraint_epoch": payload["constraint_epoch"],
                "model_requested": payload.get("model_requested", "sonnet"),
                "context_manifest": payload.get("context_manifest", []),
                "budget": {
                    "max_turns": payload.get("max_turns", 20),
                    "max_runtime_seconds": payload.get("max_runtime_seconds", 600),
                },
                "output_artifact_id": None,
                "created_at": utcnow(),
            },
            "$set": {"status": WorkerState.STARTING.value, "started_at": utcnow()},
        },
        upsert=True,
    )

    checkout_dir, base_tree = _materialize_checkout(payload["base_artifact_id"], worker_run_id)
    wr.update_one({"_id": worker_run_id}, {"$set": {"status": WorkerState.RUNNING.value}})

    try:
        metrics = backend.run(checkout_dir, payload)
    finally:
        # Stop the worker + children BEFORE reading the filesystem.
        wr.update_one({"_id": worker_run_id}, {"$set": {"status": WorkerState.STOPPING.value}})
        backend.stop()

    wr.update_one({"_id": worker_run_id}, {"$set": {"status": WorkerState.SNAPSHOTTING.value}})
    frozen = snapshots.freeze_worker_output(
        checkout_dir=checkout_dir,
        base_tree=base_tree,
        project_id=payload["project_id"],
        task_id=payload["task_id"],
        worker_run_id=worker_run_id,
        base_artifact_id=payload["base_artifact_id"],
        allowed_scope=payload.get("file_scope", []),
        created_by="supervisor_01",
    )

    wr.update_one(
        {"_id": worker_run_id},
        {
            "$set": {
                # finished == output frozen, NOT accepted.
                "status": WorkerState.FINISHED.value,
                "output_artifact_id": frozen["artifact"]["_id"],
                "claude_session_id": metrics.get("claude_session_id"),
                "metrics": metrics,
                "finished_at": utcnow(),
            }
        },
    )
    return {
        "worker_run_id": worker_run_id,
        "output_artifact_id": frozen["artifact"]["_id"],
        "changed_files": frozen["changed_files"],
        "scope_violations": frozen["scope_violations"],
    }
