"""Prediction-drift detection between consecutive runs.

Each run compares its scores to the previous run so the system *notices* when the
ecosystem shifts (a release wave, a CVE spike, an ingestion regression). Uses the
Population Stability Index (PSI) on the score distributions plus label churn. The
DataScientist agent reports the result and escalates when drift is significant.

PSI rule of thumb: < 0.10 stable · 0.10–0.25 moderate · > 0.25 significant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    if len(expected) < 2 or len(actual) < 2:
        return 0.0
    edges = np.unique(np.quantile(np.concatenate([expected, actual]), np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    eps = 1e-4
    e = np.clip(np.histogram(expected, edges)[0] / len(expected), eps, None)
    a = np.clip(np.histogram(actual, edges)[0] / len(actual), eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def compute_prediction_drift(prev: pd.DataFrame | None, curr: pd.DataFrame) -> dict:
    """Drift of the current run's predictions vs. the previous run's."""
    if prev is None or prev.empty or curr.empty:
        return {"available": False, "severity": "none"}

    out: dict = {"available": True, "n_compared": int(len(curr))}
    for col in ("momentum_score", "risk_score"):
        if col in prev and col in curr:
            p = pd.to_numeric(prev[col], errors="coerce").dropna().to_numpy()
            c = pd.to_numeric(curr[col], errors="coerce").dropna().to_numpy()
            out[f"{col}_psi"] = round(_psi(p, c), 3)
            out[f"{col}_mean_shift"] = round(float(c.mean() - p.mean()), 2) if len(p) and len(c) else 0.0

    merged = prev.merge(curr, on="name", suffixes=("_prev", "_curr"))
    if len(merged):
        churns = []
        for lab in ("momentum_label", "risk_level"):
            a, b = f"{lab}_prev", f"{lab}_curr"
            if a in merged and b in merged:
                churns.append(float((merged[a] != merged[b]).mean()))
        out["label_churn"] = round(sum(churns) / len(churns), 3) if churns else 0.0
    else:
        out["label_churn"] = 0.0

    psi = max(out.get("momentum_score_psi", 0.0), out.get("risk_score_psi", 0.0))
    churn = out.get("label_churn", 0.0)
    out["severity"] = (
        "high" if psi > 0.25 or churn > 0.30
        else "moderate" if psi > 0.10 or churn > 0.15
        else "low"
    )
    return out
