"""Runtime configuration, sourced from the environment.

Person B never hands Atlas or publication credentials to coding workers or to
the target application (spec section 4). The only secret a managed worker needs
is a short-lived inference credential, restricted to the inference gateway.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIRLOCK_", extra="ignore")

    # --- Atlas control-plane database (never shared with workers / app) ---
    # db_name reads AIRLOCK_DB to match Person A's controller/db.py, so B's
    # (sync) artifact/GridFS client and A's async client share one database.
    mongo_uri: str = "mongodb://localhost:27017"
    db_name: str = Field(default="airlock", validation_alias="AIRLOCK_DB")
    gridfs_bucket: str = "fs"

    # --- Execution backend ---------------------------------------------------
    # "container" is the real, isolated path (Docker). "local" is a dev-only
    # fallback that runs on the host: it is NOT a security boundary and must
    # never be used for untrusted code in production.
    execution_mode: Literal["container", "local"] = "local"
    runner_image: str = "airlock-runner:latest"
    worker_image: str = "airlock-worker:latest"

    # Scratch space for checkouts, archives, and disposable run dirs.
    work_root: Path = Path("/tmp/airlock")

    # --- Durable job queue leases -------------------------------------------
    lease_ttl_seconds: int = 120
    job_poll_interval_seconds: float = 1.0

    # --- Worker inference gateway (short-lived, egress-restricted) -----------
    inference_base_url: str | None = None
    worker_default_model: str = "sonnet"

    def ensure_work_root(self) -> Path:
        self.work_root.mkdir(parents=True, exist_ok=True)
        return self.work_root


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
