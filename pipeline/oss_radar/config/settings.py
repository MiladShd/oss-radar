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
    growth_horizon_days: int = 70
    min_train_rows: int = 200
    random_seed: int = 42

    # --- Self-improvement ---
    risk_horizon_days: int = 14  # snapshot span before risk switches to realized-outcome labels
    forward_min_rows: int = 25   # min realized-outcome rows before the model trusts them
    feature_lift_margin: float = 0.01  # min held-out Spearman lift to propose a new feature

    # --- Validation gate (hard promotion/CI guard; see docs/VALIDATION.md + IMPROVEMENT.md) ---
    # A retrained growth model is promoted to champion (served) ONLY if it clears these. The
    # defaults pass the validated envelope (same-package R^2~0.58 / Spearman~0.79, unseen-package
    # R^2~0.36 / Spearman~0.68) and fail the leak signatures (the retired centered-MA leak scored
    # R^2~0.70; a shared-package leak blows out the same->unseen gap).
    gate_enabled: bool = True
    gate_min_spearman: float = 0.05            # held-out rank skill floor (beats chance)
    gate_min_r2: float = 0.0                   # held-out R^2 must beat the mean predictor
    gate_min_oof_spearman: float = 0.05        # unseen-package (GroupKFold) rank skill floor
    gate_max_r2: float = 0.90                  # ceiling: implausibly high R^2 == a re-introduced leak
    gate_max_generalization_gap: float = 0.40  # same-package R^2 minus unseen-package R^2
    gate_cv_splits: int = 5

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
