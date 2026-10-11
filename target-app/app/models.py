"""Request/response models for the target app."""
from __future__ import annotations

from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ItemCreate(BaseModel):
    title: str
    description: str = ""


class ItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
