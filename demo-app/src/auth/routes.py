"""HTTP routes for the demo auth app."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .db import connect
from .service import login

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def post_login(req: LoginRequest):
    conn = connect()
    try:
        token = login(conn, req.email, req.password)
    finally:
        conn.close()
    if token is None:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": True, "token": token}, status_code=200)


@router.get("/healthz")
def healthz():
    return {"status": "ok"}
