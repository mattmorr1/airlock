"""Minimal but real bearer-token auth: the authorization boundary under test.

An HMAC-signed token keeps the demo dependency-light while still giving a
genuine `owner_id` boundary — the thing the bulk-update feature must respect.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from fastapi import Depends, Header, HTTPException

from .db import get_db, hash_password


def _secret() -> bytes:
    return os.environ.get("TARGET_SECRET", "dev-secret").encode()


def issue_token(user_id: str) -> str:
    body = base64.urlsafe_b64encode(user_id.encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _parse_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return base64.urlsafe_b64decode(body.encode()).decode()


async def authenticate(email: str, password: str) -> str | None:
    user = await get_db().users.find_one({"email": email})
    if user is None or user["password_hash"] != hash_password(password):
        return None
    return user["_id"]


async def get_current_user(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    user_id = _parse_token(authorization[len("Bearer "):])
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user_id


CurrentUser = Depends(get_current_user)
