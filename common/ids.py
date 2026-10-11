"""ID generation and conventions.

Document `_id`s are controller-minted and are never trusted when supplied by a
model or worker (spec section 4: "Prompt markers, model-supplied task IDs, and
worker reports are not authorization credentials"). Content identity for
artifacts is the canonical tree hash, computed from bytes, not an `_id`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    """Opaque controller-minted id, e.g. ``worker_run`` -> ``worker_run_a1b2c3d4``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    """Real UTC Date value. Store these, not timestamp strings (spec 7)."""
    return datetime.now(timezone.utc)
