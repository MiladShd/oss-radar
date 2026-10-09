"""Reproducible evidence for the survival risk model (docs/SURVIVAL.md).

Reads the daily ``snapshots`` history (warehouse, or ``--snapshots-csv`` for an offline export), then:

  1. fits the Cox model on all history and reports hazard ratios with clustered robust CIs;
  2. checks the proportional-hazards assumption with Schoenfeld-residual trends;
  3. runs a temporal landmark evaluation (train on days <= T0, forecast later days) against baselines:
     fixed-horizon logistic regression on the same covariates, the existing composite risk heuristic,
     the single best covariate (advisory history), and the base rate;
  4. writes ``docs/survival_results.json``.

Usage:  python pipeline/scripts/survival_analysis.py [--snapshots-csv PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import warnings

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.linear_model import LogisticRegression

from oss_radar.models.scoring import risk_composite
from oss_radar.models.survival import (
    SURVIVAL_FEATURES,
    build_counting_process,
    fit_cox,
    fit_cox_selected,
    landmark_evaluation,
)

warnings.simplefilter("ignore")
TRAIN_ENDS = (60, 70, 80)
HORIZONS = (14, 28)


def _load(csv: str | None) -> pd.DataFrame:
    if csv:
        return pd.read_csv(csv, parse_dates=["snapshot_date", "ingested_at"])
    from oss_radar.warehouse import get_warehouse

    return get_warehouse().query_df("SELECT * FROM snapshots")


def _attach_composite(snap: pd.DataFrame, cp: pd.DataFrame) -> pd.DataFrame:
    s = snap.sort_values(["name", "snapshot_date"]).drop_duplicates(["name", "snapshot_date"], keep="last").copy()
    s["snapshot_date"] = pd.to_datetime(s["snapshot_date"]).dt.normalize()
    s["day"] = (s["snapshot_date"] - s["snapshot_date"].min()).dt.days
    scores = {(r["name"], int(r["day"]) + 1): risk_composite(r)[0] for _, r in s.iterrows()}
    out = cp.copy()
    out["composite"] = [scores.get((n, int(d)), np.nan) for n, d in zip(out["name"], out["day"], strict=True)]
    return out


def _schoenfeld_trend(cp: pd.DataFrame, fit) -> dict[str, dict]:
    """Proportional-hazards check: does the covariate's effect drift over time?

    On each event day t the Schoenfeld residual is s_t = mean_{i in D_t}(z_i) − z̄_t, the gap between the
    event packages' covariates and the risk-set average. If the hazard ratio were constant, s_t would show
    no trend in t; a Spearman trend with small p flags a time-varying effect."""
    Z = ((cp[fit.features].astype(float).fillna(dict(zip(fit.features, fit.fill, strict=True)))
          - fit.mean) / fit.scale).to_numpy()
    w = np.exp(Z @ fit.beta)
    days = cp["day"].to_numpy()
    ev = cp["event"].to_numpy() == 1
    rows = []
    for t in np.unique(days[ev]):
        m = days == t
        zbar = (w[m, None] * Z[m]).sum(axis=0) / w[m].sum()
        rows.append((t, Z[m & ev].mean(axis=0) - zbar))
    t_arr = np.array([r[0] for r in rows])
    res = np.array([r[1] for r in rows])
    out = {}
    for k, name in enumerate(fit.features):
        rho, p = spearmanr(t_arr, res[:, k])
        out[name] = {"spearman_rho": float(rho), "p_value": float(p), "n_event_days": int(len(t_arr))}
    return out


def _logistic_scorer(cp, fit, train_end, horizon):
    X, y = [], []
    fill = dict(zip(fit.features, fit.fill, strict=True))
    for L in range(1, train_end - horizon + 1):
        at = cp[cp["day"] == L + 1]
        fut = cp[(cp["day"] > L + 1) & (cp["day"] <= L + horizon)]
        had = set(fut.loc[fut["event"] == 1, "name"]) | set(at.loc[at["event"] == 1, "name"])
        X.append(at[fit.features].astype(float).fillna(fill).to_numpy())
        y.append(at["name"].isin(had).astype(int).to_numpy())
    Xs, ys = (np.vstack(X) - fit.mean) / fit.scale, np.concatenate(y)
    model = LogisticRegression(C=1.0, max_iter=1000).fit(Xs, ys)

    def score(rows):
        z = (rows[fit.features].astype(float).fillna(fill).to_numpy() - fit.mean) / fit.scale
        return model.predict_proba(z)[:, 1]

    return score, float(ys.mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots-csv")
    ap.add_argument("--out", default="docs/survival_results.json")
    args = ap.parse_args()

    snap = _load(args.snapshots_csv)
    cp = _attach_composite(snap, build_counting_process(snap))
    full = fit_cox(cp)
    summary = full.summary()
    out: dict = {
        "config": {
            "n_packages": int(cp["name"].nunique()), "n_days": int(cp["day"].max()),
            "n_rows": int(len(cp)), "n_events": int(cp["event"].sum()),
            "n_event_days": int(cp.loc[cp["event"] == 1, "day"].nunique()),
            "features": SURVIVAL_FEATURES, "ties": "breslow", "ridge_l2": full.l2,
            "event": "vuln_count increased vs the previous daily snapshot (new advisory)",
            "note": "covariates are the previous day's snapshot; calendar-time baseline hazard",
        },
        "full_sample_hazard_ratios": json.loads(summary.round(6).to_json(orient="records")),
        "likelihood_ratio_chi2": 2 * (full.log_likelihood - full.null_log_likelihood),
        "proportional_hazards_check": _schoenfeld_trend(cp, full),
        "landmark_evaluation": [],
    }
    for T0 in TRAIN_ENDS:
        train = cp[cp["day"] <= T0]
        cox_sel, cox_full = fit_cox_selected(train), fit_cox(train)
        for H in HORIZONS:
            logit, base_rate = _logistic_scorer(cp, cox_full, T0, H)
            scorers = {
                "cox_selected": (lambda r, f=cox_sel, h=H: f.event_probability(r, h)),
                "cox_full": (lambda r, f=cox_full, h=H: f.event_probability(r, h)),
                "logistic_fixed_horizon": logit,
                "composite_risk_heuristic": (lambda r: r["composite"].fillna(0).to_numpy()),
                "advisory_history_only": (lambda r: r["log_vuln_history"].fillna(0).to_numpy()),
                "base_rate": (lambda r, b=base_rate: np.full(len(r), b)),
            }
            for name, fn in scorers.items():
                res = landmark_evaluation(cp, cox_full, fn, first_landmark=T0, horizon_days=H)
                out["landmark_evaluation"].append({
                    "train_days": T0, "horizon_days": H, "method": name,
                    "features": cox_sel.features if name == "cox_selected" else None,
                    "auc": None if math.isnan(res.auc) else res.auc,
                    "brier": None if math.isnan(res.brier) else res.brier,
                    "mean_predicted": res.mean_predicted, "observed_rate": res.observed_rate,
                    "n_rows": res.n_rows, "n_positive": res.n_positive, "n_landmarks": res.n_landmarks,
                })
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, allow_nan=False)
    print(json.dumps(out["config"], indent=2))
    print(summary.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
