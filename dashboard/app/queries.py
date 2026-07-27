"""Read-side queries over the OSS Radar warehouse (DuckDB locally, BigQuery in cloud)."""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re

import numpy as np
import pandas as pd

from oss_radar.config import get_settings
from oss_radar.source_health import github_recovery_guidance
from oss_radar.warehouse import get_warehouse

_PACKAGE_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,80}\Z")
_wh_cache = None
_SOURCE_HEALTH_THRESHOLD = 0.7


def _wh():
    global _wh_cache
    if _wh_cache is None:
        _wh_cache = get_warehouse(get_settings())
    return _wh_cache


def normalize_package_name(name: str) -> str:
    """Return a canonical package name, or an empty string for invalid input."""
    value = (name or "").strip().lower()
    return value if _PACKAGE_NAME.fullmatch(value) else ""


def _warehouse_label(wh) -> str:
    if getattr(wh, "path", None):
        return str(wh.path)
    project = getattr(wh, "project", "")
    dataset = getattr(wh, "dataset", "")
    return f"{project}.{dataset}".strip(".") or "configured warehouse"


def active_warehouse_label() -> str:
    """Identify the configured warehouse without requiring a successful query."""
    if _wh_cache is not None:
        return _warehouse_label(_wh_cache)
    settings = get_settings()
    if settings.backend == "bigquery":
        return (
            f"{settings.gcp_project}.{settings.bq_dataset}".strip(".")
            or "configured warehouse"
        )
    return str(settings.duckdb_path)


def _clean(obj):
    """Recursively coerce to JSON-safe values (numpy scalars, timestamps, NaN/inf)."""
    if obj is None or obj is pd.NaT:
        return None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (pd.Timestamp, _dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, np.ndarray)):
        return [_clean(v) for v in obj]
    return obj


def _df_records(df: pd.DataFrame) -> list[dict]:
    return _clean(df.where(pd.notna(df), None).to_dict("records"))


def _parse_reasons(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def _json_obj(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _normalize_growth_prediction(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if "growth_pred_70d" not in df.columns and "growth_pred_7d" in df.columns:
        df["growth_pred_70d"] = df["growth_pred_7d"]
    elif "growth_pred_70d" in df.columns and "growth_pred_7d" in df.columns:
        current = df["growth_pred_70d"]
        df["growth_pred_70d"] = current.where(current.notna(), df["growth_pred_7d"])
    return df


def _run_date(row: dict) -> str:
    started = row.get("started_at")
    if started:
        return str(started)[:10]
    run_id = str(row.get("run_id") or "")
    return f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}" if len(run_id) >= 8 else ""


def _duration_seconds(row: dict) -> float | None:
    try:
        start = pd.to_datetime(row.get("started_at"), utc=True)
        end = pd.to_datetime(row.get("finished_at"), utc=True)
        if pd.isna(start) or pd.isna(end):
            return None
        return round(max(0.0, (end - start).total_seconds()), 1)
    except Exception:  # noqa: BLE001
        return None


def _age_hours(value) -> float | None:
    try:
        timestamp = pd.to_datetime(value, utc=True)
        if pd.isna(timestamp):
            return None
        return max(
            0.0,
            (pd.Timestamp.now(tz="UTC") - timestamp).total_seconds() / 3600,
        )
    except Exception:  # noqa: BLE001
        return None


def _source_health(wh) -> list[dict]:
    """Return latest-run source success rates while preserving dashboard availability."""
    try:
        frame = wh.query_df(
            "SELECT source_status FROM snapshots WHERE run_id = "
            "(SELECT run_id FROM snapshots "
            "ORDER BY snapshot_date DESC, ingested_at DESC LIMIT 1)"
        )
    except Exception:  # noqa: BLE001
        return []
    per_source: dict[str, list[int]] = {}
    for raw in frame.get("source_status", pd.Series(dtype=object)).dropna():
        status = _json_obj(raw)
        for source, ok in status.items():
            per_source.setdefault(source, []).append(1 if ok else 0)

    is_cloud = bool(
        not getattr(wh, "path", None)
        and (
            getattr(wh, "project", None)
            or getattr(get_settings(), "is_cloud", False)
        )
    )
    github_hint = github_recovery_guidance(is_cloud=is_cloud)
    rows = []
    for source, observations in sorted(per_source.items()):
        if not observations:
            continue
        rate = round(sum(observations) / len(observations), 3)
        status = (
            "down"
            if rate == 0
            else "degraded"
            if rate < _SOURCE_HEALTH_THRESHOLD
            else "healthy"
        )
        rows.append({
            "source": source,
            "success_rate": rate,
            "status": status,
            "guidance": (
                github_hint
                if source == "github" and rate < _SOURCE_HEALTH_THRESHOLD
                else ""
            ),
        })
    return rows


def system_health(limit: int = 30) -> dict:
    """Current health summary with recent run history retained for inspection."""
    wh = _wh()
    try:
        runs_df = wh.query_df(f"SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT {int(limit)}")
    except Exception:  # noqa: BLE001
        return {
            "data_state": "error",
            "warehouse": active_warehouse_label(),
            "setup_command": "make demo",
            "status": "unknown",
            "headline": "pipeline-run warehouse query failed",
            "run_days": 0,
            "total_runs": 0,
            "error_count": 1,
            "warning_count": 0,
            "logs": [],
            "issues": [],
            "runs": [],
        }
    if runs_df.empty:
        return {
            "data_state": "empty",
            "warehouse": active_warehouse_label(),
            "setup_command": "make demo",
            "status": "unknown", "headline": "no pipeline runs recorded", "run_days": 0,
            "total_runs": 0, "error_count": 0, "warning_count": 0, "logs": [], "issues": [], "runs": [],
        }

    runs = _df_records(runs_df)
    for r in runs:
        r["stages"] = _json_obj(r.get("stages"))
        r["counts"] = _json_obj(r.get("counts"))
        r["run_date"] = _run_date(r)
        r["duration_sec"] = _duration_seconds(r)

    try:
        acts_df = wh.query_df("SELECT * FROM agent_activity ORDER BY ts DESC LIMIT 300")
    except Exception:  # noqa: BLE001
        acts_df = pd.DataFrame()
    acts = _df_records(acts_df) if not acts_df.empty else []
    latest = runs[0]
    latest_run_id = latest.get("run_id")
    latest_acts = [a for a in acts if a.get("run_id") == latest_run_id]

    issues = []
    warnings = []
    for index, r in enumerate(runs):
        status = str(r.get("status") or "").lower()
        if status == "running":
            running_age = _age_hours(r.get("started_at"))
            record = {
                "run_id": r.get("run_id"),
                "ts": r.get("started_at"),
                "source": "pipeline",
                "status": "running",
                "summary": (
                    f"Pipeline execution has remained running for {running_age:.1f}h"
                    if running_age is not None
                    else "Pipeline execution is running with an unknown start time"
                ),
            }
            (issues if running_age is None or running_age > 1.0 else warnings).append(record)
        elif index == 0 and status not in ("success", "ok"):
            issues.append({
                "run_id": r.get("run_id"), "ts": r.get("finished_at") or r.get("started_at"),
                "source": "pipeline", "status": r.get("status") or "unknown",
                "summary": f"Pipeline run ended with status {r.get('status') or 'unknown'}",
            })
    # Recovered warnings remain visible in run/agent history, but they must not keep
    # the current service badge yellow after a later clean run.
    for a in latest_acts:
        status = str(a.get("status") or "").lower()
        row = {
            "run_id": a.get("run_id"), "ts": a.get("ts"), "source": a.get("agent"),
            "status": a.get("status"), "summary": a.get("summary") or a.get("action") or "",
        }
        if status == "error":
            issues.append(row)
        elif status == "warning":
            warnings.append(row)

    try:
        freshness_hours = max(
            1.0,
            float(os.environ.get("OSS_RADAR_PIPELINE_FRESHNESS_HOURS", "30")),
        )
    except ValueError:
        freshness_hours = 30.0
    latest_status = str(latest.get("status") or "").lower()
    if latest_status in ("success", "ok"):
        success_age = _age_hours(latest.get("finished_at"))
        if success_age is None or success_age > freshness_hours:
            issues.append({
                "run_id": latest_run_id,
                "ts": latest.get("finished_at") or latest.get("started_at"),
                "source": "scheduler",
                "status": "stale",
                "summary": (
                    f"Latest successful run is {success_age:.1f}h old "
                    f"(freshness SLA {freshness_hours:.0f}h)"
                    if success_age is not None
                    else "Latest successful run has no usable completion timestamp"
                ),
            })

    health_status = "red" if issues else "yellow" if warnings else "green"
    run_days = len({r["run_date"] for r in runs if r.get("run_date")})
    logs = [{
        "ts": latest.get("started_at"), "level": "ok" if latest.get("status") == "success" else "error",
        "source": "pipeline", "message": f"Run {latest_run_id} status: {latest.get('status')}",
    }]
    for name, seconds in latest.get("stages", {}).items():
        logs.append({
            "ts": latest.get("finished_at"), "level": "ok",
            "source": "stage", "message": f"{name} completed in {seconds}s",
        })
    counts = latest.get("counts", {})
    if counts:
        logs.append({
            "ts": latest.get("finished_at"), "level": "ok", "source": "counts",
            "message": ", ".join(f"{k}={v}" for k, v in counts.items()),
        })
    for a in sorted(latest_acts, key=lambda x: str(x.get("ts") or "")):
        logs.append({
            "ts": a.get("ts"), "level": a.get("status") or "ok",
            "source": a.get("agent") or "agent",
            "message": f"{a.get('action')}: {a.get('summary')}",
        })

    headline = (
        f"green: {run_days} run day(s), no warnings or errors"
        if health_status == "green"
        else f"{health_status}: {len(issues)} error(s), {len(warnings)} warning(s)"
    )
    return _clean({
        "data_state": "ready",
        "warehouse": active_warehouse_label(),
        "status": health_status,
        "headline": headline,
        "run_days": run_days,
        "total_runs": len(runs),
        "first_run": runs[-1],
        "latest_run": latest,
        "error_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues[:80],
        "warnings": warnings[:80],
        "logs": logs[:120],
        "runs": runs,
    })


def latest_predictions() -> pd.DataFrame:
    wh = _wh()
    preds = wh.query_df(
        "SELECT * FROM predictions WHERE run_id = "
        "(SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)"
    )
    if preds.empty:
        return preds
    preds = _normalize_growth_prediction(preds)
    latest_snapshot = (
        "FROM snapshots WHERE run_id = "
        "(SELECT run_id FROM snapshots ORDER BY snapshot_date DESC LIMIT 1)"
    )
    try:
        snaps = wh.query_df(
            "SELECT name, primary_category, capabilities, github_topics, primary_language, "
            "repo, stars, forks, monthly_downloads, downloads_7d, dependent_repos_count, "
            "vuln_count, scorecard_overall, days_since_last_release, bus_factor, archived "
            + latest_snapshot
        )
    except Exception:  # noqa: BLE001
        # A freshly deployed dashboard may briefly precede the additive warehouse
        # migration performed by the next pipeline run.
        snaps = wh.query_df(
            "SELECT name, repo, stars, forks, monthly_downloads, downloads_7d, "
            "dependent_repos_count, vuln_count, scorecard_overall, "
            "days_since_last_release, bus_factor, archived "
            + latest_snapshot
        )
    merged = preds.merge(snaps, on="name", how="left")
    for column in (
        "top_reasons",
        "momentum_reasons",
        "risk_reasons",
        "capabilities",
        "github_topics",
    ):
        if column in merged:
            merged[column] = merged[column].apply(_parse_reasons)
    if "top_reasons" not in merged:
        merged["top_reasons"] = [[] for _ in range(len(merged))]
    legacy_reasons = merged["top_reasons"]
    for column, selector in (
        ("momentum_reasons", lambda reasons: reasons[:2]),
        ("risk_reasons", lambda reasons: reasons[-2:]),
    ):
        if column not in merged:
            merged[column] = legacy_reasons.apply(selector)
        else:
            merged[column] = [
                scoped if scoped else selector(legacy)
                for scoped, legacy in zip(merged[column], legacy_reasons, strict=False)
            ]
    return merged


def overview() -> dict:
    preds = latest_predictions()
    wh = _wh()
    runs = wh.query_df("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
    last_run = _df_records(runs)[0] if not runs.empty else None
    if preds.empty:
        return {
            "data_state": "empty",
            "warehouse": _warehouse_label(wh),
            "setup_command": "make demo",
            "tracked": 0,
            "last_run": last_run,
            "source_health": _source_health(wh),
            "movers": [],
            "risks": [],
            "categories": {},
        }

    movers = preds.sort_values("momentum_score", ascending=False).head(8)
    risks = preds.sort_values("risk_score", ascending=False).head(8)
    cats = {str(k): int(v) for k, v in preds.groupby("category").size().to_dict().items()}
    return _clean({
        "data_state": "ready",
        "warehouse": _warehouse_label(wh),
        "tracked": int(len(preds)),
        "avg_momentum": round(float(preds["momentum_score"].mean()), 1),
        "high_risk": int((preds["risk_level"] == "high").sum()),
        "rising": int((preds["momentum_label"] == "high").sum()),
        "last_run": last_run,
        "source_health": _source_health(wh),
        "movers": _df_records(movers),
        "risks": _df_records(risks),
        "categories": cats,
    })


def all_packages() -> list[dict]:
    preds = latest_predictions()
    if preds.empty:
        return []
    cols = [
        "name", "category", "primary_category", "capabilities", "github_topics",
        "primary_language", "repo", "momentum_score", "risk_score", "growth_pred_70d",
        "risk_composite_score", "risk_classifier_probability",
        "momentum_label", "risk_level", "momentum_reasons", "risk_reasons", "top_reasons",
        "stars", "monthly_downloads",
        "dependent_repos_count", "vuln_count", "scorecard_overall",
    ]
    cols = [c for c in cols if c in preds.columns]
    return _df_records(preds[cols].sort_values("momentum_score", ascending=False))


def package_detail(name: str) -> dict:
    name = normalize_package_name(name)
    if not name:
        return {"prediction": None, "downloads": [], "snapshots": []}
    wh = _wh()
    preds = latest_predictions()
    row = preds[preds["name"] == name]
    pred = _df_records(row)[0] if not row.empty else None

    hist = wh.query_df(
        "SELECT date, downloads FROM download_history WHERE name = ? ORDER BY date",
        (name,),
    )
    snaps = wh.query_df(
        "SELECT snapshot_date, stars, forks, open_issues, downloads_7d "
        "FROM snapshots WHERE name = ? ORDER BY snapshot_date",
        (name,),
    )
    return {
        "prediction": pred,
        "downloads": _df_records(hist),
        "snapshots": _df_records(snaps),
    }


def backtest() -> dict:
    df = _wh().query_df("SELECT payload FROM backtest ORDER BY created_at DESC LIMIT 1")
    if df.empty:
        return {}
    payload = df.iloc[0]["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return {}
    return _clean(payload)


def self_audit() -> dict:
    df = _wh().query_df("SELECT payload FROM self_audit ORDER BY created_at DESC LIMIT 1")
    if df.empty:
        return {"summary": {}, "packages": []}
    payload = df.iloc[0]["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return {"summary": {}, "packages": []}
    return _clean(payload)


def model_history() -> list[dict]:
    base_columns = (
        "run_id, model_name, trained_at, version, metric_name, metric_value, "
        "n_train, n_test, params, is_champion, gcs_uri, notes"
    )
    try:
        df = _wh().query_df(
            "SELECT "
            + base_columns
            + ", served_version, eval_provenance, comparison_version, "
            "comparison_metric_value, comparison_mode "
            "FROM model_runs ORDER BY trained_at"
        )
    except Exception:  # noqa: BLE001
        # Keep the dashboard deployable while an older warehouse is waiting for
        # the next additive schema migration.
        df = _wh().query_df(
            "SELECT " + base_columns + " FROM model_runs ORDER BY trained_at"
        )
    records = _df_records(df)
    for row in records:
        row["params"] = _json_obj(row.get("params"))
        row["served_version"] = row.get("served_version") or ""
        row["eval_provenance"] = _json_obj(row.get("eval_provenance"))
        row["comparison_version"] = row.get("comparison_version") or ""
        row.setdefault("comparison_metric_value", None)
        row["comparison_mode"] = row.get("comparison_mode") or ""
    return records


def agent_activity(limit: int = 60) -> list[dict]:
    df = _wh().query_df(f"SELECT * FROM agent_activity ORDER BY ts DESC LIMIT {int(limit)}")
    return _df_records(df)


def runs(limit: int = 30) -> list[dict]:
    df = _wh().query_df(f"SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT {int(limit)}")
    out = _df_records(df)
    for r in out:
        for k in ("stages", "counts"):
            if isinstance(r.get(k), str):
                try:
                    r[k] = json.loads(r[k])
                except Exception:  # noqa: BLE001
                    pass
    return out
