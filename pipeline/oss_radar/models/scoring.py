"""Turn model outputs into dashboard-ready scores, labels, and human-readable reasons.

* momentum_score: sigmoid of the growth model's predicted 70-day log-growth (0-100).
* risk_score: a transparent weighted composite, optionally blended with the promoted calibrated
  classifier probability; categorical safety floors remain authoritative after blending.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:  # annotations only — avoid dragging LightGBM into the lean dashboard image,
    # which installs the pipeline with --no-deps. risk_composite (used by the audit API) must
    # stay importable without scipy/sklearn/lightgbm. See dashboard/Dockerfile.
    from oss_radar.models.growth import GrowthModel
    from oss_radar.models.risk import RiskModel

_GROWTH_REASON = {
    "log_d56": ("strong 8-week download base", "small 8-week download base"),
    "log_d84": ("strong 12-week download base", "small 12-week download base"),
    "mom_28v28": ("monthly downloads rising vs prior month", "monthly downloads falling vs prior month"),
    "mom_56v56": ("8-week downloads rising vs prior 8 weeks", "8-week downloads falling vs prior 8 weeks"),
    "mom_28v56": ("recent month above the 8-week pace", "recent month below the 8-week pace"),
    "trend_slope_56": ("8-week download trend rising", "8-week download trend falling"),
    "trend_slope_84": ("12-week download trend rising", "12-week download trend falling"),
    "mom_7v7": ("weekly downloads accelerating vs prior week", "weekly downloads slowing vs prior week"),
    "mom_7v28": ("this week running above the monthly average", "this week below the monthly average"),
    "trend_slope_28": ("28-day download trend rising", "28-day download trend falling"),
    "velocity": ("high recent download volume", "low recent download volume"),
    "log_d7": ("strong weekly download base", "small weekly download base"),
    "log_d28": ("strong monthly download base", "small monthly download base"),
    "volatility_28": ("volatile download pattern", "steady download pattern"),
    "recent_share": ("downloads concentrated in the last week", "downloads spread over the month"),
    "trend_slope_7": ("7-day download trend rising", "7-day download trend falling"),
    "dow_volatility_7": ("spiky week of downloads", "smooth week of downloads"),
}


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def momentum_from_pred(growth_pred: float) -> tuple[float, str]:
    score = round(100 * _sigmoid(3.0 * growth_pred), 1)
    label = "high" if score >= 66 else "declining" if score <= 40 else "normal"
    return score, label


def _num(row: pd.Series, key: str):
    """Return a float, or None if the value is missing/NaN (pandas stores NA as NaN)."""
    v = row.get(key)
    if v is None or (isinstance(v, float) and v != v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _recent_vulnerability_severity(row: pd.Series) -> str:
    """Return severity derived only from advisories published in the recent window."""
    recent_severity = row.get("max_severity_new_28d")
    return recent_severity.upper() if isinstance(recent_severity, str) else ""


def _risk_safety_floor(row: pd.Series) -> tuple[float, str | None]:
    """Return a policy floor for categorical dependency hazards.

    Weighted averages are useful for ranking ordinary cases, but they must not dilute an
    archived/removed package or a recent critical signal into a "low" label.
    """
    status = row.get("status")
    removed = bool(isinstance(status, str) and status.strip())
    if row.get("archived") is True or removed:
        return 85.0, "archived / removed"

    recent_count = _num(row, "vuln_new_28d") or 0
    recent_severity = _recent_vulnerability_severity(row)
    if recent_count > 0 and recent_severity == "CRITICAL":
        return 75.0, "recent critical vulnerability signal"
    if recent_count > 0 and recent_severity == "HIGH":
        return 66.0, "recent high-severity vulnerability signal"
    return 0.0, None


def risk_composite(row: pd.Series) -> tuple[float, list[str]]:
    """Transparent 0-100 risk score from weighted components plus categorical safety floors."""
    comps: dict[str, float] = {}

    vuln_new = _num(row, "vuln_new_28d") or 0
    sev = _recent_vulnerability_severity(row)
    sev_w = {"CRITICAL": 1.0, "HIGH": 0.7, "MODERATE": 0.4, "LOW": 0.2}.get(sev, 0.0)
    comps["recent vulnerabilities"] = min(1.0, vuln_new * 0.5) * (0.5 + sev_w / 2)

    dslr = _num(row, "days_since_last_release")
    comps["release staleness"] = min(1.0, max(0.0, (dslr - 30) / 335)) if dslr is not None else 0.3

    bf = _num(row, "bus_factor")
    comps["maintainer key-person risk"] = (1.0 - bf) if bf is not None else 0.4

    sc = _num(row, "scorecard_overall")
    comps["weak security posture"] = (1.0 - sc / 10.0) if sc is not None else 0.35

    comps["abandoned / removed"] = 1.0 if (row.get("archived") is True or
                                           (isinstance(row.get("status"), str) and row.get("status"))) else 0.0

    issues = _num(row, "issues_opened_7d") or 0
    prs = _num(row, "prs_merged_7d") or 0
    comps["issue backlog pressure"] = min(1.0, issues / max(prs + issues, 1)) if (issues + prs) else 0.3

    weights = {
        "recent vulnerabilities": 0.24,
        "release staleness": 0.20,
        "maintainer key-person risk": 0.18,
        "weak security posture": 0.16,
        "abandoned / removed": 0.12,
        "issue backlog pressure": 0.10,
    }
    weighted_score = sum(comps[k] * weights[k] for k in weights) * 100
    safety_floor, safety_reason = _risk_safety_floor(row)
    score = max(weighted_score, safety_floor)
    reasons = [k for k, _ in sorted(comps.items(), key=lambda kv: kv[1] * weights[kv[0]], reverse=True)
               if comps[k] > 0.25][:2]
    if safety_reason:
        reasons = [safety_reason, *[reason for reason in reasons if reason != safety_reason]][:2]
    return round(score, 1), reasons


def _growth_reasons(shap_pairs: list[tuple[str, float]]) -> list[str]:
    out = []
    for feat, val in shap_pairs:
        pos, neg = _GROWTH_REASON.get(feat, (feat, feat))
        out.append(pos if val >= 0 else neg)
    return out[:2]


def persistence_growth_predictions(growth_scoring: pd.DataFrame) -> tuple[list[float], list]:
    """Return a deterministic momentum baseline when no promoted artifact is durable.

    The previous 56-day momentum ratio is projected over the 70-day product horizon. This is a
    transparent persistence fallback, not a trained challenger.
    """
    ratio_column = "mom_56v56" if "mom_56v56" in growth_scoring else "mom_28v28"
    if ratio_column not in growth_scoring:
        values = [0.0] * len(growth_scoring)
    else:
        ratios = pd.to_numeric(growth_scoring[ratio_column], errors="coerce").fillna(1.0)
        scale = 70.0 / (56.0 if ratio_column == "mom_56v56" else 28.0)
        values = [
            float(max(-0.9, min(3.0, math.log(max(0.05, min(20.0, value))) * scale)))
            for value in ratios
        ]
    reasons = [[(ratio_column, value)] for value in values]
    return values, reasons


def build_predictions(
    run_id: str,
    growth_scoring: pd.DataFrame,
    snapshots_latest: pd.DataFrame,
    risk_frame: pd.DataFrame,
    growth_model: GrowthModel | None,
    risk_model: RiskModel,
    growth_model_version: str | None = None,
    risk_model_version: str | None = None,
) -> pd.DataFrame:
    now = datetime.now(UTC)
    gs = growth_scoring.reset_index(drop=True)
    if growth_model is None:
        growth_pred, shap_rows = persistence_growth_predictions(gs)
    else:
        growth_pred = growth_model.predict(gs)
        shap_rows = growth_model.explain(gs)

    risk_proba = risk_model.predict_proba(risk_frame)
    proba_by_name = dict(zip(risk_frame["name"], risk_proba, strict=False))
    snap_by_name = {r["name"]: r for _, r in snapshots_latest.iterrows()}

    records = []
    for i, name in enumerate(gs["name"]):
        snap = snap_by_name.get(name)
        if snap is None:
            continue
        m_score, m_label = momentum_from_pred(float(growth_pred[i]))
        comp_score, risk_reasons = risk_composite(snap)
        p = proba_by_name.get(name)
        risk_score = round(0.6 * comp_score + 0.4 * (p * 100), 1) if p == p and p is not None else comp_score
        # A low classifier probability may not wash out an explicit safety condition.
        safety_floor, _ = _risk_safety_floor(snap)
        risk_score = max(risk_score, safety_floor)
        risk_level = "high" if risk_score >= 66 else "medium" if risk_score >= 40 else "low"

        momentum_reasons = _growth_reasons(shap_rows[i])
        reasons = momentum_reasons + risk_reasons
        records.append(
            {
                "run_id": run_id,
                "predicted_at": now,
                "growth_model_version": growth_model_version,
                "risk_model_version": risk_model_version,
                "name": name,
                "category": snap.get("category"),
                "momentum_score": m_score,
                "risk_score": risk_score,
                "risk_composite_score": comp_score,
                "risk_classifier_probability": (
                    round(float(p), 6) if p is not None and p == p else None
                ),
                "growth_pred_70d": round(float(growth_pred[i]), 4),
                "momentum_label": m_label,
                "risk_level": risk_level,
                "momentum_reasons": momentum_reasons,
                "risk_reasons": risk_reasons,
                "top_reasons": reasons,
            }
        )
    return pd.DataFrame(records)
