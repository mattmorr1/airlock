"""Target app entrypoint.

Run:  TARGET_MONGO_URI=... TARGET_DB=target_app \
      uvicorn app.main:app --app-dir target-app --port 8090
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import get_db, seed
from .routes import auth as auth_routes
from .routes import items as item_routes


def _load_bulk():
    """The bulk feature is added by workers as app/bulk.py. If present, use it.
    This keeps the delegated feature a single added module, like worker output."""
    try:
        from . import bulk  # type: ignore
        return bulk
    except ImportError:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await seed()
    bulk = _load_bulk()
    if bulk is not None and hasattr(bulk, "setup"):
        await bulk.setup(get_db())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="target-items", lifespan=lifespan)
    app.include_router(auth_routes.router)

    # Register the bulk router BEFORE single-item routes so that PATCH
    # /items/bulk is not captured by PATCH /items/{item_id}.
    bulk = _load_bulk()
    if bulk is not None:
        app.include_router(bulk.router)

    app.include_router(item_routes.router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
