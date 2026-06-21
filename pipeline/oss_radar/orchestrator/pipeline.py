"""End-to-end daily pipeline: ingest -> features -> train -> register -> score -> agents -> persist."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import structlog

from oss_radar.agents.crew import run_crew
from oss_radar.agents.llm import Claude
from oss_radar.audit import audit_own_dependencies
from oss_radar.config import Settings, get_settings
from oss_radar.config.active_features import active_download_features, active_risk_features
from oss_radar.features import build_growth_scoring, build_growth_training, build_risk_frame
from oss_radar.features.forward import choose_risk_training
from oss_radar.ingest.collector import collect
from oss_radar.ingest.healing import heal
from oss_radar.models.backtest import growth_backtest, risk_backtest
from oss_radar.models.drift import compute_prediction_drift
from oss_radar.models.growth import GrowthModel
from oss_radar.models.risk import RiskModel
from oss_radar.models.scoring import build_predictions
from oss_radar.models.validation_gate import GateResult, growth_gate
from oss_radar.registry import ModelRegistry
from oss_radar.warehouse import get_warehouse

log = structlog.get_logger(__name__)


def _git_sha() -> str:
    if os.environ.get("GIT_SHA"):
        return os.environ["GIT_SHA"][:12]
    try:
        import subprocess

        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_pipeline(settings: Settings | None = None, dry_run: bool = False) -> dict:
    settings = settings or get_settings()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = datetime.now(UTC)
    stages: dict[str, float] = {}
    log.info("pipeline.start", run_id=run_id, backend=settings.backend, dry_run=dry_run)

    wh = get_warehouse(settings)
    wh.init_schema()

    # 1) Ingest (+ self-healing of transient failures)
    t = time.time()
    healed = heal(collect(run_id, settings), settings, wh, run_id)
    snapshots, history, heal_stats = healed["snapshots"], healed["history"], healed["stats"]
    wh.truncate("download_history")
    wh.insert_rows("snapshots", snapshots)
    wh.insert_rows("download_history", history)
    stages["ingest"] = round(time.time() - t, 1)

    import pandas as pd

    snap_df = pd.DataFrame(snapshots)
    hist_df = pd.DataFrame(history)

    # 2) Features (active feature sets are PR-controlled; see config/active_features.json)
    t = time.time()
    active_download = active_download_features()
    active_risk = active_risk_features()
    train_df = build_growth_training(hist_df, horizon=settings.growth_horizon_days)
    score_df = build_growth_scoring(hist_df)
    risk_df = build_risk_frame(snap_df)
    # Risk training labels: realized-outcome once daily history spans the horizon, else heuristic.
    snap_history = wh.query_df("SELECT * FROM snapshots")
    risk_train, risk_label_mode = choose_risk_training(
        risk_df, snap_history, horizon_days=settings.risk_horizon_days, min_rows=settings.forward_min_rows)
    stages["features"] = round(time.time() - t, 1)

    # 3) Train
    t = time.time()
    growth = GrowthModel(features=active_download, seed=settings.random_seed)
    growth_metrics = growth.fit(train_df) if len(train_df) >= settings.min_train_rows else {
        "spearman": float("nan"), "note": "insufficient training rows", "n_train": len(train_df)}
    risk = RiskModel(features=active_risk, seed=settings.random_seed)
    risk_metrics = risk.fit(risk_train)
    stages["train"] = round(time.time() - t, 1)

    # 3b) VALIDATION GATE — does the retrained growth model clear the leak-free / beats-baseline /
    # generalises bar (docs/VALIDATION.md)? Promotion AND serving are gated on this, so the daily
    # self-improvement loop can never silently ship a leaky or sub-baseline model.
    gate = (growth_gate(train_df, active_download, settings)
            if settings.gate_enabled and growth.model is not None
            else GateResult(passed=True, skipped=True))
    if gate.metrics:
        growth_metrics.update(gate.as_metric_dict())
    log.info("pipeline.validation_gate", passed=gate.passed, skipped=gate.skipped,
             reasons=gate.reasons, **{k: round(v, 4) for k, v in gate.metrics.items() if v == v})

    # 4) Register (champion/challenger). Growth promotion is hard-gated: a candidate that fails
    # the validation gate is BLOCKED from becoming champion regardless of its primary metric.
    registry = ModelRegistry(settings)
    model_runs_rows: list[dict] = []
    model_metrics: dict[str, dict] = {}
    for name, model_obj, metrics, params in [
        ("growth", growth, growth_metrics, {"model": "LightGBMRegressor", "horizon_days": settings.growth_horizon_days}),
        ("risk", risk, risk_metrics, {"model": "LightGBMClassifier"}),
    ]:
        gate_passed = gate.passed if (name == "growth" and settings.gate_enabled and not gate.skipped) else None
        if model_obj.model is not None:
            champ, rows = registry.persist(wh, run_id, name, model_obj, metrics, params, gate_passed=gate_passed)
        else:
            champ, rows = False, []
        model_runs_rows.extend(rows)
        model_metrics[name] = {**metrics, "is_champion": champ}
    model_metrics["risk"]["label_mode"] = risk_label_mode
    model_metrics["growth"]["gate"] = {"passed": gate.passed, "skipped": gate.skipped, "reasons": gate.reasons}

    # 5) Score — serve the candidate only if it passed the gate; otherwise AUTO-ROLLBACK to the
    # last-good (gate-passed) champion so a failed model never reaches the dashboard.
    serving_growth, serving_note = growth, "candidate"
    if growth.model is not None and settings.gate_enabled and not gate.passed and not gate.skipped:
        champ_model, champ_ver = registry.load_champion(wh, "growth", GrowthModel)
        if champ_model is not None:
            serving_growth, serving_note = champ_model, f"rolled-back to {champ_ver}"
            log.warning("pipeline.auto_rollback", serving=champ_ver, reasons=gate.reasons)
        else:
            serving_note = "candidate (gate-failed; no prior champion to roll back to)"
            log.warning("pipeline.gate_failed_no_rollback", reasons=gate.reasons)
    model_metrics["growth"]["serving"] = serving_note

    t = time.time()
    if serving_growth.model is not None and not score_df.empty:
        predictions = build_predictions(run_id, score_df, snap_df, risk_df, serving_growth, risk)
    else:
        predictions = pd.DataFrame()
    stages["score"] = round(time.time() - t, 1)

    # 5b) Drift vs the previous run (current predictions not yet written => this is the prior run)
    prev_preds = wh.query_df(
        "SELECT name, momentum_score, risk_score, momentum_label, risk_level FROM predictions "
        "WHERE run_id = (SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)")
    drift = compute_prediction_drift(prev_preds if not prev_preds.empty else None, predictions)
    if drift.get("available"):
        now_d = datetime.now(UTC)
        for k in ("momentum_score_psi", "risk_score_psi", "label_churn"):
            if k in drift:
                model_runs_rows.append({
                    "run_id": run_id, "model_name": "monitor", "trained_at": now_d,
                    "version": f"monitor-{run_id}", "metric_name": k, "metric_value": float(drift[k]),
                    "n_train": None, "n_test": None, "params": {"severity": drift.get("severity")},
                    "is_champion": False, "gcs_uri": "", "notes": f"drift {drift.get('severity')}"})

    # 5c) Backtest (held-out predicted vs actual) for the dashboard "Model accuracy" tab
    backtest_payload = {
        "growth": growth_backtest(train_df, active_download) if len(train_df) >= settings.min_train_rows else None,
        "risk": risk_backtest(risk_train, active_risk),
        "label_mode": risk_label_mode,
    }
    wh.insert_rows("backtest", [{"run_id": run_id, "created_at": datetime.now(UTC),
                                 "payload": backtest_payload}])

    # 5d) Dogfood: audit OSS Radar's OWN dependencies and store the result (supply-chain self-check)
    t = time.time()
    try:
        self_audit = audit_own_dependencies(settings, on_demand=True)
        wh.insert_rows("self_audit", [{"run_id": run_id, "created_at": datetime.now(UTC),
                                       "payload": self_audit}])
        log.info("pipeline.self_audit", **self_audit.get("summary", {}))
    except Exception as exc:  # noqa: BLE001 — never let the self-audit break the run
        log.warning("pipeline.self_audit_failed", error=str(exc))
    stages["self_audit"] = round(time.time() - t, 1)

    # 6) Agents
    t = time.time()
    crew = run_crew(run_id, settings, Claude(settings), snap_df, predictions, model_metrics,
                    drift=drift, heal_stats=heal_stats, train_df=train_df,
                    active_download=active_download, dry_run=dry_run)
    stages["agents"] = round(time.time() - t, 1)

    # 7) Persist everything
    if not predictions.empty:
        wh.insert_rows("predictions", predictions.to_dict("records"))
    if model_runs_rows:
        wh.insert_rows("model_runs", model_runs_rows)
    if crew["activities"]:
        wh.insert_rows("agent_activity", crew["activities"])
    _persist_features(wh, run_id, score_df, risk_df, snap_df)

    finished = datetime.now(UTC)
    counts = {
        "packages": len(snapshots), "predictions": len(predictions),
        "training_rows": len(train_df), "activities": len(crew["activities"]),
    }
    wh.insert_rows("pipeline_runs", [{
        "run_id": run_id, "started_at": started, "finished_at": finished, "status": "success",
        "stages": stages, "counts": counts, "git_sha": _git_sha(),
    }])
    log.info("pipeline.done", run_id=run_id, stages=stages, counts=counts, pr=crew.get("pr_url"))
    return {"run_id": run_id, "counts": counts, "stages": stages,
            "model_metrics": model_metrics, "pr_url": crew.get("pr_url")}


def _persist_features(wh, run_id, score_df, risk_df, snap_df) -> None:
    if score_df.empty:
        return

    risk_by_name = {r["name"]: r for _, r in risk_df.iterrows()} if not risk_df.empty else {}
    cat_by_name = dict(zip(snap_df["name"], snap_df["category"], strict=False)) if not snap_df.empty else {}
    rows = []
    for _, r in score_df.iterrows():
        rk = risk_by_name.get(r["name"], {})
        row = {"run_id": run_id, "name": r["name"], "category": cat_by_name.get(r["name"]),
               "feature_date": r.get("feature_date"), "is_scoring_row": True,
               "at_risk_label": rk.get("at_risk_label")}
        for col in ("log_d7", "log_d28", "velocity"):
            row[{"log_d7": "downloads_7d", "log_d28": "downloads_28d", "velocity": "download_velocity"}[col]] = r.get(col)
        for col in ("bus_factor", "scorecard_overall", "release_cadence_days", "dependency_count"):
            if col in rk:
                row[col] = rk.get(col)
        rows.append(row)
    wh.insert_rows("features", rows)
