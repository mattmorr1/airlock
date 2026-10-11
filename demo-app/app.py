"""Demo auth application entrypoint.

Run standalone:  APP_DB=/tmp/x/auth.db APP_LOG=/tmp/x/app.log \
                 uvicorn app:app --app-dir demo-app --port 8080

Logging is configured to a file so the trusted runner can inspect captured log
output for planted credential material (spec section 9).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from src.auth.db import init_db
from src.auth.routes import router


def _configure_logging() -> None:
    log_path = os.environ.get("APP_LOG")
    handler: logging.Handler = logging.FileHandler(log_path) if log_path else logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def create_app() -> FastAPI:
    _configure_logging()
    init_db()
    app = FastAPI(title="demo-auth")
    app.include_router(router)
    return app


app = create_app()
