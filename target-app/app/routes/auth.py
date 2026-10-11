"""Login route."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..auth import authenticate, issue_token
from ..models import LoginRequest, TokenResponse

router = APIRouter(prefix="/api/v1")


@router.post("/login/access-token", response_model=TokenResponse)
async def login(req: LoginRequest):
    user_id = await authenticate(req.email, req.password)
    if user_id is None:
        raise HTTPException(status_code=401, detail="bad credentials")
    return TokenResponse(access_token=issue_token(user_id))
