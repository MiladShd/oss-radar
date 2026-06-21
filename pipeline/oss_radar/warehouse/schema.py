"""Backend-agnostic table definitions.

A column type is one of: STRING, INT, FLOAT, BOOL, DATE, TIMESTAMP, JSON.
Each backend maps these to its native types. JSON is stored as a serialized STRING
for maximum portability between DuckDB and BigQuery.
"""

from __future__ import annotations

Column = tuple[str, str]

# Point-in-time snapshot of every watched package, one row per package per run.
SNAPSHOTS: list[Column] = [
    ("run_id", "STRING"),
    ("snapshot_date", "DATE"),
    ("name", "STRING"),
    ("category", "STRING"),
    ("repo", "STRING"),
    # downloads (pypistats)
    ("downloads_1d", "INT"),
    ("downloads_7d", "INT"),
    ("downloads_28d", "INT"),
    ("download_velocity", "FLOAT"),
    ("download_acceleration", "FLOAT"),
    ("monthly_downloads", "INT"),
    # repo signals (ecosyste.ms + github)
    ("stars", "INT"),
    ("forks", "INT"),
    ("open_issues", "INT"),
    ("subscribers", "INT"),
    ("pushed_at", "TIMESTAMP"),
    ("created_at", "TIMESTAMP"),
    ("commit_count_4w", "INT"),
    ("prs_merged_7d", "INT"),
    ("issues_opened_7d", "INT"),
    ("bus_factor", "FLOAT"),
    ("archived", "BOOL"),
    # ecosystem (ecosyste.ms)
    ("dependent_packages_count", "INT"),
    ("dependent_repos_count", "INT"),
    ("rank_average", "FLOAT"),
    ("status", "STRING"),
    # versions / releases (pypi + deps.dev)
    ("latest_version", "STRING"),
    ("version_count", "INT"),
    ("days_since_last_release", "FLOAT"),
    ("release_cadence_days", "FLOAT"),
    ("dependency_count", "INT"),
    ("license", "STRING"),
    # security (deps.dev scorecard + osv)
    ("scorecard_overall", "FLOAT"),
    ("scorecard_maintained", "INT"),
    ("scorecard_branch_protection", "INT"),
    ("scorecard_code_review", "INT"),
    ("vuln_count", "INT"),
    ("vuln_new_14d", "INT"),
    ("vuln_new_28d", "INT"),
    ("max_severity", "STRING"),
    # provenance
    ("source_status", "JSON"),
    ("ingested_at", "TIMESTAMP"),
]

# Backfilled daily download series used to build supervised growth labels.
DOWNLOAD_HISTORY: list[Column] = [
    ("name", "STRING"),
    ("date", "DATE"),
    ("downloads", "INT"),
]

# Engineered package-day feature table (training rows + the latest scoring row).
FEATURES: list[Column] = [
    ("run_id", "STRING"),
    ("name", "STRING"),
    ("category", "STRING"),
    ("feature_date", "DATE"),
    ("downloads_7d", "FLOAT"),
    ("downloads_28d", "FLOAT"),
    ("download_velocity", "FLOAT"),
    ("download_acceleration", "FLOAT"),
    ("download_growth_28d", "FLOAT"),
    ("dow_log", "FLOAT"),
    ("stars", "FLOAT"),
    ("forks", "FLOAT"),
    ("open_issues", "FLOAT"),
    ("commit_count_4w", "FLOAT"),
    ("prs_merged_7d", "FLOAT"),
    ("issues_opened_7d", "FLOAT"),
    ("dependent_repos_count", "FLOAT"),
    ("dependent_packages_count", "FLOAT"),
    ("rank_average", "FLOAT"),
    ("days_since_last_release", "FLOAT"),
    ("release_cadence_days", "FLOAT"),
    ("dependency_count", "FLOAT"),
    ("bus_factor", "FLOAT"),
    ("scorecard_overall", "FLOAT"),
    ("scorecard_maintained", "FLOAT"),
    ("vuln_count", "FLOAT"),
    ("vuln_new_28d", "FLOAT"),
    ("archived", "FLOAT"),
    # labels (NULL on the latest scoring row)
    ("growth_target_7d", "FLOAT"),
    ("momentum_label", "STRING"),
    ("at_risk_label", "INT"),
    ("is_scoring_row", "BOOL"),
]

# Daily model output per package.
PREDICTIONS: list[Column] = [
    ("run_id", "STRING"),
    ("predicted_at", "TIMESTAMP"),
    ("name", "STRING"),
    ("category", "STRING"),
    ("momentum_score", "FLOAT"),
    ("risk_score", "FLOAT"),
    ("growth_pred_7d", "FLOAT"),
    ("momentum_label", "STRING"),
    ("risk_level", "STRING"),
    ("top_reasons", "JSON"),
]

# One row per metric per trained model per run — the model-improvement history.
MODEL_RUNS: list[Column] = [
    ("run_id", "STRING"),
    ("model_name", "STRING"),
    ("trained_at", "TIMESTAMP"),
    ("version", "STRING"),
    ("metric_name", "STRING"),
    ("metric_value", "FLOAT"),
    ("n_train", "INT"),
    ("n_test", "INT"),
    ("params", "JSON"),
    ("is_champion", "BOOL"),
    ("gcs_uri", "STRING"),
    ("notes", "STRING"),
]

# What each AI agent did, every run — powers the dashboard activity timeline.
AGENT_ACTIVITY: list[Column] = [
    ("run_id", "STRING"),
    ("ts", "TIMESTAMP"),
    ("agent", "STRING"),
    ("action", "STRING"),
    ("status", "STRING"),
    ("summary", "STRING"),
    ("artifact_url", "STRING"),
]

# One row per pipeline execution.
PIPELINE_RUNS: list[Column] = [
    ("run_id", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("status", "STRING"),
    ("stages", "JSON"),
    ("counts", "JSON"),
    ("git_sha", "STRING"),
]

# Held-out predicted-vs-actual backtest per run (metrics + calibration + scatter + ROC as JSON).
BACKTEST: list[Column] = [
    ("run_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("payload", "JSON"),
]

TABLES: dict[str, list[Column]] = {
    "snapshots": SNAPSHOTS,
    "download_history": DOWNLOAD_HISTORY,
    "features": FEATURES,
    "predictions": PREDICTIONS,
    "model_runs": MODEL_RUNS,
    "agent_activity": AGENT_ACTIVITY,
    "pipeline_runs": PIPELINE_RUNS,
    "backtest": BACKTEST,
}

DUCKDB_TYPES = {
    "STRING": "VARCHAR",
    "INT": "BIGINT",
    "FLOAT": "DOUBLE",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "VARCHAR",
}

BIGQUERY_TYPES = {
    "STRING": "STRING",
    "INT": "INT64",
    "FLOAT": "FLOAT64",
    "BOOL": "BOOL",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "STRING",
}
