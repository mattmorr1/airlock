"""Target app entrypoint.

Run:  TARGET_MONGO_URI=... TARGET_DB=target_app \
      uvicorn app.main:app --app-dir target-app --port 8090
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import seed
from .routes import auth as auth_routes
from .routes import items as item_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    await seed()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="target-items", lifespan=lifespan)
    app.include_router(auth_routes.router)
    app.include_router(item_routes.router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
