"""End-to-end daily pipeline: ingest -> features -> train -> register -> score -> agents -> persist."""

from __future__ import annotations

import math
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
from oss_radar.registry import ModelRegistry, evaluation_lineage_matches
from oss_radar.warehouse import get_warehouse

log = structlog.get_logger(__name__)


def _git_sha() -> str:
    if os.environ.get("GIT_SHA"):
        return os.environ["GIT_SHA"]
    try:
        import subprocess

        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_pipeline(settings: Settings | None = None, dry_run: bool = False) -> dict:
    """Execute one observable pipeline run and persist terminal failures.

    The inner runner records ``running`` as soon as the warehouse is available. This outer
    boundary converts every otherwise-unhandled exception into ``failed`` while preserving the
    original traceback for Cloud Run retry/error reporting.
    """
    settings = settings or get_settings()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = datetime.now(UTC)
    stages: dict[str, float] = {}
    try:
        return _execute_pipeline(settings, dry_run, run_id, started, stages)
    except Exception as exc:
        try:
            wh = get_warehouse(settings)
            wh.init_schema()
            existing = wh.query_df(
                "SELECT status FROM pipeline_runs WHERE run_id = ?",
                (run_id,),
            )
            # The explicit no-data failure path already persists richer counts; keep it intact.
            already_failed = (
                not existing.empty
                and str(existing.iloc[0].get("status") or "").lower() == "failed"
            )
            if not already_failed:
                wh.upsert_rows("pipeline_runs", [{
                    "run_id": run_id,
                    "started_at": started,
                    "finished_at": datetime.now(UTC),
                    "status": "failed",
                    "stages": stages,
                    "counts": {"error_type": type(exc).__name__},
                    "git_sha": _git_sha(),
                }], ["run_id"])
        except Exception as record_exc:  # noqa: BLE001
            log.error(
                "pipeline.failure_status_persist_failed",
                run_id=run_id,
                error_type=type(record_exc).__name__,
            )
        log.exception(
            "pipeline.failed",
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise


def _execute_pipeline(
    settings: Settings,
    dry_run: bool,
    run_id: str,
    started: datetime,
    stages: dict[str, float],
) -> dict:
    log.info("pipeline.start", run_id=run_id, backend=settings.backend, dry_run=dry_run)

    wh = get_warehouse(settings)
    wh.init_schema()
    # Persist liveness before any network/model work. A hard crash leaves this row as ``running``
    # so the health API can surface the stuck execution instead of showing an older success.
    wh.upsert_rows("pipeline_runs", [{
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "status": "running",
        "stages": {},
        "counts": {},
        "git_sha": _git_sha(),
    }], ["run_id"])

    # 1) Ingest (+ self-healing of transient failures)
    t = time.time()
    healed = heal(collect(run_id, settings), settings, wh, run_id)
    snapshots, history, heal_stats = healed["snapshots"], healed["history"], healed["stats"]
    usable_snapshots = sum(snapshot.get("downloads_7d") is not None for snapshot in snapshots)
    if not snapshots or usable_snapshots == 0:
        finished = datetime.now(UTC)
        wh.upsert_rows("pipeline_runs", [{
            "run_id": run_id,
            "started_at": started,
            "finished_at": finished,
            "status": "failed",
            "stages": {"ingest": round(time.time() - t, 1)},
            "counts": {"packages": len(snapshots), "usable_snapshots": usable_snapshots},
            "git_sha": _git_sha(),
        }], ["run_id"])
        raise RuntimeError("ingestion produced no usable package download snapshots")
    wh.insert_rows("snapshots", snapshots)
    # The upstream API returns a rolling window and can revise recent dates. Upserting by the
    # natural package-day key keeps older days as an expanding learning set and replaces revisions.
    wh.upsert_rows("download_history", history, ["name", "date"])
    stages["ingest"] = round(time.time() - t, 1)

    import pandas as pd

    snap_df = pd.DataFrame(snapshots)
    hist_df = wh.query_df("SELECT name, date, downloads FROM download_history")

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
    growth = GrowthModel(
        features=active_download,
        seed=settings.random_seed,
        horizon_days=settings.growth_horizon_days,
    )
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
    matched_comparisons: dict[str, dict] = {}
    compatible_growth, compatible_growth_version = registry.load_champion(
        wh,
        "growth",
        GrowthModel,
        required_provenance=growth.eval_provenance or None,
    )
    incumbent_growth, incumbent_growth_version = compatible_growth, compatible_growth_version
    if incumbent_growth is None:
        legacy_growth, legacy_growth_version = registry.load_champion(
            wh, "growth", GrowthModel
        )
        if (
            legacy_growth is not None
            and legacy_growth.horizon_days == settings.growth_horizon_days
        ):
            incumbent_growth, incumbent_growth_version = legacy_growth, legacy_growth_version
    if incumbent_growth is not None and growth.model is not None:
        try:
            incumbent_metrics, incumbent_provenance = incumbent_growth.evaluate_closed_test(
                train_df
            )
            if (
                incumbent_provenance.get("benchmark_hash")
                == growth.eval_provenance.get("benchmark_hash")
                and evaluation_lineage_matches(
                    growth.eval_provenance, incumbent_provenance
                )
            ):
                matched_comparisons["growth"] = {
                    "incumbent_metric": incumbent_metrics.get("spearman"),
                    "incumbent_version": incumbent_growth_version,
                    "comparison_provenance": incumbent_provenance,
                }
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "pipeline.incumbent_comparison_failed",
                model="growth",
                version=incumbent_growth_version,
                error=str(exc),
            )
    incumbent_risk, incumbent_risk_version = registry.load_champion(
        wh,
        "risk",
        RiskModel,
        required_provenance=risk.eval_provenance or None,
    )
    if incumbent_risk is not None and risk.model is not None:
        try:
            incumbent_metrics, incumbent_provenance = incumbent_risk.evaluate_closed_test(
                risk_train
            )
            if (
                incumbent_provenance.get("benchmark_hash")
                == risk.eval_provenance.get("benchmark_hash")
                and evaluation_lineage_matches(
                    risk.eval_provenance, incumbent_provenance
                )
            ):
                matched_comparisons["risk"] = {
                    "incumbent_metric": incumbent_metrics.get("group_auc"),
                    "incumbent_version": incumbent_risk_version,
                    "comparison_provenance": incumbent_provenance,
                }
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "pipeline.incumbent_comparison_failed",
                model="risk",
                version=incumbent_risk_version,
                error=str(exc),
            )
    model_runs_rows: list[dict] = []
    model_metrics: dict[str, dict] = {}
    promoted: dict[str, bool] = {}
    for name, model_obj, metrics, params in [
        ("growth", growth, growth_metrics, {"model": "LightGBMRegressor", "horizon_days": settings.growth_horizon_days}),
        ("risk", risk, risk_metrics, {"model": "LightGBMClassifier",
                                      "evaluation": "stable-package-disjoint-holdout",
                                      "cv": "StratifiedGroupKFold diagnostic",
                                      "label_mode": risk_label_mode}),
    ]:
        gate_passed = gate.passed if (name == "growth" and settings.gate_enabled and not gate.skipped) else None
        if model_obj.model is not None:
            champ, rows = registry.persist(
                wh,
                run_id,
                name,
                model_obj,
                metrics,
                params,
                gate_passed=gate_passed,
                compatible_incumbent_available=(
                    compatible_growth is not None
                    if name == "growth"
                    else incumbent_risk is not None
                ),
                **matched_comparisons.get(name, {}),
            )
        else:
            champ, rows = False, []
        promoted[name] = champ
        model_runs_rows.extend(rows)
        model_metrics[name] = {
            **metrics,
            "is_champion": champ,
            "evaluation_provenance": getattr(model_obj, "eval_provenance", {}),
        }
    model_metrics["risk"]["label_mode"] = risk_label_mode
    model_metrics["growth"]["gate"] = {"passed": gate.passed, "skipped": gate.skipped, "reasons": gate.reasons}

    # 5) Score — champion means served. A candidate that merely trains or passes validation but
    # does not beat the best metric remains a challenger for BOTH models.
    serving_growth, growth_ver = registry.select_for_serving(
        wh, "growth", growth, promoted["growth"], GrowthModel)
    serving_risk, risk_ver = registry.select_for_serving(
        wh, "risk", risk, promoted["risk"], RiskModel)
    if (
        serving_growth is None
        and incumbent_growth is not None
        and incumbent_growth_version
        and "growth" in matched_comparisons
    ):
        # During the evaluation-schema migration, the stored incumbent row has no new lineage
        # metadata even though we just re-scored its artifact on the challenger's exact closed
        # cohort. Keep serving that verified incumbent if the candidate is held; otherwise the
        # first post-migration gate failure would blank every prediction.
        serving_growth = incumbent_growth
        growth_ver = incumbent_growth_version
        log.info(
            "pipeline.serving_rescored_legacy_growth_incumbent",
            version=growth_ver,
            benchmark=growth.eval_provenance.get("benchmark_hash"),
        )

    def _serving_note(name: str, version: str | None) -> str:
        if version is None:
            return "unavailable (no promoted champion)"
        suffix = "newly promoted" if version == f"{name}-{run_id}" else "previous champion"
        return f"{version} ({suffix})"

    model_metrics["growth"]["serving"] = _serving_note("growth", growth_ver)
    model_metrics["risk"]["serving"] = _serving_note("risk", risk_ver)
    model_metrics["growth"]["served_version"] = growth_ver
    model_metrics["risk"]["served_version"] = risk_ver
    if serving_growth is not None:
        current_incumbent_metric = matched_comparisons.get("growth", {}).get(
            "incumbent_metric"
        )
        model_metrics["growth"]["serving_spearman"] = (
            current_incumbent_metric
            if growth_ver == incumbent_growth_version and current_incumbent_metric is not None
            else serving_growth.metrics.get("spearman")
        )
        model_metrics["growth"]["served_eval_provenance"] = getattr(
            serving_growth, "eval_provenance", {}
        )
    if serving_risk is not None:
        current_incumbent_metric = matched_comparisons.get("risk", {}).get(
            "incumbent_metric"
        )
        model_metrics["risk"]["serving_auc"] = (
            current_incumbent_metric
            if risk_ver == incumbent_risk_version and current_incumbent_metric is not None
            else serving_risk.metrics.get("group_auc", serving_risk.metrics.get("auc"))
        )
        model_metrics["risk"]["served_eval_provenance"] = getattr(
            serving_risk, "eval_provenance", {}
        )
    else:
        # No unvalidated risk classifier is mixed into the transparent composite fallback.
        serving_risk = RiskModel(features=active_risk, seed=settings.random_seed)
        risk_ver = "risk-composite-v1"
        model_metrics["risk"]["served_version"] = risk_ver
        model_metrics["risk"]["serving"] = "composite-only (no promoted champion)"
    if serving_growth is None:
        growth_ver = "growth-persistence-v1"
        model_metrics["growth"]["served_version"] = growth_ver
        model_metrics["growth"]["serving"] = (
            "deterministic persistence baseline (no durable promoted champion)"
        )

    # Every candidate metric row records the artifact actually used for this run's predictions.
    served_versions = {"growth": growth_ver or "", "risk": risk_ver or ""}
    for row in model_runs_rows:
        if row["model_name"] in served_versions:
            row["served_version"] = served_versions[row["model_name"]]

    if not promoted["growth"]:
        log.info("pipeline.challenger_held", model="growth", serving=growth_ver)
    if not promoted["risk"]:
        log.info("pipeline.challenger_held", model="risk", serving=risk_ver)

    t = time.time()
    if not score_df.empty:
        predictions = build_predictions(run_id, score_df, snap_df, risk_df,
                                        serving_growth, serving_risk,
                                        growth_model_version=growth_ver,
                                        risk_model_version=risk_ver)
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
                    "is_champion": False, "gcs_uri": "", "served_version": "",
                    "eval_provenance": {}, "notes": f"drift {drift.get('severity')}"})

    # 5c) Backtest (held-out predicted vs actual) for the dashboard "Model accuracy" tab
    backtest_payload = {
        "growth": (
            growth_backtest(
                train_df,
                active_download,
                horizon_days=settings.growth_horizon_days,
            )
            if len(train_df) >= settings.min_train_rows
            else None
        ),
        "risk": risk_backtest(risk_train, active_risk),
        "label_mode": risk_label_mode,
        "served_versions": {"growth": growth_ver, "risk": risk_ver},
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
        "training_rows": len(train_df), "risk_training_rows": len(risk_train),
        "download_history_rows": len(hist_df), "activities": len(crew["activities"]),
    }
    wh.upsert_rows("pipeline_runs", [{
        "run_id": run_id, "started_at": started, "finished_at": finished, "status": "success",
        "stages": stages, "counts": counts, "git_sha": _git_sha(),
    }], ["run_id"])
    log.info("pipeline.done", run_id=run_id, stages=stages, counts=counts, pr=crew.get("pr_url"))
    return {"run_id": run_id, "counts": counts, "stages": stages,
            "model_metrics": model_metrics, "pr_url": crew.get("pr_url")}


def _persist_features(wh, run_id, score_df, risk_df, snap_df) -> None:
    if score_df.empty:
        return

    def download_count(row, column: str) -> float | None:
        value = row.get(column)
        try:
            numeric = float(value)
            return math.expm1(numeric) if math.isfinite(numeric) else None
        except (TypeError, ValueError):
            return None

    risk_by_name = {r["name"]: r for _, r in risk_df.iterrows()} if not risk_df.empty else {}
    cat_by_name = dict(zip(snap_df["name"], snap_df["category"], strict=False)) if not snap_df.empty else {}
    rows = []
    for _, r in score_df.iterrows():
        rk = risk_by_name.get(r["name"], {})
        row = {"run_id": run_id, "name": r["name"], "category": cat_by_name.get(r["name"]),
               "feature_date": r.get("feature_date"), "is_scoring_row": True,
               "at_risk_label": rk.get("at_risk_label")}
        # Storage columns are in download counts, while the model uses log1p counts.
        row["downloads_7d"] = download_count(r, "log_d7")
        row["downloads_28d"] = download_count(r, "log_d28")
        row["download_velocity"] = r.get("velocity")
        for col in ("bus_factor", "scorecard_overall", "release_cadence_days", "dependency_count"):
            if col in rk:
                row[col] = rk.get(col)
        rows.append(row)
    wh.insert_rows("features", rows)
