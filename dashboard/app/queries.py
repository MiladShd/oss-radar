"""Read-side queries over the OSS Radar warehouse (DuckDB locally, BigQuery in cloud)."""

from __future__ import annotations

import datetime as _dt
import json
import math
import re

import numpy as np
import pandas as pd

from oss_radar.config import get_settings
from oss_radar.warehouse import get_warehouse

_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")
_wh_cache = None


def _wh():
    global _wh_cache
    if _wh_cache is None:
        _wh_cache = get_warehouse(get_settings())
    return _wh_cache


def _safe(name: str) -> str:
    return _SAFE.sub("", name or "")[:80]


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
            return json.loads(val)
        except Exception:  # noqa: BLE001
            return []
    return []


def latest_predictions() -> pd.DataFrame:
    wh = _wh()
    preds = wh.query_df(
        "SELECT * FROM predictions WHERE run_id = "
        "(SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)"
    )
    if preds.empty:
        return preds
    snaps = wh.query_df(
        "SELECT name, repo, stars, forks, monthly_downloads, downloads_7d, dependent_repos_count, "
        "vuln_count, scorecard_overall, days_since_last_release, bus_factor, archived "
        "FROM snapshots WHERE run_id = (SELECT run_id FROM snapshots ORDER BY snapshot_date DESC LIMIT 1)"
    )
    merged = preds.merge(snaps, on="name", how="left")
    merged["top_reasons"] = merged["top_reasons"].apply(_parse_reasons)
    return merged


def overview() -> dict:
    preds = latest_predictions()
    runs = _wh().query_df("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
    last_run = _df_records(runs)[0] if not runs.empty else None
    if preds.empty:
        return {"tracked": 0, "last_run": last_run, "movers": [], "risks": [], "categories": {}}

    movers = preds.sort_values("momentum_score", ascending=False).head(8)
    risks = preds.sort_values("risk_score", ascending=False).head(8)
    cats = {str(k): int(v) for k, v in preds.groupby("category").size().to_dict().items()}
    return _clean({
        "tracked": int(len(preds)),
        "avg_momentum": round(float(preds["momentum_score"].mean()), 1),
        "high_risk": int((preds["risk_level"] == "high").sum()),
        "rising": int((preds["momentum_label"] == "high").sum()),
        "last_run": last_run,
        "movers": _df_records(movers),
        "risks": _df_records(risks),
        "categories": cats,
    })


def all_packages() -> list[dict]:
    preds = latest_predictions()
    if preds.empty:
        return []
    cols = ["name", "category", "repo", "momentum_score", "risk_score", "growth_pred_7d",
            "momentum_label", "risk_level", "top_reasons", "stars", "monthly_downloads",
            "dependent_repos_count", "vuln_count", "scorecard_overall"]
    cols = [c for c in cols if c in preds.columns]
    return _df_records(preds[cols].sort_values("momentum_score", ascending=False))


def package_detail(name: str) -> dict:
    name = _safe(name)
    wh = _wh()
    preds = latest_predictions()
    row = preds[preds["name"] == name]
    pred = _df_records(row)[0] if not row.empty else None

    hist = wh.query_df(
        f"SELECT date, downloads FROM download_history WHERE name = '{name}' ORDER BY date"
    )
    snaps = wh.query_df(
        f"SELECT snapshot_date, stars, forks, open_issues, downloads_7d "
        f"FROM snapshots WHERE name = '{name}' ORDER BY snapshot_date"
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


def model_history() -> list[dict]:
    df = _wh().query_df(
        "SELECT run_id, model_name, trained_at, version, metric_name, metric_value, "
        "n_train, is_champion FROM model_runs ORDER BY trained_at"
    )
    return _df_records(df)


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
