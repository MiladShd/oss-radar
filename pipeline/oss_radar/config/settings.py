"""Central configuration for OSS Radar.

Reads from environment variables (12-factor). The single most important switch is
``OSS_RADAR_BACKEND`` which selects the warehouse:

* ``duckdb``  — local file, zero cloud deps (default; used by tests & local dev)
* ``bigquery`` — managed warehouse in the configured GCP project (used in the Cloud Run job)

Everything is overridable via env so the exact same image runs locally and in the cloud.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _discover_project() -> str:
    """Best-effort GCP project discovery for local dev (the Cloud Run env sets GCP_PROJECT)."""
    for key in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        if os.environ.get(key):
            return os.environ[key]
    # local convenience: the bootstrap script writes the id to repo-root/.gcp_project
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidate = parent / ".gcp_project"
        if candidate.exists():
            return candidate.read_text().strip()
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OSS_RADAR_",
        env_file=".env",
        extra="ignore",
        protected_namespaces=(),
    )

    # --- Environment ---
    env: str = Field(default="local", description="local | cloud")
    backend: str = Field(default="duckdb", description="duckdb | bigquery")

    # --- GCP ---
    gcp_project: str = Field(default_factory=_discover_project)
    region: str = "us-central1"
    bq_dataset: str = "oss_radar"
    gcs_bucket: str = Field(default="", description="artifact bucket; defaults to <project>-oss-radar")

    # --- Local warehouse ---
    duckdb_path: str = "oss_radar.duckdb"

    # --- GitHub ---
    github_repo: str = "MiladShd/oss-radar"
    github_token: str = Field(default="", description="lifts REST limits to 5000/hr; from Secret Manager in cloud")

    # --- LLM agents (Claude) ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 1600

    # --- Pipeline knobs ---
    watchlist_limit: int = Field(default=0, description="0 = all packages")
    http_timeout: int = 30
    backfill_days: int = 180

    # --- Model hyperparameters ---
    growth_horizon_days: int = 7
    min_train_rows: int = 200
    random_seed: int = 42

    # --- Self-improvement ---
    risk_horizon_days: int = 14  # snapshot span before risk switches to realized-outcome labels
    forward_min_rows: int = 25   # min realized-outcome rows before the model trusts them

    @property
    def use_llm(self) -> bool:
        # real Anthropic keys start with sk-ant-; a sentinel (e.g. "DISABLED") => template mode
        return self.anthropic_api_key.startswith("sk-ant")

    @property
    def artifact_bucket(self) -> str:
        if self.gcs_bucket:
            return self.gcs_bucket
        return f"{self.gcp_project}-oss-radar" if self.gcp_project else "oss-radar-local"

    @property
    def is_cloud(self) -> bool:
        return self.backend == "bigquery"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
