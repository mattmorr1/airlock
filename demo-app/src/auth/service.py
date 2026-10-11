"""Auth service functions: login() and create_session().

Contract (spec section 3):
  - login(email, password) -> token
  - a valid login creates exactly one session
  - an invalid login creates no session
  - no credential material in application logs

NOTE: this baseline contains a *planted* defect on the exception path. A
lookup miss sends an unknown email down an unhandled branch whose except block
logs the raw password. The HTTP response still looks like an ordinary rejected
login, so a request-shape diff cannot see it — only log inspection can. Harness
v1 (success path only) never exercises this branch. This is the escaped failure
the harness-evolution demo is built to catch (spec section 10).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from .db import hash_password

logger = logging.getLogger("demo.auth")

SESSION_TTL = timedelta(hours=1)


def _get_user(conn, email: str):
    cur = conn.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))
    return cur.fetchone()


def create_session(conn, user_id: int) -> str:
    token = uuid.uuid4().hex
    expires_at = (datetime.now(timezone.utc) + SESSION_TTL).isoformat()
    conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )
    conn.commit()
    return token


def login(conn, email: str, password: str) -> str | None:
    """Return a session token on success, or None on invalid credentials."""
    try:
        user = _get_user(conn, email)
        # BUG: when `user` is None (unknown email) this raises, and the except
        # branch below leaks the password. A known user with a wrong password
        # returns None cleanly without leaking.
        if user["password_hash"] != hash_password(password):
            return None
        return create_session(conn, user["id"])
    except Exception:
        # BUG: credential material written to logs on the error path.
        logger.error("login failed email=%s password=%s", email, password)
        return None
